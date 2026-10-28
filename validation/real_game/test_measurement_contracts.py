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
from autoadv.model import Token
from autoadv.hook_protocol import HookEvent, ThreadSource
from validation.real_game import fixture_runtime, issue_reporting, prepare_accuracy_assets as media
from validation.real_game.benchmark_alignment import run as run_alignment_benchmark
from validation.real_game.benchmark_human_vocal import summarize_frames as summarize_vocal_frames
from autoadv.acoustic_observers import VocalActivityFrame
from validation.real_game.gui_session import (
    ACCURACY_GAME_INPUTS_MS, CALIBRATION, FIXTURE_BODY_HOOK_IDENTITY,
    FIXTURE_SPEAKER_HOOK_IDENTITY, FIXTURE_SPEAKER_RULE,
    SessionDriver, fixture_source, load_session,
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
        expected = sorted({Path(line['voice_file']).name for line in ACCURACY_SCENARIO
                           if str(line.get('voice_file', '')).startswith('audio/voice/derived_')})
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
    repo_authored_voice = {
        line['voice_file'] for line in ACCURACY_SCENARIO
        if str(line.get('voice_file', '')).startswith('audio/voice/')
    }
    assert repo_authored_voice <= sample_paths | directly_annotated


def test_accuracy_measurement_scope_covers_product_input_equivalence_classes():
    scenario = ACCURACY_SCENARIO
    assert scenario
    assert all(line['advance_mode'] == 'audio' and not line['use_tts'] for line in scenario)
    channels = {channel for line in scenario for channel in line['expected_mix_channels']}
    assert {'adv_vocal_loop', 'adv_sfx'}.issubset(channels)

    # Coverage is expressed as separate observable facts. A case may carry
    # several at once; no source is assigned a dialogue role or treated as owned
    # to the current dialogue merely because it overlaps in time.
    axis_names = {
        'text_correspondence', 'finite_vocal_activity', 'finite_vocal_repetition',
        'vocal_loop', 'vocal_crosses_boundary', 'delayed_vocal_onset',
    }
    assert all(set(line['audio_facts']) == axis_names for line in scenario)
    for line in scenario:
        facts = line['audio_facts']
        # These are authored facts, not mutually-exclusive source classes.
        # More specific temporal patterns may only be asserted when the
        # corresponding human-vocal activity is actually present.
        assert not facts['finite_vocal_repetition'] or facts['finite_vocal_activity']
        assert not facts['vocal_crosses_boundary'] or (
            facts['finite_vocal_activity'] or facts['vocal_loop'])
        assert not facts['delayed_vocal_onset'] or (
            facts['finite_vocal_activity'] or facts['vocal_loop'])
    combinations = {tuple(line['audio_facts'][name] for name in sorted(axis_names)) for line in scenario}
    assert len(combinations) > 4

    def has(**facts):
        return any(all(line['audio_facts'][key] is value for key, value in facts.items())
                   for line in scenario)

    assert has(text_correspondence=True, finite_vocal_activity=True, vocal_loop=False)
    assert has(text_correspondence=False, finite_vocal_activity=False, vocal_loop=False)
    assert has(text_correspondence=False, finite_vocal_activity=True, vocal_loop=False)
    assert has(text_correspondence=True, finite_vocal_activity=True, vocal_loop=True)
    assert has(text_correspondence=False, finite_vocal_activity=False, vocal_loop=True)
    assert has(finite_vocal_repetition=True, vocal_loop=False)
    assert has(vocal_loop=True, vocal_crosses_boundary=True)
    assert has(delayed_vocal_onset=True)

    # Timing perturbations remain independent fixture facts rather than new
    # audio-source classes.
    assert {'mid_voice_after_game_input', 'most_voice_elapsed', 'after_voice_end'} <= {
        line.get('hook_delay_class') for line in scenario}
    assert any('game_input_after_ms' in line for line in scenario)
    assert any('recurrent_vocal_start_after_ms' in line for line in scenario)


def _offline_verifier_sample():
    case = ACCURACY_SCENARIO[0]
    start_ns = 1_000_000_000
    voice_ns = 1_010_000_000
    input_ns = 2_500_000_000
    records = [
        {'event': 'trace_started', 'version': 3, 'at_ns': start_ns},
        {'event': 'validation_collection_target', 'seq': 1, 'at_ns': start_ns + 1,
         'phase': 'accuracy', 'dialogues': 1, 'final_observation_seconds': 12.0},
        {'event': 'utterance_progress', 'seq': 2, 'at_ns': start_ns + 2,
         'session': 1, 'line': 1, 'state': 'active'},
        {'event': 'advance_attempt', 'seq': 3, 'at_ns': input_ns, 'session': 1, 'line': 1},
        {'event': 'advance_input', 'seq': 4, 'at_ns': input_ns + 1, 'session': 1, 'line': 1},
    ]
    fixture = [
        {'event': 'fixture_started', 'version': 1, 'seq': 0, 'at_ns': start_ns, 'pid': 50},
        {'event': 'fixture_case', 'seq': 1, 'at_ns': start_ns + 5, 'case': case['id'],
         'text': case['text'], 'voice_file': case['voice_file']},
        {'event': 'fixture_voice_commanded', 'seq': 2, 'at_ns': voice_ns, 'case': case['id'],
         'voice_file': case['voice_file'], 'duration_ms': 1637.0},
    ]
    return records, fixture, voice_ns


def test_offline_verifier_classifies_input_from_independent_fixture_time_not_product_inference():
    records, fixture, voice_ns = _offline_verifier_sample()
    result = validate(records, phase='accuracy', fixture_records=fixture)
    assert result['verdict'] == 'no_failure_observed'
    assert result['case_results'][0]['outcome'] == 'advanced'
    assert result['case_results'][0]['early_input'] is False
    assert result['facts']['advance_inputs'] == 1

    # Contradictory product inference is evidence under analysis, never the
    # offline oracle. Changing it cannot change the independent classification.
    changed = [dict(row) for row in records]
    changed[2]['state'] = 'ended'
    assert validate(changed, phase='accuracy', fixture_records=fixture)['case_results'] == result['case_results']

    early = [dict(row) for row in records]
    early[3]['at_ns'] = voice_ns + 100_000_000
    early[4]['at_ns'] = early[3]['at_ns'] + 1
    early_result = validate(early, phase='accuracy', fixture_records=fixture)
    assert early_result['case_results'][0]['outcome'] == 'early_input'
    assert early_result['verdict'] == 'failure_observed'


def test_offline_verifier_reports_unmeasured_authored_voice_endpoint_as_incomplete():
    records, fixture, _voice_ns = _offline_verifier_sample()
    unmeasured_path = 'audio/runtime/voice/not-annotated.wav'
    unmeasured_fixture = [dict(row) for row in fixture]
    unmeasured_fixture[1]['voice_file'] = unmeasured_path
    unmeasured_fixture[2]['voice_file'] = unmeasured_path
    result = validate(records, phase='accuracy', fixture_records=unmeasured_fixture)
    assert result['case_results'][0]['outcome'] == 'unmeasured_endpoint'
    assert result['case_results'][0]['authored_voice_endpoint_status'] == 'unmeasured'
    assert result['verdict'] == 'incomplete'


def test_offline_verifier_keeps_authored_external_input_separate_from_product_advancement():
    _records, fixture, voice_ns = _offline_verifier_sample()
    start_ns = fixture[0]['at_ns']
    authored_records = [
        {'event': 'trace_started', 'version': 3, 'at_ns': start_ns},
        {'event': 'validation_collection_target', 'seq': 1, 'at_ns': start_ns + 1,
         'phase': 'accuracy', 'dialogues': 2, 'final_observation_seconds': 12.0},
        {'event': 'game_input_observed', 'seq': 2, 'at_ns': voice_ns + 90_000_000,
         'at_ms': 1234},
        {'event': 'validation_game_input', 'seq': 3, 'at_ns': voice_ns + 100_000_000,
         'case': ACCURACY_SCENARIO[0]['id'], 'scheduled_after_ms': 100},
        {'event': 'game_input_candidates_attached', 'seq': 4,
         'at_ns': voice_ns + 150_000_000,
         'text_received_ns': voice_ns + 140_000_000,
         'input_candidates_ms': [1234]},
    ]
    authored_fixture = fixture + [{
        'event': 'fixture_case', 'seq': 3, 'at_ns': voice_ns + 200_000_000,
        'case': ACCURACY_SCENARIO[1]['id'], 'text': ACCURACY_SCENARIO[1]['text'],
        'voice_file': ACCURACY_SCENARIO[1]['voice_file'],
    }]
    result = validate(authored_records, phase='accuracy', fixture_records=authored_fixture)
    first = result['case_results'][0]
    assert first['outcome'] == 'authored_input'
    assert first['product_input_at_ns'] is None and first['early_input'] is None
    assert result['facts']['validation_game_inputs'] == 1
    assert result['facts']['game_input_observations'] == 1
    assert result['facts']['external_input_candidate_attachments'] == 1
    assert result['facts']['external_input_candidates'] == 1
    assert result['facts']['auto_input_causal_bounds'] == 0

    missing_observation = validate(
        [row for row in authored_records if row.get('event') != 'game_input_observed'],
        phase='accuracy', fixture_records=authored_fixture)
    assert missing_observation['verdict'] == 'incomplete'
    assert any('validation external input was not observed' in issue
               for issue in missing_observation['evidence_issues'])

    obsolete_recovery = authored_records + [{
        'event': 'validation_recovery_input', 'seq': 5,
        'at_ns': voice_ns + 170_000_000,
        'case': ACCURACY_SCENARIO[1]['id'], 'watchdog_seconds': 12.0,
    }]
    obsolete_result = validate(
        obsolete_recovery, phase='accuracy', fixture_records=authored_fixture)
    assert obsolete_result['verdict'] == 'incomplete'
    assert 'obsolete validation recovery input observed' in obsolete_result['evidence_issues']


def test_authored_game_input_activates_game_and_uses_low_level_keyboard_stimulus(monkeypatch):
    import ctypes
    from validation.real_game import gui_session

    class Function:
        def __init__(self, callback):
            self.callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.callback(*args)

    calls = []

    def foreground_pid(_hwnd, pid):
        pid._obj.value = 50
        return 1

    def send_input(count, inputs, size):
        calls.append(('send', int(count), int(size), tuple(
            (int(inputs[index].type), int(inputs[index].ki.wVk),
             int(inputs[index].ki.wScan), int(inputs[index].ki.dwFlags))
            for index in range(int(count))
        )))
        return count

    user32 = SimpleNamespace(
        SetForegroundWindow=Function(lambda hwnd: calls.append(
            ('foreground', int(hwnd.value))) or 1),
        GetForegroundWindow=Function(lambda: 77),
        GetWindowThreadProcessId=Function(foreground_pid),
        MapVirtualKeyW=Function(lambda key, _mode: 0x1C if key == 13 else 0),
        SendInput=Function(send_input),
    )
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=user32), raising=False)
    monkeypatch.setattr(gui_session.time, 'sleep', lambda _seconds: None)
    runtime = SimpleNamespace(
        target=SimpleNamespace(pid=50, hwnd=77),
        profile=SimpleNamespace(input_key=13),
    )

    SessionDriver._send_validation_game_input(runtime)

    assert calls[0] == ('foreground', 77)
    assert calls[1][0:2] == ('send', 2)
    assert calls[1][3] == (
        (1, 13, 0x1C, 0),
        (1, 13, 0x1C, 0x0002),
    )


