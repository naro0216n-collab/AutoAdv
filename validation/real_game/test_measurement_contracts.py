"""Contracts of the real-game measurement system; not substitutes for real acceptance."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import wave
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import numpy as np
import pytest

from autoadv.g2p import PronunciationCandidate
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.application import CaptureHostEventOutcome, HookEventOutcome
from autoadv.conversation import Token
from validation.real_game import issue_reporting, prepare_accuracy_assets as media
from validation.real_game._synthetic_checker_oracle import synthetic_game_observations
from validation.real_game.benchmark_alignment import contract_failures, run as run_alignment_benchmark
from validation.real_game.gui_session import CALIBRATION, SessionDriver, calibration_candidates, choose_calibration_source, load_session
from validation.real_game.renpy.prepare_derived_audio import build as build_derived_audio
from validation.real_game.trace import ProductTraceObserver, ValidationTrace, text_digest
from validation.real_game.verify_trace import scenario_for_phase, validate as _validate

HERE = Path(__file__).parent
SCENARIO = json.loads((HERE / 'SCENARIO.json').read_text(encoding='utf-8'))['lines']
ACCURACY_SCENARIO = scenario_for_phase('accuracy')


def contract_records(first: int, last: int, *, mode=None, advance=True, lines=None):
    lines = SCENARIO if lines is None else lines
    records = [{'event': 'trace_started', 'at_ns': 1, 'version': 2}]

    def add(event, **extra):
        at = len(records) * 1_000_000_000
        if event == 'hook_output':
            extra['received_ns'] = at - 1
        records.append({'event': event, 'at_ns': at, 'seq': len(records), **extra})

    add('hook_connected', pid=50, generation='host-a', connection=1)
    add('monitor_started', session=1, source='hook', game_id='g', profile_id='p',
        hook_pid=50, audio_pid=50, input_pid=50, generation='host-a', connection=1)
    add('native_ready', session=1, audio_pid=50)
    for i in range(first, last):
        line = lines[i]
        key = {'session': 1, 'line': i - first + 1}
        chosen = mode or ('tts' if line['use_tts'] else ('progress' if line['advance_mode'] == 'audio' else 'reading'))
        raw = text_digest('明日香「' + line['text'] + '」')
        add('hook_output', session=1, pid=50, generation='host-a', connection=1,
            source='HOOK|1|2', address=902, hook_sequence=i + 1, context=1, context2=2, raw_hash=raw)
        add('utterance', **key, source='HOOK|1|2', hook_sequence=i + 1,
            raw_hash=raw, body_hash=text_digest(line['text']),
            advance_mode='text' if chosen == 'reading' else 'audio', use_tts=chosen == 'tts',
            text_wait_ms=500, reading_units=5, uncertain_text_fallback=False, speaker='明日香')
        if chosen == 'progress':
            add('native_pcm', **key, frames=960, audio_pid=50)
            add('native_audio_observation', **key, kind='acoustic', target_units=5,
                aligned_target_units=5, target_associated=True, complete_alignment=True,
                committed_target_end=True, vocal_first_active_ms=1000, vocal_last_active_ms=1400,
                vocal_mean_probability=.8, vocal_active_fraction=.7,
                recurrence_strength=.1, recurrence_stability=.1, cross_epoch_persistence=0.0)
            add('alignment_inference', **key, audio_ms=500, align_ms=50, queue_ms=5)
            add('utterance_progress', **key, state='finite_vocal_active', ended_at_ms=None,
                lexical_target_associated=True, lexical_target_ended=True,
                finite_vocal_observed=True, persistent_vocal_observed=False,
                boundary_certainty='autoadv')
            add('utterance_progress', **key, state='ended', ended_at_ms=1400,
                lexical_target_associated=True, lexical_target_ended=True,
                finite_vocal_observed=True, persistent_vocal_observed=False,
                boundary_certainty='autoadv')
            observed = len(records) * 1_000_000_000
            add('utterance_progress_end_estimate', **key,
                estimated_at_ns=observed - 300_000_000, evidence='utterance_progress')
        elif chosen == 'tts':
            add('tts_result', **key, success=True)
        add('presentation_complete', **key, success=True,
            reason='tts' if chosen == 'tts' else ('utterance_progress' if chosen == 'progress' else 'text'))
        if advance and i + 1 < last and not line.get('manual_handoff'):
            add('advance_attempt', **key)
            add('advance_input', **key)
    return records



def resequence(records):
    for i, record in enumerate(records):
        record['at_ns'] = 1 if i == 0 else i * 1_000_000_000
        if i:
            record['seq'] = i
        if record['event'] == 'hook_output':
            record['received_ns'] = record['at_ns'] - 1
        elif record['event'] == 'utterance_progress_end_estimate':
            record['estimated_at_ns'] = record['at_ns'] - 300_000_000
    return records

def validate(records, *, phase='mixed', **kwargs):
    if phase == 'accuracy' and 'start_line' not in kwargs:
        kwargs['start_line'] = 1
    return _validate(
        records, phase=phase,
        oracle_records=synthetic_game_observations(records, phase=phase, start_line=kwargs.get('start_line')),
        **kwargs,
    )


def accuracy_pos(case_id):
    return next(i for i, line in enumerate(ACCURACY_SCENARIO) if line['id'] == case_id)


def test_accuracy_fixture_and_manifest_form_one_continuous_sequence_with_deterministic_derived_audio():
    script = (HERE / 'renpy' / 'game' / 'script.rpy').read_text(encoding='utf-8')
    block = script.split('label advancement_accuracy:', 1)[1].split('\nlabel standard_tts:', 1)[0]
    fixture_texts = re.findall(r'^\s+(?:a|s|p|b|c) "(.*)"\s*$', block, re.M)
    assert fixture_texts == [line['text'] for line in ACCURACY_SCENARIO]

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


def test_accuracy_measurement_scope_is_one_continuous_realistic_sequence_not_case_restarts():
    scenario = ACCURACY_SCENARIO
    assert len(scenario) >= 30
    assert (scenario[0]['id'], scenario[-1]['id']) == ('room-clean-short', 'quiet-recovery')
    assert sum(bool(line['foreground_voice']) for line in scenario) > sum(not bool(line['foreground_voice']) for line in scenario)
    assert all(line['advance_mode'] == 'audio' and not line['use_tts'] for line in scenario)
    channels = {channel for line in scenario for channel in line['expected_mix_channels']}
    assert {'adv_background', 'adv_sfx'}.issubset(channels)
    assert any(line['speaker_family'] == 'nonlexical' for line in scenario)
    assert {'mid_voice', 'most_voice_elapsed', 'after_foreground_end', 'nonverbal_mid_voice'} <= {line.get('hook_delay_class') for line in scenario}
    pair = [line for line in scenario if line.get('duration_pair') == 'same-text-001']
    assert {line.get('duration_variant') for line in pair} == {'short', 'long_nonverbal_tail'}
    assert {'loop_started_current_epoch', 'persistent_cross_epoch', 'finite_recurrent'} <= {line.get('vocal_source_class') for line in scenario}
    assert {'panting', 'breath'} <= {line.get('vocal_class') for line in scenario}


def test_verifier_separates_collection_from_quality_and_progress_from_fallback():
    records = contract_records(0, 3, lines=ACCURACY_SCENARIO)
    result = validate(records, phase='accuracy')
    assert result['collection_valid'] and result['measurement_valid'] and result['ok']
    assert result['quality']['foreground_progress_completion_rate'] is not None
    full = validate(contract_records(0, len(ACCURACY_SCENARIO), lines=ACCURACY_SCENARIO), phase='accuracy')
    assert {'mid_voice', 'most_voice_elapsed', 'after_foreground_end', 'nonverbal_mid_voice'} <= set(full['category_quality']['hook_delay'])
    assert 'same-text-001' in full['category_quality']['duration_pairs']
    assert 'finite_recurrent' in full['category_quality']['vocal_source']
    assert full['quality']['nonverbal_cases'] > 0
    assert full['quality']['finite_vocal_loop_control_cases'] > 0

    # Unvoiced lines may advance by explicit fallback, but audio-derived progress completion on them is misattribution.
    idx = accuracy_pos('room-clean-unvoiced')
    fallback = contract_records(idx, idx + 2, lines=ACCURACY_SCENARIO)
    rewritten = []
    for record in fallback:
        record = dict(record)
        if record.get('line') == 1:
            if record['event'] == 'utterance':
                record['uncertain_text_fallback'] = True
            if record['event'] == 'utterance_progress_end_estimate':
                continue
            if record['event'] == 'utterance_progress' and record.get('state') == 'ended':
                continue
            if record['event'] == 'presentation_complete':
                record['reason'] = 'uncertain_text_fallback'
        rewritten.append(record)
    fallback_result = validate(resequence(rewritten), phase='accuracy', start_line=idx + 1)
    assert fallback_result['collection_valid']
    assert fallback_result['line_results'][0]['completion_reason'] == 'uncertain_text_fallback'
    assert fallback_result['quality']['misattribution_rate'] == 0.0
    assert fallback_result['quality']['fallback_early_input_rate'] == 0.0

    progress_result = validate(fallback, phase='accuracy', start_line=idx + 1)
    assert progress_result['quality']['misattribution_rate'] > 0.0


def test_independent_source_endpoint_is_safety_truth_and_channel_inactive_cannot_hide_early_input():
    product = contract_records(0, 3, lines=ACCURACY_SCENARIO)
    game = synthetic_game_observations(product, phase='accuracy', start_line=1)
    input_at = next(x['at_ns'] for x in product if x['event'] == 'advance_input' and x['line'] == 1)
    onset = next(x for x in game if x['event'] == 'voice_started' and x['line'] == 1)
    onset['source_acoustic_end_at_ns'] = input_at + 5_000_000
    onset['source_file_end_at_ns'] = input_at + 10_000_000
    onset['source_acoustic_uncertainty_ms'] = 0.0
    inactive = next(x for x in game if x['event'] == 'voice_channel_inactive' and x['line'] == 1)
    inactive['at_ns'] = input_at + 1_000_000
    inactive['last_channel_active_at_ns'] = input_at
    game.sort(key=lambda x: x['at_ns'])
    result = _validate(product, phase='accuracy', start_line=1, oracle_records=game)
    assert not result['ok']
    assert result['game_evidence']['early_inputs'] == 1
    assert result['performance']['source_acoustic_end_to_input_ms']['p50'] < 0
    assert result['quality']['progress_early_input_rate'] > 0.0

    indeterminate = [dict(x) for x in game]
    onset = next(x for x in indeterminate if x['event'] == 'voice_started' and x['line'] == 1)
    onset['source_acoustic_end_at_ns'] = input_at + 5_000_000
    onset['source_acoustic_uncertainty_ms'] = 10.0
    uncertain = _validate(product, phase='accuracy', start_line=1, oracle_records=indeterminate)
    assert uncertain['game_evidence']['indeterminate_source_acoustic_end'] == 1
    assert uncertain['game_evidence']['early_inputs'] == 0
    assert not uncertain['voice_safety_verified']

    missing = [dict(x) for x in game]
    onset = next(x for x in missing if x['event'] == 'voice_started' and x['line'] == 1)
    onset.pop('source_acoustic_end_at_ns', None)
    onset.pop('source_acoustic_uncertainty_ms', None)
    unverified = _validate(product, phase='accuracy', start_line=1, oracle_records=missing)
    assert unverified['collection_valid'] and not unverified['voice_safety_verified']

    wrong_pid = [dict(x, pid=999) for x in game]
    provenance = _validate(product, phase='accuracy', start_line=1, oracle_records=wrong_pid)
    assert not provenance['collection_valid']
    assert any('PID does not match' in error for error in provenance['errors'])


def test_validation_recovery_continues_collection_without_becoming_product_success():
    actions, recoveries, traces = [], [], []
    clock = [1.0]
    runtime = SimpleNamespace(token=SimpleNamespace(session=1, line=1),
                              pause_reason='target_unavailable', sent=False,
                              target=SimpleNamespace(pid=50, hwnd=77),
                              profile=SimpleNamespace(input_key=13))
    product = SimpleNamespace(runtime_state=lambda: runtime)
    window = SimpleNamespace(application=product, close=lambda: actions.append('close'))
    trace = SimpleNamespace(emit=lambda name, **fields: traces.append((name, fields)))
    driver = SessionDriver(
        window, SimpleNamespace(quit=lambda: actions.append('quit')),
        {'timeout_seconds': 30, 'phase': 'accuracy', 'dialogues': 3, 'continue_collection': True},
        trace, clock=lambda: clock[0], recovery_send=lambda state: recoveries.append(state.token.line),
    )
    driver.state = 'running'
    driver.tick()
    assert recoveries == []
    clock[0] = 1.3
    driver.tick()
    assert recoveries == [1]
    assert traces[-1][0] == 'validation_recovery_input'
    assert actions == []
    runtime.token = SimpleNamespace(session=1, line=2)
    clock[0] = 2.0
    driver.tick()
    assert actions == []


def test_trace_records_product_selected_provenance_and_keeps_observation_separate_from_knowledge():
    class Application:
        def configure_telemetry(self, observer): self.observer = observer
        def runtime_state(self): return SimpleNamespace(token=None, utterance=None)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'run.jsonl'
        app = Application()
        observer = ProductTraceObserver(ValidationTrace(path), app)
        observer.install()
        token = Token(4, 2)
        app.observer('native_audio_observation', token, {'kind': 'alignment', 'matched_phonemes': 3})
        app.observer('utterance_progress', token, {
            'state': 'finite_vocal_active', 'ended_at_ms': None,
            'lexical_target_associated': True, 'lexical_target_ended': False,
            'finite_vocal_observed': True, 'persistent_vocal_observed': False,
            'boundary_certainty': 'autoadv'})
        source = ThreadSource(50, 0x100, 1, 2, 'T', 'T')
        output = HookEvent('output', 'host-a', 7, 3, 30, 50, source, '選択台詞')
        app.observer('selected_hook_output', None, {
            'session': 2, 'generation': 'host-a', 'connection': 7, 'hook_sequence': 3,
            'source': source.identity, 'received_ns': 30, 'pid': 50, 'address': source.address,
            'context': source.context, 'context2': source.context2, 'raw': output.text})
        app.observer('hook_event', None, {
            'hook_event': output, 'outcome': HookEventOutcome(capture=CaptureHostEventOutcome(changed=True))})
        records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        raw = next(r for r in records if r['event'] == 'native_audio_observation')
        progress = next(r for r in records if r['event'] == 'utterance_progress')
        assert 'state' not in raw and 'matched_phonemes' not in progress
        assert len([r for r in records if r['event'] == 'hook_output']) == 1
        assert len([r for r in records if r['event'] == 'hook_candidate_output']) == 1
        assert 'raw' not in next(r for r in records if r['event'] == 'hook_output')

    assert not _validate([])['ok']
    assert 'no live dialogue observed' in _validate([{'event': 'trace_started', 'at_ns': 1}])['errors']


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


def _phase(name: str, *, eligible_accuracy: bool = False) -> dict:
    return {
        'phase': name, 'collection_valid': True, 'measurement_valid': True,
        'collection_complete': True, 'advancement_observed': True,
        'automatic_continuation_verified': True, 'validation_recovery_inputs': 0,
        'voice_safety_verified': True if eligible_accuracy else None,
        'quality': {
            'requested_cases': len(ACCURACY_SCENARIO) if name == 'accuracy' else 3,
            'observed_cases': len(ACCURACY_SCENARIO) if name == 'accuracy' else 3,
            'automatic_advance_rate': 1.0, 'safe_automatic_advance_rate': 1.0,
            'process_audio_observation_rate': 1.0 if name == 'accuracy' else None,
            'alignment_observation_rate': 1.0 if name == 'accuracy' else None,
            'mix_environment_observation_rate': 1.0 if name == 'accuracy' else None,
            'foreground_progress_completion_rate': 1.0 if name == 'accuracy' else None,
            'progress_early_input_rate': 0.0 if name == 'accuracy' else None,
            'fallback_early_input_rate': 0.0 if name == 'accuracy' else None,
            'recovery_rate': 0.0, 'early_input_rate': 0.0 if name == 'accuracy' else None,
            'overhold_rate': 0.0 if name == 'accuracy' else None,
            'misattribution_rate': 0.0 if name == 'accuracy' else None,
        },
        'quality_issues': [], 'report_present': True, 'runner_error': None,
    }


def _write_summary(root: Path, *, eligible=False):
    payload = {
        'schema': 'autoadv-local-validation-v3', 'repository': 'naro0216n-collab/AutoAdv',
        'branch': 'develop', 'commit': 'a' * 40, 'started_utc': '2026-10-23T13:41:56Z',
        'ended_utc': '2026-10-23T13:45:00Z', 'phase_requested': 'accuracy',
        'phases': [_phase('accuracy', eligible_accuracy=eligible)], 'launcher_error': None,
    }
    (root / 'validation-summary.json').write_text(json.dumps(payload), encoding='utf-8')


def _comments():
    runs = {1: '20261023T130049Z-36c5ea8d', 2: '20261023T102922Z-d01111ed', 3: '20261023T095133Z-4fdc7015', 4: '20261023T014504Z-0b2c7d31'}
    result = [{'id': 1000 + slot, 'body': f'{issue_reporting.history_marker(slot)}\n<!-- autoadv-validation-run:{run} -->\nold'}
              for slot, run in runs.items()]
    result.append({'id': 2000, 'body': f'{issue_reporting.BASELINE_MARKER}\nold baseline'})
    return result


def test_issue_reporting_is_reconstructable_fixed_surface_and_baseline_is_independent():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / 'run-new'
        root.mkdir()
        _write_summary(root, eligible=True)
        trace = b'{"event":"decision"}\n'
        (root / 'autoadv-accuracy.jsonl').write_bytes(trace)
        (root / 'autoadv-accuracy.result.json').write_text(json.dumps({'line_results': [{
            'scenario': 'case-a', 'scene': 'room', 'speaker_family': 'local',
            'audio_environment': 'clean', 'mix_channels_observed': [], 'outcome': 'next_hook_observed',
            'completion_reason': 'utterance_progress', 'pcm_observed': True, 'alignment_inference_count': 1,
            'game_outcome': 'source_acoustic_end_then_input', 'source_acoustic_end_to_input_ms': 123.5,
        }], 'performance': {}}), encoding='utf-8')
        (root / 'validation.log').write_text('C:\\Users\\private\\x token github_pat_1234567890abcdef\n', encoding='utf-8')
        snapshot = issue_reporting.format_snapshot(root, 'a' * 40)
        assert 'progress early' in snapshot.body and 'fallback fg' in snapshot.body and 'fallback early' in snapshot.body
        assert 'source end→input ms' in snapshot.body and '123.5' in snapshot.body
        match = re.search(re.escape(issue_reporting.EVIDENCE_MARKER) + r'.*?```text\n(?P<data>[A-Za-z0-9+/=]+)\n```', snapshot.body, re.S)
        assert match
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(match.group('data')))) as archive:
            assert archive.read('autoadv-accuracy.jsonl') == trace
            log = archive.read('validation.log').decode()
            assert '<USERPROFILE>\\x' in log and '<REDACTED-TOKEN>' in log

        plan = issue_reporting.build_upload_plan(root, 'a' * 40, _comments())
        assert [op['role'] for op in plan['operations']] == ['history', 'baseline']
        assert plan['operations'][0]['slot'] == 4

        duplicated = _comments() + [{'id': 9999, 'body': f'{issue_reporting.history_marker(2)}\nduplicate'}]
        with pytest.raises(ValueError):
            issue_reporting.choose_history_slot(duplicated, root.name)


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
        report = run_alignment_benchmark(model, vocab, conditions=('clean',), rounds=1, seed=10,
                                         model_backend=StaticModel(), g2p=StaticG2P())
    assert report['schema'] == 'autoadv-alignment-benchmark-v3'
    assert 'cer' not in report['conditions']['clean'] and not report['real_game_verified']

    unsafe = {'conditions': {
        'clean': {'runs': 1, 'associated': 1, 'committed': 1, 'early_commits': 1, 'early_decisions': 0},
        'bgm': {'runs': 1, 'associated': 1, 'committed': 0, 'early_commits': 0, 'early_decisions': 0},
        'truncated': {'runs': 1, 'committed': 1},
    }}
    failures = contract_failures(unsafe)
    assert any('early committed' in x for x in failures)
    assert any('bgm target end' in x for x in failures)
    assert any('missing target suffix' in x for x in failures)
