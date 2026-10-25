"""Contracts of the real-game evidence system; not substitutes for real acceptance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from autoadv.g2p import PronunciationCandidate
from autoadv.conversation import Token
from autoadv.hook_protocol import HookEvent, ThreadSource
from validation.real_game import fixture_runtime, issue_reporting, prepare_accuracy_assets as media
from validation.real_game.benchmark_alignment import run as run_alignment_benchmark
from validation.real_game.benchmark_human_vocal import summarize_frames as summarize_vocal_frames
from autoadv.acoustic_observers import VocalActivityFrame
from validation.real_game.gui_session import (
    ACCURACY_GAME_INPUTS_MS, CALIBRATION, SessionDriver, calibration_candidates, choose_calibration_source, load_session,
)
from validation.real_game.renpy.prepare_derived_audio import build as build_derived_audio
from validation.real_game.trace import ProductTraceObserver, ValidationTrace
from validation.real_game.verify_trace import scenario_for_phase, validate

HERE = Path(__file__).parent
ACCURACY_SCENARIO = scenario_for_phase('accuracy')


def test_fixture_source_identity_is_independent_of_checkout_line_endings():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        contents = {
            'builder.py': 'print("fixture")\n',
            'game/script.rpy': 'label start:\n    return\n',
            'CREDITS.txt': 'fixture credits\n',
        }
        for relative, content in contents.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8', newline='\n')
        lf_digest = fixture_runtime.source_digest(root)
        for relative in contents:
            path = root / relative
            data = path.read_bytes().replace(b'\r\n', b'\n').replace(b'\r', b'\n')
            path.write_bytes(data.replace(b'\n', b'\r\n'))
        assert fixture_runtime.source_digest(root) == lf_digest


def test_accuracy_fixture_and_evidence_form_one_continuous_authored_sequence():
    script = (HERE / 'renpy' / 'game' / 'script.rpy').read_text(encoding='utf-8')
    block = script.split('label advancement_accuracy:', 1)[1].split('\nlabel standard_tts:', 1)[0]
    fixture_texts = re.findall(r'^\s+(?:a|s|p|b|c) "(.*)"\s*$', block, re.M)
    evidence_ids = re.findall(r'^\s+\$ _adv_evidence_case\("([^"]+)"', block, re.M)
    assert fixture_texts == [line['text'] for line in ACCURACY_SCENARIO]
    assert evidence_ids == [line['id'] for line in ACCURACY_SCENARIO]
    assert re.search(r'^\s+return\s*$', block, re.M) is None
    assert 'jump advancement_accuracy_complete' in block
    assert re.search(
        r'label advancement_accuracy_complete:\s+pause\s+jump advancement_accuracy_complete',
        block,
    )

    evidence_source = (HERE / 'renpy' / 'game' / 'autoadv_evidence.rpy').read_text(encoding='utf-8')
    forbidden = ('get_playing', 'get_pos', 'periodic_callbacks', 'all_character_callbacks',
                 'wave', 'source_acoustic', 'mix_started', 'voice_started')
    assert all(token not in evidence_source for token in forbidden)

    source = HERE / 'renpy' / 'game' / 'audio' / 'voice'
    required = ('normal_001.wav', 'normal_020.wav', 'normal_030.wav', 'stress_cry.wav', 'whisper_001.wav')
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        for name in required:
            shutil.copy2(source / name, target / name)
        build_derived_audio(target)
        derived = sorted(path.name for path in target.glob('derived_*.wav'))
        expected = sorted({Path(line['foreground_file']).name for line in ACCURACY_SCENARIO
                           if str(line.get('foreground_file', '')).startswith('audio/voice/derived_')})
        assert derived == expected
        for name in derived:
            with wave.open(str(target / name), 'rb') as reader:
                assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (48_000, 1, 2)
                assert reader.getnframes() > 0

    manifest = json.loads((HERE / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
    oracle = json.loads((HERE / 'ALIGNMENT_ENDPOINT_ORACLE.json').read_text(encoding='utf-8'))
    sample_paths = {
        sample['path'] for sample in manifest['samples']
        if sample['id'] in oracle.get('samples', {})
    }
    directly_annotated = set(oracle.get('paths', {}))
    repo_foreground = {
        line['foreground_file'] for line in ACCURACY_SCENARIO
        if line.get('foreground_voice') and str(line.get('foreground_file', '')).startswith('audio/voice/')
    }
    assert repo_foreground <= sample_paths | directly_annotated


def test_accuracy_measurement_scope_covers_product_input_equivalence_classes():
    scenario = ACCURACY_SCENARIO
    assert scenario
    assert all(line['advance_mode'] == 'audio' and not line['use_tts'] for line in scenario)
    channels = {channel for line in scenario for channel in line['expected_mix_channels']}
    assert {'adv_background', 'adv_sfx'}.issubset(channels)

    # The fixture is owned by product-relevant equivalence classes, not stable
    # case names, positions, or a historical case count.
    assert 'hook_before_voice' in {line.get('voice_start_relation') for line in scenario}
    assert {'mid_voice_after_game_input', 'most_voice_elapsed', 'after_foreground_end'} <= {
        line.get('hook_delay_class') for line in scenario}
    assert {'starts_during_current', 'persistent_cross_epoch'} <= {
        line.get('background_vocal_relation') for line in scenario}
    assert {'loop_started_current_epoch', 'persistent_cross_epoch', 'finite_recurrent'} <= {
        line.get('vocal_source_class') for line in scenario}

    authored = [(index, line) for index, line in enumerate(scenario)
                if 'game_input_after_ms' in line]
    assert any(
        not line.get('foreground_voice') and not line.get('process_audio_expected')
        and index + 1 < len(scenario)
        and scenario[index + 1].get('hook_delay_class') == 'mid_voice_after_game_input'
        for index, line in authored
    )
    assert any(
        line.get('foreground_voice')
        and index + 1 < len(scenario)
        and not scenario[index + 1].get('foreground_voice')
        for index, line in authored
    )
    assert any(
        line.get('persistent_vocal_expected')
        and line.get('vocal_source_class') == 'persistent_cross_epoch'
        and index + 1 < len(scenario)
        and scenario[index + 1].get('hook_delay_class') == 'mid_voice_after_game_input'
        for index, line in authored
    )


def test_offline_verifier_uses_fixture_and_input_times_not_product_inference():
    case = ACCURACY_SCENARIO[0]
    start_ns = 1_000_000_000
    voice_ns = 1_010_000_000
    input_ns = 2_500_000_000
    records = [
        {'event': 'trace_started', 'version': 3, 'at_ns': start_ns},
        {'event': 'validation_collection_target', 'seq': 1, 'at_ns': start_ns + 1,
         'phase': 'accuracy', 'dialogues': 1, 'final_observation_seconds': 12.0},
        # Deliberately contradictory product inference. Offline classification must
        # not consume it.
        {'event': 'utterance_progress', 'seq': 2, 'at_ns': start_ns + 2,
         'session': 1, 'line': 1, 'state': 'active'},
        {'event': 'advance_attempt', 'seq': 3, 'at_ns': input_ns, 'session': 1, 'line': 1},
        {'event': 'advance_input', 'seq': 4, 'at_ns': input_ns + 1, 'session': 1, 'line': 1},
    ]
    fixture = [
        {'event': 'fixture_started', 'version': 1, 'seq': 0, 'at_ns': start_ns, 'pid': 50},
        {'event': 'fixture_case', 'seq': 1, 'at_ns': start_ns + 5, 'case': case['id'],
         'text': case['text'], 'voice_file': case['foreground_file']},
        {'event': 'fixture_voice_commanded', 'seq': 2, 'at_ns': voice_ns, 'case': case['id'],
         'voice_file': case['foreground_file'], 'duration_ms': 1637.0},
    ]
    result = validate(records, phase='accuracy', fixture_records=fixture)
    assert result['verdict'] == 'no_failure_observed'
    assert result['case_results'][0]['outcome'] == 'advanced'
    assert result['case_results'][0]['early_input'] is False
    assert result['facts']['advance_inputs'] == 1
    for removed in ('ok', 'collection_valid', 'measurement_valid', 'collection_complete'):
        assert removed not in result

    changed = [dict(row) for row in records]
    changed[2]['state'] = 'ended'
    assert validate(changed, phase='accuracy', fixture_records=fixture)['case_results'] == result['case_results']

    early = [dict(row) for row in records]
    early[3]['at_ns'] = voice_ns + 100_000_000
    early[4]['at_ns'] = early[3]['at_ns'] + 1
    early_result = validate(early, phase='accuracy', fixture_records=fixture)
    assert early_result['case_results'][0]['outcome'] == 'early_input'
    assert early_result['verdict'] == 'failure_observed'

    # A voiced case without an independent clean-source endpoint is measured as
    # incomplete rather than being promoted to a safe advancement by absence of
    # contrary evidence.
    unmeasured_fixture = [dict(row) for row in fixture]
    unmeasured_path = 'audio/runtime/voice/not-annotated.wav'
    unmeasured_fixture[1]['voice_file'] = unmeasured_path
    unmeasured_fixture[2]['voice_file'] = unmeasured_path
    unmeasured = validate(records, phase='accuracy', fixture_records=unmeasured_fixture)
    assert unmeasured['case_results'][0]['outcome'] == 'unmeasured_endpoint'
    assert unmeasured['case_results'][0]['foreground_endpoint_status'] == 'unmeasured'
    assert unmeasured['verdict'] == 'incomplete'

    # An authored external game input is a stimulus fact, not an AutoAdv input.
    # Even when it occurs before the clean-source endpoint it must not be called
    # an AutoAdv early advance.
    authored_records = [
        {'event': 'trace_started', 'version': 3, 'at_ns': start_ns},
        {'event': 'validation_collection_target', 'seq': 1, 'at_ns': start_ns + 1,
         'phase': 'accuracy', 'dialogues': 2, 'final_observation_seconds': 12.0},
        {'event': 'game_input_observed', 'seq': 2, 'at_ns': voice_ns + 90_000_000,
         'at_ms': 1234},
        {'event': 'validation_game_input', 'seq': 3, 'at_ns': voice_ns + 100_000_000,
         'case': ACCURACY_SCENARIO[0]['id'], 'scheduled_after_ms': 100},
        {'event': 'audio_input_lower_bound', 'seq': 4, 'at_ns': voice_ns + 150_000_000,
         'at_ms': 1234, 'source': 'external_input'},
    ]
    authored_fixture = [
        {'event': 'fixture_started', 'version': 1, 'seq': 0, 'at_ns': start_ns, 'pid': 50},
        {'event': 'fixture_case', 'seq': 1, 'at_ns': start_ns + 5,
         'case': ACCURACY_SCENARIO[0]['id'], 'text': ACCURACY_SCENARIO[0]['text'],
         'voice_file': ACCURACY_SCENARIO[0]['foreground_file']},
        {'event': 'fixture_voice_commanded', 'seq': 2, 'at_ns': voice_ns,
         'case': ACCURACY_SCENARIO[0]['id'],
         'voice_file': ACCURACY_SCENARIO[0]['foreground_file'], 'duration_ms': 1637.0},
        {'event': 'fixture_case', 'seq': 3, 'at_ns': voice_ns + 200_000_000,
         'case': ACCURACY_SCENARIO[1]['id'], 'text': ACCURACY_SCENARIO[1]['text'],
         'voice_file': ACCURACY_SCENARIO[1]['foreground_file']},
    ]
    authored = validate(authored_records, phase='accuracy', fixture_records=authored_fixture)
    first_result = authored['case_results'][0]
    assert first_result['outcome'] == 'authored_input'
    assert first_result['product_input_at_ns'] is None
    assert first_result['early_input'] is None
    assert authored['facts']['validation_game_inputs'] == 1
    # Posted keyboard messages are not low-level input. Even a coincident
    # observation must not be attributed to this driver stimulus by time alone.
    assert authored['facts']['game_input_observations'] == 1
    assert authored['facts']['external_input_lower_bounds'] == 1
    assert 'authored_input_observed_at_ns' not in first_result
    assert 'authored_input_causal_bound_ms' not in first_result
    assert not any('authored game input was not observed' in issue
                   for issue in authored['evidence_issues'])


def test_authored_game_input_uses_fixed_fixture_time_not_product_state(monkeypatch):
    sent, traces = [], []
    from autoadv import windows_input

    class TargetedKeyboard:
        def __init__(self, hwnd, *, virtual_key):
            sent.append(('bound', hwnd, virtual_key))

        def advance(self):
            sent.append(('sent',))

    monkeypatch.setattr(windows_input, 'WindowsAdvancementInput', TargetedKeyboard)

    runtime = SimpleNamespace(
        token=SimpleNamespace(session=1, line=8), pause_reason='audio_judgment_active_hold', sent=True,
        target=SimpleNamespace(pid=50, hwnd=77), profile=SimpleNamespace(input_key=13))
    product = SimpleNamespace(runtime_state=lambda: runtime)
    driver = SessionDriver(
        SimpleNamespace(application=product), SimpleNamespace(),
        {'timeout_seconds': 60},
        SimpleNamespace(emit=lambda name, **fields: traces.append((name, fields))),
        clock=lambda: 0.0,
    )
    case_id = 'room-clean-unvoiced'
    driver.current_case = case_id
    driver.current_case_seen_at = 10.0
    delay_s = ACCURACY_GAME_INPUTS_MS[case_id] / 1000.0
    assert not driver._send_authored_game_input(runtime, case_id, 10.0 + delay_s - 0.001)
    assert driver._send_authored_game_input(runtime, case_id, 10.0 + delay_s)
    assert sent == [('bound', 77, 13), ('sent',)]
    assert traces == [('validation_game_input', {
        'case': case_id, 'scheduled_after_ms': ACCURACY_GAME_INPUTS_MS[case_id]})]
    runtime.pause_reason = ''
    runtime.sent = False
    assert not driver._send_authored_game_input(runtime, case_id, 99.0)
    assert sent == [('bound', 77, 13), ('sent',)]


def test_validation_recovery_is_fixed_case_watchdog_not_product_state_interpretation():
    actions, recoveries, traces = [], [], []
    clock = [1.0]
    runtime = SimpleNamespace(
        token=SimpleNamespace(session=1, line=99), pause_reason='', sent=False,
        target=SimpleNamespace(pid=50, hwnd=77), profile=SimpleNamespace(input_key=13))
    product = SimpleNamespace(runtime_state=lambda: runtime)
    window = SimpleNamespace(application=product, close=lambda: actions.append('close'))
    trace = SimpleNamespace(emit=lambda name, **fields: traces.append((name, fields)))
    with tempfile.TemporaryDirectory() as directory:
        evidence = Path(directory) / 'fixture.jsonl'
        def write(cases):
            rows = [{'event': 'fixture_started', 'version': 1, 'seq': 0, 'at_ns': 1, 'pid': 50}]
            rows.extend({'event': 'fixture_case', 'seq': i + 1, 'at_ns': 2 + i,
                         'case': case, 'text': case, 'voice_file': None}
                        for i, case in enumerate(cases))
            evidence.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        write(['case-1'])
        driver = SessionDriver(
            window, SimpleNamespace(quit=lambda: actions.append('quit'),
                                    exit=lambda code: actions.append(('exit', code))),
            {'timeout_seconds': 60, 'phase': 'accuracy', 'dialogues': 3,
             'continue_collection': True, 'evidence': str(evidence),
             'case_watchdog_seconds': 12.0, 'final_observation_seconds': 12.0},
            trace, clock=lambda: clock[0], recovery_send=lambda state: recoveries.append(state.target.hwnd),
        )
        driver.state = 'running'
        driver.tick()  # observe case-1
        runtime.pause_reason = 'audio_judgment_active_hold'
        runtime.sent = True
        clock[0] = 13.1
        driver.tick()
        assert recoveries == [77]
        runtime.pause_reason = 'different_reason'
        clock[0] = 30.0
        driver.tick()
        assert recoveries == [77]

        write(['case-1', 'case-2'])
        clock[0] = 31.0
        driver.tick()  # observe case-2
        runtime.pause_reason = ''
        runtime.sent = False
        clock[0] = 43.1
        driver.tick()
        assert recoveries == [77, 77]
        assert [fields['case'] for name, fields in traces if name == 'validation_recovery_input'] == ['case-1', 'case-2']

        write(['case-1', 'case-2', 'case-3'])
        clock[0] = 44.0
        driver.tick()
        assert driver.collection_target_seen_at == 44.0
        clock[0] = 56.1
        driver.tick()
        assert actions[-2:] == ['close', 'quit']

        write(['case-1', 'case-2', 'case-3', 'case-1'])
        clock[0] = 57.0
        driver.tick()
        assert actions[-2:] == ['close', ('exit', 3)]
        assert traces[-1][0] == 'validation_driver_failure'
        assert 'additional case records' in traces[-1][1]['reason']


def test_trace_keeps_raw_hook_and_utterance_instead_of_hash_only_evidence():
    class Application:
        def __init__(self):
            self.state = SimpleNamespace(token=None, utterance=None)
        def configure_telemetry(self, observer): self.observer = observer
        def runtime_state(self): return self.state

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'run.jsonl'
        app = Application()
        observer = ProductTraceObserver(ValidationTrace(path), app)
        observer.install()
        raw = 'また、東寺のように、\nこれは音声のない台詞です。'
        app.observer('selected_hook_output', None, {
            'session': 1, 'source': 'HOOK|1|2', 'hook_sequence': 3, 'raw': raw})
        token = Token(1, 1)
        app.state = SimpleNamespace(
            token=token,
            utterance=SimpleNamespace(
                source='HOOK|1|2', sequence=3, raw=raw, text=raw, speaker='',
                advance_mode=SimpleNamespace(value='audio'), use_tts=False,
                uncertain_text_fallback=True, reading_unit_count=12, text_wait_ms=900))
        app.observer('utterance', token, {})
        app.observer('hook_error', token, {'reason': 'native diagnostic'})
        app.observer('native_ready', None, {'session': 1, 'ready': False, 'reason': 'capture unavailable', 'audio_pid': 9})
        host_event = HookEvent(
            kind='output', host_generation='g', connection_generation=1, sequence=3,
            received_ns=10, process_id=5,
            source=ThreadSource(5, 0x1234, 1, 2, 'H', 'name'), text='raw')
        app.observer('hook_event', token, {'hook_event': host_event, 'outcome': 'observed'})
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        hook = next(r for r in records if r['event'] == 'hook_output')
        utterance = next(r for r in records if r['event'] == 'utterance')
        assert hook['raw'] == raw and 'raw_hash' not in hook
        assert utterance['raw'] == raw and utterance['body'] == raw and 'body_hash' not in utterance
        assert next(r for r in records if r['event'] == 'hook_error')['reason'] == 'native diagnostic'
        unavailable = next(r for r in records if r['event'] == 'native_ready' and not r['ready'])
        assert unavailable['reason'] == 'capture unavailable' and unavailable['audio_pid'] == 9
        recorded_hook_event = next(r for r in records if r['event'] == 'hook_event')
        assert recorded_hook_event['hook_event']['source']['address'] == 0x1234
        assert recorded_hook_event['hook_event']['text'] == 'raw'


def test_calibration_uses_product_preview_and_session_contract_is_finite_and_bound():
    exact = SimpleNamespace(identity='a', address=20, ambiguous=False, history=(CALIBRATION,),
                            samples=(SimpleNamespace(raw=CALIBRATION, speaker='', body=CALIBRATION),))
    structured = SimpleNamespace(identity='b', address=10, ambiguous=False,
                                 history=('明日香\n' + CALIBRATION,),
                                 samples=(SimpleNamespace(raw='明日香\n' + CALIBRATION,
                                                          speaker='明日香', body=CALIBRATION),))
    noisy = SimpleNamespace(identity='c', address=30, ambiguous=False, history=('prefix ' + CALIBRATION,),
                            samples=(SimpleNamespace(raw='prefix ' + CALIBRATION, speaker='', body='prefix ' + CALIBRATION),))
    assert calibration_candidates((exact, structured, noisy)) == [exact, structured]
    assert choose_calibration_source((structured, exact)) is exact
    valid = dict(gate='session-gate', executable='validation-game.exe', phase='accuracy', timeout_seconds=120)
    assert load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(valid)})['phase'] == 'accuracy'
    with pytest.raises(ValueError):
        load_session({'AUTOADV_VALIDATION_SESSION': json.dumps({**valid, 'timeout_seconds': 0})})


def _comments():
    runs = {1: '20261023T130049Z-36c5ea8d', 2: '20261023T102922Z-d01111ed',
            3: '20261023T095133Z-4fdc7015', 4: '20261023T014504Z-0b2c7d31'}
    result = [{'id': 1000 + slot, 'body': f'{issue_reporting.history_marker(slot)}\n<!-- autoadv-validation-run:{run} -->\nold'}
              for slot, run in runs.items()]
    result.extend([
        {'id': 2001, 'body': issue_reporting.ARCHIVED_MARKER},
        {'id': 2002, 'body': issue_reporting.ARCHIVED_MARKER},
    ])
    return result


def test_issue_reporting_publishes_complete_persisted_evidence_across_bounded_comments_without_data_loss():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / '20261025T000000Z-runnew'; root.mkdir()
        summary = {
            'schema': 'autoadv-local-validation-v4', 'phase_requested': 'accuracy',
            'started_utc': '2026-10-25T00:00:00Z', 'ended_utc': '2026-10-25T00:01:00Z',
            'phases': [{'phase': 'accuracy', 'report_present': True, 'runner_error': None,
                        'facts': {'requested_cases': 2, 'fixture_cases': 2, 'hook_outputs': 1,
                                  'utterances': 1, 'presentation_completions': 1,
                                  'advance_inputs': 1, 'validation_game_inputs': 1, 'validation_recovery_inputs': 0}}],
            'launcher_error': None,
        }
        (root / 'validation-summary.json').write_text(json.dumps(summary), encoding='utf-8')

        raw = 'case2\ncase3'
        trace = [
            {'event': 'trace_started', 'version': 3, 'at_ns': 1},
            {'event': 'hook_output', 'seq': 1, 'at_ns': 11, 'raw': raw},
            {'event': 'native_audio_observation', 'seq': 2, 'at_ns': 12,
             'vocal_series': [{'source': 'nonlexical', 'frame_count': 5,
                               'active_count': 4, 'peak_score': .93,
                               'active_intervals': [[1072, 1312]]}],
             'recurrence_strength': .91, 'recurrence_period_ms': 640,
             'recurrence_stability': .82, 'recent_recurrence_strength': .88,
             'recurrence_level_consistency': .84,
             'recurrence_low_vocal_reference_similarity': .17},
        ]
        trace.extend({
            'event': 'alignment_inference', 'seq': i + 3, 'at_ns': 20 + i,
            'target_associated': bool(i % 2), 'padding': '音' * 180,
        } for i in range(220))
        (root / 'autoadv-accuracy.jsonl').write_text(
            ''.join(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n' for item in trace),
            encoding='utf-8')

        fixture = [
            {'event': 'fixture_started', 'version': 2, 'at_ns': 2},
            {'event': 'fixture_case', 'seq': 1, 'at_ns': 10, 'case': 'case2', 'text': 'case2', 'voice_file': 'jvs.wav'},
            {'event': 'fixture_voice_commanded', 'seq': 2, 'at_ns': 10, 'case': 'case2', 'voice_file': 'jvs.wav'},
        ]
        (root / 'autoadv-fixture-evidence.jsonl').write_text(
            ''.join(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n' for item in fixture),
            encoding='utf-8')

        report = {
            'schema': 'autoadv-offline-verification-v1', 'phase': 'accuracy',
            'verdict': 'failure_observed', 'facts': {'requested_cases': 2},
            'case_results': [{'case': 'case2', 'outcome': 'early_input', 'source_endpoint_ms': 1234.5}],
            'evidence_issues': [],
        }
        (root / 'autoadv-accuracy.result.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (root / 'validation.log').write_text(
            'runtime C:\\Users\\alice\\AutoAdv ghp_1234567890abcdefghijkl diagnostics\n',
            encoding='utf-8')

        snapshot = issue_reporting.format_snapshot(root, 'a' * 40)
        assert len(snapshot.parts) >= 2
        assert 'Complete persisted evidence' in snapshot.body
        assert 'autoadv-accuracy.jsonl' in snapshot.body
        assert 'autoadv-fixture-evidence.jsonl' in snapshot.body
        assert 'autoadv-accuracy.result.json' in snapshot.body
        assert 'validation-summary.json' in snapshot.body
        assert 'validation.log' in snapshot.body
        assert '"event":"native_audio_observation"' in snapshot.body
        assert '"event":"alignment_inference"' in snapshot.body
        assert '"source_endpoint_ms": 1234.5' in snapshot.body
        assert '"raw":"case2\\ncase3"' in snapshot.body
        assert '<USERPROFILE>' in snapshot.body and '<REDACTED-TOKEN>' in snapshot.body
        assert 'analysis evidence timeline' not in snapshot.body
        assert 'base64' not in snapshot.body

        plan = issue_reporting.build_upload_plan(root, 'a' * 40, _comments())
        history = [op for op in plan['operations'] if op['role'] == 'history']
        continuations = [op for op in plan['operations'] if op['role'] == 'continuation']
        assert len(history) == 1
        assert len(continuations) == len(snapshot.parts) - 1
        assert history[0]['slot'] == 4
        assert plan['operations'].index(history[0]) == len(continuations)
        assert all(len(op['body']) <= issue_reporting.COMMENT_BODY_TARGET_CHARS
                   for op in history + continuations)
        assert all(len(op['body'].encode('utf-8')) <= issue_reporting.COMMENT_BODY_TARGET_BYTES
                   for op in history + continuations)
        # Existing archived comments are recycled before any new comment is created.
        assert all(op['action'] == 'patch' for op in continuations[:2])


def test_accuracy_assets_and_alignment_benchmark_verify_inputs_without_becoming_acceptance_proxy():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        payload = b'fixture-audio'
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({
            'schema': 'autoadv-advancement-accuracy-assets-v1',
            'assets': [{
                'id': 'sample', 'kind': 'foreground_voice', 'filename': 'sample.wav',
                'runtime_path': 'game/audio/runtime/voice/sample.wav',
                'sha256': hashlib.sha256(payload).hexdigest(), 'size_bytes': len(payload),
                'max_bytes': 1024, 'source_page': 'https://example.test/source',
                'urls': ['https://example.test/sample.wav'], 'license': 'test-only',
                'redistributable': False,
            }],
        }), encoding='utf-8')
        cache = root / 'cache'; cache.mkdir(); (cache / 'sample.wav').write_bytes(payload)
        downloader = Mock(side_effect=AssertionError('verified cache must not download'))
        assert media.prepare(cache, manifest_path=manifest, downloader=downloader)['assets'][0]['source'] == 'cache'
        downloader.assert_not_called()
        runtime = root / 'runtime'; runtime.mkdir(); (runtime / 'AutoAdvValidationGame.exe').write_bytes(b'MZ')
        media.hydrate(runtime, cache, manifest_path=manifest)
        assert (runtime / 'game/audio/runtime/voice/sample.wav').read_bytes() == payload

    class StaticG2P:
        def candidates(self, text, *, max_candidates=4):
            return (PronunciationCandidate(('a', 'b', 'c')),)
    class StaticModel:
        frame_stride_ms = 20.0
        vocab = {'PAD': 0, 'UNK': 1, 'SOS': 2, 'EOS': 3, 'a': 4, 'b': 5, 'c': 6}
        def infer(self, chunks):
            frames = max(1, int(sum(chunk.duration_ms for chunk in chunks) // self.frame_stride_ms))
            logits = np.full((frames, len(self.vocab)), -4.0, dtype=np.float32)
            logits[:, 0] = 5.0
            for phone, frame in zip(('a', 'b', 'c'), (5, 10, 15)):
                if frame < frames:
                    logits[frame, 0] = 0.0; logits[frame, self.vocab[phone]] = 7.0
            return logits

    with tempfile.TemporaryDirectory() as root:
        model, vocab = Path(root) / 'model', Path(root) / 'vocab'
        model.write_bytes(b'x'); vocab.write_bytes(b'x')
        report = run_alignment_benchmark(
            model, vocab, conditions=('clean',), rounds=1, seed=10,
            model_backend=StaticModel(), g2p=StaticG2P())
    assert report['schema'] == 'autoadv-alignment-benchmark-v3'
    assert 'cer' not in report['conditions']['clean']
    assert not report['real_game_verified']
    assert report['conditions']['clean']['runs'] > 0
    assert 'early_commits' in report['conditions']['clean']
    assert 'cumulative_compute_ratio' in report['conditions']['clean']


def test_human_vocal_diagnostic_preserves_observer_scores_without_acceptance_verdict():
    frames = (
        VocalActivityFrame(at_ms=100, score=0.08, support_start_ms=0, source="nonlexical"),
        VocalActivityFrame(at_ms=340, score=0.22, support_start_ms=0, source="nonlexical"),
        VocalActivityFrame(at_ms=580, score=0.51, support_start_ms=0, source="nonlexical"),
        VocalActivityFrame(at_ms=116, score=0.72, support_start_ms=100, source="speech"),
    )
    measured = summarize_vocal_frames(frames)
    assert set(measured) == {"nonlexical", "speech"}
    assert measured["nonlexical"]["frame_count"] == 3
    assert measured["nonlexical"]["peak_score"] == 0.51
    assert measured["nonlexical"]["thresholds"]["0.20"]["active_count"] == 2
    assert measured["speech"]["thresholds"]["0.60"]["active_count"] == 1
    assert "verdict" not in measured