def test_authored_game_input_uses_fixed_fixture_time_not_product_state():
    sent, traces = [], []
    runtime = SimpleNamespace(
        token=SimpleNamespace(session=1, line=8), pause_reason='irrelevant_runtime_reason', sent=True,
        target=SimpleNamespace(pid=50, hwnd=77), profile=SimpleNamespace(input_key=13))
    product = SimpleNamespace(runtime_state=lambda: runtime)
    clock = [10.0]

    with tempfile.TemporaryDirectory() as directory:
        evidence = Path(directory) / 'fixture.jsonl'
        case_id = 'room-clean-unvoiced'
        evidence.write_text(json.dumps({
            'event': 'fixture_case', 'seq': 1, 'at_ns': 1,
            'case': case_id, 'text': 'fixture', 'voice_file': None,
        }) + '\n', encoding='utf-8')
        driver = SessionDriver(
            SimpleNamespace(application=product, close=lambda: None),
            SimpleNamespace(quit=lambda: None, exit=lambda _code: None),
            {'timeout_seconds': 60, 'phase': 'accuracy', 'dialogues': 2,
             'evidence': str(evidence)},
            SimpleNamespace(emit=lambda name, **fields: traces.append((name, fields))),
            clock=lambda: clock[0], game_input_send=lambda current: sent.append(current),
        )
        driver.state = 'running'

        # The first observed fixture case establishes the authored schedule.
        driver.tick()
        assert sent == []
        delay_s = ACCURACY_GAME_INPUTS_MS[case_id] / 1000.0
        clock[0] = 10.0 + delay_s - 0.001
        driver.tick()
        assert sent == []

        # Runtime pause/sent fields are deliberately irrelevant: the stimulus is
        # fixed by the fixture case and its authored delay.
        clock[0] = 10.0 + delay_s
        driver.tick()
        assert sent == [runtime]
        assert traces == [('validation_game_input', {
            'case': case_id, 'scheduled_after_ms': ACCURACY_GAME_INPUTS_MS[case_id]})]
        runtime.pause_reason = ''
        runtime.sent = False
        clock[0] = 59.0
        driver.tick()
        assert sent == [runtime]

def test_validation_never_advances_a_stalled_case_and_outer_timeout_only_fails_collection():
    actions, inputs, traces = [], [], []
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
             'evidence': str(evidence), 'final_observation_seconds': 12.0},
            trace, clock=lambda: clock[0], game_input_send=lambda state: inputs.append(state.target.hwnd),
        )
        driver.state = 'running'
        driver.tick()  # observe case-1
        runtime.pause_reason = 'irrelevant_runtime_reason'
        runtime.sent = True
        clock[0] = 13.1
        driver.tick()
        assert inputs == []
        runtime.pause_reason = 'different_reason'
        clock[0] = 30.0
        driver.tick()
        assert inputs == []

        write(['case-1', 'case-2'])
        clock[0] = 31.0
        driver.tick()  # observe case-2
        runtime.pause_reason = ''
        runtime.sent = False
        clock[0] = 43.1
        driver.tick()
        assert inputs == []
        assert not any(name == 'validation_recovery_input' for name, _fields in traces)

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

        # A stalled run is terminated by the outer collection timeout without
        # injecting any input to make the game continue.
        write(['case-1'])
        driver.current_case = None
        driver.collection_target_seen_at = None
        clock[0] = 61.1
        driver.tick()
        assert actions[-2:] == ['close', ('exit', 3)]
        assert inputs == []


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
                    uncertain_text_fallback=True, force_advance_seconds=30,
                    input_method=SimpleNamespace(value='window_message'), input_key=13,
                    reading_unit_count=12, text_wait_ms=900))
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


def test_calibration_uses_preconfigured_fixture_hook_for_body_and_speaker():
    from autoadv.model import StructureRule, extract_fields

    rule = StructureRule(FIXTURE_SPEAKER_RULE)
    for raw, speaker, body in (
            ('明日香\nえっ嘘でしょ。\nえっ嘘でしょ。', '明日香', 'えっ嘘でしょ。'),
            ('栞……ふぅ……\n……ふぅ……', '栞', '……ふぅ……'),
            ('主人公\nこれは音声のない台詞です。\nこれは音声のない台詞です。',
             '主人公', 'これは音声のない台詞です。')):
        fields = extract_fields(raw, (rule,))
        assert (fields.speaker, fields.body) == (speaker, body)

    body_candidate = SimpleNamespace(
        identity=FIXTURE_BODY_HOOK_IDENTITY, address=10, ambiguous=False)
    speaker_candidate = SimpleNamespace(
        identity=FIXTURE_SPEAKER_HOOK_IDENTITY, address=20, ambiguous=False)
    other = SimpleNamespace(identity='other|0|0', address=20, ambiguous=False)
    assert fixture_source((other, body_candidate), FIXTURE_BODY_HOOK_IDENTITY) is body_candidate
    assert fixture_source((other,), FIXTURE_BODY_HOOK_IDENTITY) is None

    calls = []
    sources = {
        (FIXTURE_BODY_HOOK_IDENTITY, 10): SimpleNamespace(
            identity=FIXTURE_BODY_HOOK_IDENTITY, address=10),
        (FIXTURE_SPEAKER_HOOK_IDENTITY, 20): SimpleNamespace(
            identity=FIXTURE_SPEAKER_HOOK_IDENTITY, address=20),
    }
    previews = {
        (FIXTURE_BODY_HOOK_IDENTITY, 10): SimpleNamespace(
            samples=(SimpleNamespace(speaker='', body=CALIBRATION),)),
        (FIXTURE_SPEAKER_HOOK_IDENTITY, 20): SimpleNamespace(
            samples=(SimpleNamespace(speaker='明日香', body=CALIBRATION),)),
    }

    class Product:
        def capture_view(self):
            return SimpleNamespace(candidates=(body_candidate, speaker_candidate, other))
        def capture_source(self, identity, address, require_unique=True):
            calls.append(('source', identity, address, require_unique)); return sources[(identity, address)]
        def set_capture_extraction_preset(self, identity, address, preset):
            calls.append(('preset', identity, address, preset))
        def save_capture_source_rule(self, identity, address, index, rule):
            calls.append(('rule', identity, address, index, rule.pattern))
        def capture_candidate(self, identity, address):
            calls.append(('preview', identity, address)); return previews[(identity, address)]
        def set_capture_field_source(self, field, identity, address):
            calls.append(('field', field, identity, address))
        def commit_capture(self):
            return SimpleNamespace(
                body_source=FIXTURE_BODY_HOOK_IDENTITY,
                speaker_source=FIXTURE_SPEAKER_HOOK_IDENTITY)
        def runtime_state(self): return SimpleNamespace(profile=object())

    driver = SessionDriver(
        SimpleNamespace(application=Product()), SimpleNamespace(),
        {'timeout_seconds': 60, 'phase': 'accuracy'}, SimpleNamespace(emit=lambda *_a, **_k: None),
        clock=lambda: 1.0)
    assert driver._select_calibration_source()
    assert ('preset', FIXTURE_BODY_HOOK_IDENTITY, 10, 'body') in calls
    assert ('rule', FIXTURE_SPEAKER_HOOK_IDENTITY, 20, 0, FIXTURE_SPEAKER_RULE) in calls
    assert ('field', 'body', FIXTURE_BODY_HOOK_IDENTITY, 10) in calls
    assert ('field', 'speaker', FIXTURE_SPEAKER_HOOK_IDENTITY, 20) in calls


def test_accuracy_config_uses_product_force_advance_as_the_only_case_progress_deadline():
    settings = []
    profile = SimpleNamespace(
        ocr=SimpleNamespace(executable='tesseract', language='jpn', region=(0, 0, 1, 1), stable_samples=2),
        insert_pc_hooks=False, hook_flush_ms=100, hook_codepage=932,
        hook_max_buffer=1000, hook_max_history=100)

    class Product:
        catalog = SimpleNamespace(irodori_root='')
        def create_game(self, _name, _exe): return SimpleNamespace(id='g', default_profile='p')
        def select_profile(self, *_args): pass
        def select_primary_target(self, *_args): pass
        def set_profile_setting(self, key, value): settings.append((key, value))
        def current_profile(self): return profile
        def set_capture_environment(self, **_kwargs): pass
        def activate_selected_target(self): return 'connecting'

    driver = SessionDriver(
        SimpleNamespace(application=Product()), SimpleNamespace(),
        {'timeout_seconds': 60, 'phase': 'accuracy', 'executable': 'fixture.exe'},
        SimpleNamespace(emit=lambda *_a, **_k: None), clock=lambda: 1.0)
    driver._configure_product(SimpleNamespace(pid=50, hwnd=77))
    assert ('force_advance_seconds', 12) in settings
    assert any(key == 'input_method' and value.value == 'window_message'
               for key, value in settings)


def test_session_contract_is_finite():
    valid = dict(gate='session-gate', executable='validation-game.exe', phase='accuracy', timeout_seconds=120)
    assert load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(valid)})['phase'] == 'accuracy'
    assert load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(
        {**valid, 'input_method': 'send_input'})})['input_method'] == 'send_input'
    with pytest.raises(ValueError):
        load_session({'AUTOADV_VALIDATION_SESSION': json.dumps({**valid, 'timeout_seconds': 0})})
    with pytest.raises(ValueError):
        load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(
            {**valid, 'input_method': 'unknown'})})


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


def _published_payload(snapshot, name: str) -> str:
    blocks = []
    pattern = re.compile(
        r'<details(?: open)?><summary>(?P<label>[^<]+)</summary>\n\n'
        r'(?P<fence>`{3,})[^\n]*\n(?P<body>.*?)\n(?P=fence)\n</details>',
        re.S,
    )
    for match in pattern.finditer(snapshot.body):
        label = match.group('label')
        if label == name or label.startswith(name + ' · chunk '):
            blocks.append(match.group('body'))
    return ''.join(blocks)


def _strict_json(text: str):
    return json.loads(
        text,
        parse_constant=lambda value: (_ for _ in ()).throw(
            AssertionError(f'non-finite JSON token leaked: {value}')),
    )


def _write_issue_run(root: Path, *, runner_error: str | None = None, large_fixture=False):
    summary = {
        'schema': 'autoadv-local-validation-v4', 'phase_requested': 'accuracy',
        'started_utc': '2026-10-25T00:00:00Z', 'ended_utc': '2026-10-25T00:01:00Z',
        'phases': [{'phase': 'accuracy', 'report_present': True, 'runner_error': runner_error,
                    'facts': {'requested_cases': 2, 'fixture_cases': 2, 'hook_outputs': 2,
                              'utterances': 2, 'presentation_completions': 1,
                              'advance_inputs': 1, 'validation_game_inputs': 1}}],
        'launcher_error': None,
    }
    (root / 'validation-summary.json').write_text(json.dumps(summary), encoding='utf-8')

    raw = 'case2\ncase3 C:\\Users\\alice\\AutoAdv ghp_1234567890abcdefghijkl'
    trace = [
        {'event': 'trace_started', 'version': 3, 'at_ns': 1},
        {'event': 'hook_output', 'seq': 1, 'at_ns': 11, 'session': 1,
         'generation': 'g', 'connection': 1, 'hook_sequence': 11, 'received_ns': 100,
         'pid': 5, 'address': 0x1234, 'context': 1, 'context2': 2,
         'source': 'H|1|2', 'raw': raw},
        {'event': 'utterance', 'seq': 2, 'at_ns': 12, 'session': 1, 'line': 1,
         'source': 'H|1|2', 'hook_sequence': 11, 'raw': raw, 'body': raw,
         'speaker': '', 'advance_mode': 'audio', 'use_tts': False,
         'uncertain_text_fallback': True, 'reading_units': 8, 'text_wait_ms': 1400},
        {'event': 'native_audio_observation', 'seq': 3, 'at_ns': 13,
         'session': 1, 'line': 1, 'evidence_at_ms': 1000, 'target_units': 8,
         'aligned_target_units': 3, 'target_associated': True,
         'complete_alignment': False, 'committed_target_end': False,
         'causal_lower_bound_ms': 900, 'input_candidates_ms': [920],
         'target_attached_ms': 940, 'lexical_target_end_ms': None,
         'vocal_series': [{'observer': 'nonlexical', 'frame_count': 5,
                           'active_count': 4, 'peak_score': .93,
                           'active_score_threshold': .45,
                           'measurement_support_start_ms': 900,
                           'measurement_support_end_ms': 1000}],
         'recurrence_strength': .91, 'recurrence_period_ms': 640,
         'recurrence_prominence': .24},
        {'event': 'utterance_progress', 'seq': 4, 'at_ns': 14,
         'session': 1, 'line': 1, 'state': 'active', 'evidence_at_ms': 1000},
    ]
    for index in range(40):
        trace.append({
            'event': 'alignment_inference', 'seq': 10 + index, 'at_ns': 20 + index,
            'session': 1, 'line': 1, 'target_associated': True, 'target_units': 8,
            'aligned_target_units': 3, 'complete_alignment': False,
            'committed_target_end': False, 'queue_ms': float(index % 7),
            'align_ms': 10.0 + (index % 5), 'capture_start_ms': 900,
            'capture_end_ms': 1100 + index * 10, 'path_margin': 2.5,
            'boundary_margin': float('-inf'),
        })
    trace.extend([
        {'event': 'native_audio_observation', 'seq': 60, 'at_ns': 300,
         'session': 1, 'line': 1, 'evidence_at_ms': 1500,
         'target_units': 8, 'aligned_target_units': 3,
         'target_associated': True, 'complete_alignment': False,
         'committed_target_end': False, 'causal_lower_bound_ms': 900,
         'input_candidates_ms': [920], 'target_attached_ms': 940,
         'lexical_target_end_ms': None,
         'vocal_series': [{'observer': 'nonlexical', 'frame_count': 7,
                           'active_count': 5, 'peak_score': .93,
                           'active_score_threshold': .45,
                           'measurement_support_start_ms': 900,
                           'measurement_support_end_ms': 1500}],
         'recurrence_strength': .91, 'recurrence_period_ms': 640,
         'recurrence_prominence': .24},
        {'event': 'utterance_progress', 'seq': 61, 'at_ns': 301,
         'session': 1, 'line': 1, 'state': 'unknown', 'evidence_at_ms': 1500},
        {'event': 'hook_event', 'seq': 62, 'at_ns': 302,
         'hook_event': {'kind': 'diagnostic', 'host_generation': 'g',
                        'connection_generation': 1, 'sequence': 10, 'received_ns': 99,
                        'process_id': 5, 'source': None, 'text': '',
                        'detail': 'startup-only-diagnostic'},
         'outcome': {'connection_transition': None, 'capture': {'changed': False},
                     'token': None, 'flush_delay_ms': None,
                     'note': 'LunaHost: startup-only-diagnostic'}},
        {'event': 'hook_event', 'seq': 63, 'at_ns': 303,
         'hook_event': {'kind': 'output', 'host_generation': 'g',
                        'connection_generation': 1, 'sequence': 11, 'received_ns': 100,
                        'process_id': 5,
                        'source': {'process_id': 5, 'address': 0x1234, 'context': 1,
                                   'context2': 2, 'hook_code': 'H', 'hook_name': 'name'},
                        'text': raw, 'detail': ''},
         'outcome': {'connection_transition': None, 'capture': {'changed': False},
                     'token': None, 'flush_delay_ms': None, 'note': ''}},
        {'event': 'hook_event', 'seq': 64, 'at_ns': 304,
         'hook_event': {'kind': 'output', 'host_generation': 'g',
                        'connection_generation': 1, 'sequence': 12, 'received_ns': 101,
                        'process_id': 5,
                        'source': {'process_id': 5, 'address': 0x1234, 'context': 1,
                                   'context2': 2, 'hook_code': 'H', 'hook_name': 'name'},
                        'text': 'other', 'detail': ''},
         'outcome': {'connection_transition': None, 'capture': {'changed': False},
                     'token': None, 'flush_delay_ms': None, 'note': ''}},
    ])
    (root / 'autoadv-accuracy.jsonl').write_text(
        ''.join(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n'
                for item in trace), encoding='utf-8')

    fixture = [
        {'event': 'fixture_started', 'version': 2, 'at_ns': 2, 'pid': 5},
        {'event': 'fixture_case', 'seq': 1, 'at_ns': 10, 'pid': 5,
         'case': 'case2', 'text': 'case2', 'voice_file': 'jvs.wav'},
        {'event': 'fixture_voice_commanded', 'seq': 2, 'at_ns': 10, 'pid': 5,
         'case': 'case2', 'voice_file': 'jvs.wav', 'duration_ms': 1200.0},
    ]
    if large_fixture:
        fixture.append({'event': 'fixture_note', 'seq': 3, 'at_ns': 11, 'pid': 5,
                        'note': '証' * 65000})
    (root / 'autoadv-fixture-evidence.jsonl').write_text(
        ''.join(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n'
                for item in fixture), encoding='utf-8')

    report = {
        'schema': 'autoadv-offline-verification-v1', 'phase': 'accuracy',
        'verdict': 'failure_observed', 'facts': {'requested_cases': 2},
        'case_results': [
            {'case': 'case2', 'outcome': 'early_input', 'source_endpoint_ms': 1234.5},
            {'case': 'case3', 'outcome': 'advanced', 'source_endpoint_ms': 2345.5},
        ],
        'evidence_issues': [],
    }
    (root / 'autoadv-accuracy.result.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'autoadv-alignment.result.json').write_text(
        '{"unrelated_marker":true}', encoding='utf-8')
    (root / 'autoadv-debug.jsonl').write_text(
        '{"event":"unrelated_marker"}\n', encoding='utf-8')
    (root / 'validation.log').write_text(
        'runtime C:\\Users\\alice\\AutoAdv ghp_1234567890abcdefghijkl diagnostics\n',
        encoding='utf-8')

    human_vocal = {
        'schema': 'autoadv-human-vocal-observer-diagnostic-v1',
        'evidence_layer': 'isolated_fixture_observer_measurement',
        'product_acceptance': False, 'platform': 'Windows-test',
        'model': 'C:\\Users\\alice\\AutoAdv\\human-vocal.onnx',
        'threshold_grid': [0.05, 0.10],
        'files': [{'file': 'speaker.wav', 'duration_ms': 1200,
                   'observers': {'nonlexical': {'peak_score': .9,
                                               'detail_marker': 'observer-full-detail'}}}],
    }
    human_path = root / issue_reporting.HUMAN_VOCAL_REPORT
    human_path.write_text(json.dumps(human_vocal, ensure_ascii=False, indent=2), encoding='utf-8')
    sanitized_human = issue_reporting.sanitize_public_issue(human_path.read_text(encoding='utf-8'))
    human_digest = hashlib.sha256(sanitized_human.encode('utf-8')).hexdigest()
    return summary, human_digest


def test_issue_snapshot_preserves_analysis_facts_without_copying_unrelated_or_redundant_files():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / '20261025T000000Z-runnew'; root.mkdir()
        _summary, _digest = _write_issue_run(root)
        snapshot = issue_reporting.format_snapshot(root, 'a' * 40)

        # The public Issue surface keeps the evidence needed to reconstruct the
        # run's causal story, not every file that happens to share a prefix.
        for name in ('validation-summary.json', 'autoadv-accuracy.jsonl',
                     'autoadv-fixture-evidence.jsonl', 'autoadv-accuracy.result.json'):
            assert name in snapshot.body
        assert 'unrelated_marker' not in snapshot.body
        assert 'startup-only-diagnostic' not in snapshot.body
        assert 'validation.log' not in snapshot.body

        trace_text = _published_payload(snapshot, 'autoadv-accuracy.jsonl')
        trace_records = [_strict_json(line) for line in trace_text.splitlines() if line]
        serialized_trace = json.dumps(trace_records, ensure_ascii=False)
        assert 'case2\\ncase3 <USERPROFILE>\\\\AutoAdv <REDACTED-TOKEN>' in serialized_trace
        assert 'active' in serialized_trace and 'unknown' in serialized_trace
        assert '1500' in serialized_trace and '-Infinity' in serialized_trace

        fixture = _strict_json(_published_payload(snapshot, 'autoadv-fixture-evidence.jsonl'))
        serialized_fixture = json.dumps(fixture, ensure_ascii=False)
        assert all(value in serialized_fixture for value in ('case2', 'jvs.wav', '1200.0'))

        report = _strict_json(_published_payload(snapshot, 'autoadv-accuracy.result.json'))
        serialized_report = json.dumps(report, ensure_ascii=False)
        assert all(value in serialized_report for value in ('early_input', '1234.5', 'advanced', '2345.5'))

        summary = _strict_json(_published_payload(snapshot, 'validation-summary.json'))
        serialized_summary = json.dumps(summary, ensure_ascii=False)
        # Accuracy facts have one owner on the Issue surface; summary may point to
        # them but must not duplicate the table already carried by the verifier.
        assert '"requested_cases"' not in serialized_summary
        assert snapshot.body.count('"requested_cases"') == 1


def test_issue_diagnostics_are_conditional_and_digest_scoped():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / '20261025T000000Z-runnew'; root.mkdir()
        summary, human_digest = _write_issue_run(root)

        first = issue_reporting.format_snapshot(root, 'a' * 40)
        assert human_digest in first.body
        assert 'observer-full-detail' in first.body

        repeated = issue_reporting.format_snapshot(
            root, 'a' * 40, known_human_vocal_digests={human_digest})
        assert human_digest in repeated.body
        assert 'observer-full-detail' not in repeated.body

        failed = dict(summary)
        failed['phases'] = [{**summary['phases'][0], 'runner_error': 'runner failed'}]
        (root / 'validation-summary.json').write_text(json.dumps(failed), encoding='utf-8')
        failure = issue_reporting.format_snapshot(
            root, 'a' * 40, known_human_vocal_digests={human_digest})
        assert 'validation.log' in failure.body
        assert '<USERPROFILE>\\AutoAdv <REDACTED-TOKEN> diagnostics' in failure.body


def test_issue_upload_plan_bounds_transport_without_dropping_the_logical_run():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / '20261025T000000Z-runnew'; root.mkdir()
        _summary, human_digest = _write_issue_run(root, large_fixture=True)
        snapshot = issue_reporting.format_snapshot(
            root, 'a' * 40, known_human_vocal_digests={human_digest})
        assert len(snapshot.parts) > 1

        comments = _comments()
        comments[0]['body'] += (
            f'\n| `{issue_reporting.HUMAN_VOCAL_REPORT}` | 239258 | `{human_digest}` |')
        plan = issue_reporting.build_upload_plan(root, 'a' * 40, comments)
        operations = [op for op in plan['operations']
                      if op['role'] in {'history', 'continuation'}]
        assert operations
        assert all(len(op['body']) <= issue_reporting.COMMENT_BODY_TARGET_CHARS
                   for op in operations)
        assert all(len(op['body'].encode('utf-8')) <= issue_reporting.COMMENT_BODY_TARGET_BYTES
                   for op in operations)
        assert all('observer-full-detail' not in op['body'] for op in operations)

        # Physical comment partitioning must not alter the logical evidence body.
        published = '\n\n'.join(part for part in snapshot.parts)
        assert '証' * 1000 in published
        assert 'case2' in published and '1234.5' in published


def test_accuracy_assets_and_alignment_benchmark_verify_inputs_without_becoming_acceptance_proxy():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        payload = b'fixture-audio'
        manifest = root / 'manifest.json'
        manifest.write_text(json.dumps({
            'schema': 'autoadv-advancement-accuracy-assets-v1',
            'assets': [{
                'id': 'sample', 'kind': 'voice_recording', 'filename': 'sample.wav',
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
        VocalActivityFrame(at_ms=100, score=0.08, support_start_ms=0, observer="nonlexical"),
        VocalActivityFrame(at_ms=340, score=0.22, support_start_ms=0, observer="nonlexical"),
        VocalActivityFrame(at_ms=580, score=0.51, support_start_ms=0, observer="nonlexical"),
        VocalActivityFrame(at_ms=116, score=0.72, support_start_ms=100, observer="speech"),
    )
    measured = summarize_vocal_frames(frames)
    assert set(measured) == {"nonlexical", "speech"}
    assert measured["nonlexical"]["frame_count"] == 3
    assert measured["nonlexical"]["peak_score"] == 0.51
    assert measured["nonlexical"]["thresholds"]["0.20"]["active_count"] == 2
    assert measured["speech"]["thresholds"]["0.60"]["active_count"] == 1
    assert "verdict" not in measured
