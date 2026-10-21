"""Unit tests for the evidence verifier; constructed records are NEVER E2E results."""
from __future__ import annotations
import json
from pathlib import Path
import unittest
from autoadv.validation_trace import text_digest
from validation.real_game.verify_trace import validate as _validate, scenario_for_phase
from validation.real_game._synthetic_checker_oracle import synthetic_game_observations


def validate(records, *, phase='mixed', **kwargs):
    # Both inputs are deliberately synthetic and used ONLY to unit-test the
    # verifier's error handling. Not an AutoAdv/Windows acceptance result.
    return _validate(records, phase=phase,
                     oracle_records=synthetic_game_observations(records, phase=phase),
                     **kwargs)


SCENARIO = json.loads((Path(__file__).parent / 'SCENARIO.json').read_text(encoding='utf-8'))['lines']
MIXED_SCENARIO = scenario_for_phase('mixed')


def contract_records(first: int, last: int, *, mode=None, advance=True, lines=None):
    """Manufacture checker-only records, not an AutoAdv/game execution."""
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
    add('permit_requested', session=1)
    add('native_ready', session=1, vad_only=False, audio_pid=50)
    for i in range(first, last):
        line = lines[i]
        key = {'session': 1, 'line': i - first + 1}
        chosen = mode or ('tts' if line['use_tts'] else ('native' if line['advance_mode']=='audio' else 'reading'))
        raw = text_digest('明日香「' + line['text'] + '」')
        add('hook_output', session=1, pid=50, generation='host-a', connection=1,
            source='HOOK|1|2', address=902, hook_sequence=i+1, context=1, context2=2, raw_hash=raw)
        add('utterance', **key, source='HOOK|1|2', hook_sequence=i+1,
            raw_hash=raw, body_hash=text_digest(line['text']), advance_mode='text' if chosen=='reading' else 'audio',
            use_tts=chosen=='tts', text_wait_ms=500, characters=5, uncertain_text_fallback=False, speaker='明日香')
        if chosen == 'native':
            add('native_pcm', **key, frames=960, audio_pid=50)
            add('native_event', **key, status='started', method='asr')
            observed_time = len(records) * 1_000_000_000
            add('native_voice_end_estimate', **key, estimated_at_ns=observed_time - 300_000_000,
                onset_estimated_at_ns=observed_time - 1_000_000_000, evidence='asr_vad')
            add('native_event', **key, status='completed', method='asr')
        elif chosen == 'tts':
            add('tts_result', **key, success=True)
        add('presentation_complete', **key, success=True, reason='tts' if chosen=='tts' else ('native' if chosen=='native' else 'text'))
        if advance and i+1 < last and not line.get('manual_handoff'):
            add('advance_attempt', **key)
            add('advance_input', **key)
    return records


class RealGameVerifierContract(unittest.TestCase):
    def test_hook_phase_accepts_real_source_provenance_without_auto_input(self):
        # Hook acquisition by itself needs neither ASR, Irodori nor SendInput.
        records = contract_records(0, 2, advance=False)
        self.assertTrue(validate(records, phase='hook')['ok'])

    def test_only_native_voice_judgement_requires_game_playback_oracle(self):
        # Checker-only records: no claim that a Ren'Py process ran. A selected
        # HOOK and next-HOOK after a reading/TTS input already exist in the
        # product trace; requiring game voice-end is unrelated to those paths.
        for phase, records in (
            ('hook', contract_records(0, 2, advance=False)),
            ('reading', contract_records(0, 3, mode='reading')),
            ('tts', contract_records(3, 6)),
        ):
            with self.subTest(phase=phase):
                result = _validate(records, phase=phase)
                self.assertTrue(result['ok'], result['errors'])
                self.assertIsNone(result['game_evidence'])
                self.assertEqual(result['game_observer'], 'optional')
        native = _validate(contract_records(9, 12), phase='native')
        self.assertFalse(native['ok'])
        self.assertEqual(native['game_observer'], 'required')

    def test_incidental_native_input_cannot_be_accepted_without_voice_evidence(self):
        # Running a session in 'reading' mode must not mislabel any *native*
        # auto-input in the trace as safe, merely because phase is named reading.
        records = contract_records(0, 3, mode='reading')
        native_line = next(r for r in records if r.get('event') == 'utterance' and r['line'] == 1)
        native_line['advance_mode'] = 'audio'
        native_line['use_tts'] = False
        for r in records:
            if r.get('line') == 1 and r.get('event') == 'presentation_complete':
                r['reason'] = 'native'
        # Product trace itself must still satisfy the native completion contract.
        first_input = next(i for i, r in enumerate(records) if r.get('event') == 'advance_attempt')
        completion = dict(records[first_input-1], event='native_event', status='completed', method='asr')
        # Do not manufacture valid native capture: either failure is conclusive.
        records.insert(first_input, completion)
        for i, r in enumerate(records):
            if i:
                r['seq'] = i
        result = _validate(records, phase='reading')
        self.assertFalse(result['ok'])
        self.assertEqual(result['game_observer'], 'required')

    def test_each_line_is_reported_without_hiding_unadvanced_dialogue(self):
        result = _validate(contract_records(0, 3, mode='reading'), phase='reading')
        self.assertEqual([line['outcome'] for line in result['line_results']],
                         ['next_hook_observed', 'next_hook_observed', 'no_autoadv_input'])
        self.assertEqual(result['confirmed_advance_to_next_hook'], 2)

    def test_nonadvancement_reports_controller_handoff_and_fallback_setting(self):
        records = contract_records(9, 12)
        target = next(r for r in records if r.get('event') == 'utterance' and r['line'] == 3)
        target['uncertain_text_fallback'] = False
        records.append({'event': 'controller_handoff', 'seq': len(records),
                        'at_ns': records[-1]['at_ns'] + 1,
                        'session': 1, 'line': 3,
                        'reason': 'native_inconclusive_manual_handoff'})
        result = _validate(records, phase='native', start_line=10)
        self.assertEqual(result['line_results'][2]['stop_reason'], 'native_inconclusive_manual_handoff')
        self.assertIs(result['line_results'][2]['fallback_enabled'], False)
        self.assertEqual(result['line_results'][2]['outcome'], 'no_autoadv_input')

    def test_json_report_preserves_success_and_failures(self):
        import contextlib
        from io import StringIO
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from validation.real_game.verify_trace import main
        with TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / 'session.jsonl'
            report = root / 'evaluation.json'
            records = contract_records(0, 3, mode='reading')
            trace.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in records), encoding='utf-8')
            with patch('sys.argv', ['verify_trace', '--phase', 'reading', '--trace',
                                    str(trace), '--report', str(report)]):
                with contextlib.redirect_stdout(StringIO()):
                    self.assertEqual(main(), 0)
            saved = json.loads(report.read_text(encoding='utf-8'))
            self.assertTrue(saved['ok'])
            self.assertEqual(len(saved['line_results']), 3)
            bad = [dict(r) for r in records]
            next_hook = next(r for r in bad if r.get('event') == 'hook_output' and
                             r.get('hook_sequence') == 2)
            next_hook['received_ns'] = 1
            bad_trace = root / 'bad.jsonl'
            bad_trace.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in bad), encoding='utf-8')
            bad_report = root / 'bad-evaluation.json'
            with patch('sys.argv', ['verify_trace', '--phase', 'reading', '--trace',
                                    str(bad_trace), '--report', str(bad_report)]):
                with contextlib.redirect_stdout(StringIO()):
                    self.assertEqual(main(), 1)
            failed = json.loads(bad_report.read_text(encoding='utf-8'))
            self.assertFalse(failed['ok'])
            self.assertTrue(failed['errors'])

    def test_reading_advance_must_be_followed_by_next_live_hook(self):
        records = contract_records(0, 3, mode='reading')
        result = validate(records, phase='reading')
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['confirmed_advance_to_next_hook'], 2)
        # One successful SendInput without a subsequent dialogue is not E2E success.
        truncated = records[:-4]
        observation = validate(truncated, phase='reading')
        self.assertTrue(observation['collection_valid'], observation['errors'])
        self.assertFalse(observation['advancement_observed'] and
                         observation['confirmed_advance_to_next_hook'] >= 2)
        self.assertTrue(observation['diagnostics'])

    def test_windows_clock_can_stamp_attempt_and_sent_in_same_tick(self):
        records = contract_records(0, 3, mode='reading')
        attempts = {(r['session'], r['line']): r['at_ns'] for r in records
                    if r['event'] == 'advance_attempt'}
        for record in records:
            if record['event'] == 'advance_input':
                record['at_ns'] = attempts[(record['session'], record['line'])]
        result = validate(records, phase='reading')
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertEqual(result['confirmed_advance_to_next_hook'], 2)

    def test_observation_without_asr_success_remains_a_valid_measurement(self):
        records = contract_records(9, 10, advance=False)
        result = validate(records, phase='native')
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertEqual(result['confirmed_native_asr'], 0)
        self.assertFalse(result['advancement_observed'])
        self.assertTrue(any('limited ASR-confirmed' in finding
                            for finding in result['diagnostics']))

    def test_native_requires_pcm_and_real_asr_not_only_adapter_ready_or_vad(self):
        records = contract_records(9, 12)
        self.assertTrue(validate(records, phase='native')['ok'])
        without_pcm = [r for r in records if not (r['event'] == 'native_pcm' and r['line'] == 1)]
        self.assertFalse(validate(without_pcm, phase='native')['ok'])
        vad = [dict(r, method='vad') if r['event'] == 'native_event' else r for r in records]
        self.assertFalse(validate(vad, phase='native')['ok'])
        missing_capture = [r for r in records if r['event'] != 'native_pcm']
        self.assertFalse(validate(missing_capture, phase='native')['ok'])
        wrong_audio_process = [dict(r, audio_pid=51) if r['event'] == 'native_pcm' else r for r in records]
        self.assertFalse(validate(wrong_audio_process, phase='native')['ok'])

    def test_pause_without_reauthorization_cannot_send_input(self):
        records = contract_records(0, 3, mode='reading')
        point = next(i for i, r in enumerate(records) if r['event'] == 'advance_attempt')
        records.insert(point, {'event': 'pause_requested', 'session': 1})
        for i, r in enumerate(records):
            r['at_ns'] = i * 1_000_000_000 + 1
            if r['event'] == 'hook_output':
                r['received_ns'] = r['at_ns'] - 1
            if i:
                r['seq'] = i
        self.assertFalse(validate(records, phase='reading')['ok'])

    def test_tts_requires_drain_and_next_hook(self):
        records = contract_records(3, 6)
        self.assertTrue(validate(records, phase='tts')['ok'])
        broken = [dict(r, success=False) if r['event'] == 'tts_result' and r['line'] == 1 else r for r in records]
        self.assertFalse(validate(broken, phase='tts')['ok'])

    def test_repeated_lines_are_distinct_only_with_separate_input_and_hook(self):
        records = contract_records(17, 20)
        self.assertTrue(validate(records, phase='hook', start_line=18)['ok'])
        # A single occurrence cannot resolve which of the two identical lines it is.
        self.assertFalse(validate(contract_records(17, 18, advance=False), phase='hook')['ok'])
        duplicates = [dict(r) for r in records]
        key = next(r for r in records if r['event'] == 'advance_input')
        duplicates.insert(-1, dict(key, seq=len(duplicates), at_ns=900000))
        # A duplicate input with inconsistent monotonic sequence is also rejected.
        self.assertFalse(validate(duplicates, phase='hook', start_line=18)['ok'])

    def test_cached_next_output_cannot_masquerade_as_input_success(self):
        records = contract_records(0, 3, mode='reading')
        first_send = next(r['at_ns'] for r in records if r['event'] == 'advance_attempt')
        wrong = [dict(r, received_ns=first_send - 1)
                 if r['event'] == 'hook_output' and r['hook_sequence'] == 2 else r
                 for r in records]
        self.assertFalse(validate(wrong, phase='reading')['ok'])

        # The game may react between key-down and SendInput's return.
        attempt = first_send
        positive = [dict(r, received_ns=attempt + 1)
                    if r['event'] == 'hook_output' and r['hook_sequence'] == 2 else r
                    for r in records]
        self.assertTrue(validate(positive, phase='reading')['ok'])

    def test_performance_numbers_are_observations_not_extra_acceptance_thresholds(self):
        records = contract_records(9, 12)
        from validation.real_game.verify_trace import _summary
        self.assertEqual(_summary([100, 200, 500])['p95'], 500)
        self.assertTrue(validate(records, phase='native')['ok'])
        metrics = validate(records, phase='native')['performance']
        self.assertEqual(metrics['hook_to_utterance_ms']['count'], 3)
        self.assertEqual(metrics['input_to_next_hook_ms']['count'], 2)
        self.assertEqual(metrics['utterance_to_presentation_ms']['count'], 3)
        self.assertEqual(metrics['vad_estimated_end_to_decision_ms']['count'], 2)
        self.assertEqual(metrics['vad_estimated_end_to_input_ms']['count'], 2)
        self.assertNotIn('asr_decode_ms', metrics)  # Older traces cannot invent inference speed.
        # Optional real measurements must not convert a valid input sequence into FAIL.
        at = records[-1]['at_ns'] + 100
        records.append({'event': 'asr_inference', 'seq': len(records), 'at_ns': at,
                        'session': 1, 'line': 3, 'audio_ms': 2000,
                        'decode_ms': 4000.0, 'queue_ms': 3.2})
        result = validate(records, phase='native')
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['performance']['asr_rtf']['p95'], 2.0)
        self.assertEqual(result['performance']['asr_decode_ms']['count'], 1)

    def test_voice_timing_cannot_be_fabricated_or_misclassified(self):
        records = contract_records(9, 12)
        self.assertTrue(validate(records, phase='native')['ok'])
        wrong = [dict(r, estimated_at_ns=r['at_ns'] + 1) if
                 r['event'] == 'native_voice_end_estimate' and r['line'] == 1 else r
                 for r in records]
        result = validate(wrong, phase='native')
        self.assertFalse(result['ok'])
        # A pre-HOOK onset must appear as a negative signed interval, not be
        # silently discarded by an unsigned-duration helper.
        early = [dict(r) for r in records]
        event = next(r for r in early if r['event'] == 'native_voice_end_estimate' and r['line'] == 2)
        hook = next(r for r in early if r['event'] == 'hook_output' and r['hook_sequence'] == 11)
        event['onset_estimated_at_ns'] = hook['received_ns'] - 50_000_000
        metric = validate(early, phase='native')
        self.assertTrue(metric['ok'], metric['errors'])
        self.assertLess(metric['performance']['hook_to_vad_onset_ms']['p50'], 0)

    def test_wrong_origin_and_wrong_dialogue_cannot_claim_success(self):
        records = contract_records(0, 3, mode='reading')
        wrong_pid = [dict(r, pid=999) if r['event'] == 'hook_output' else r for r in records]
        self.assertFalse(validate(wrong_pid, phase='reading')['ok'])
        wrong_text = [dict(r, body_hash=text_digest('別の台詞')) if r['event'] == 'utterance' and r['line'] == 2 else r for r in records]
        self.assertFalse(validate(wrong_text, phase='reading')['ok'])
        without_connection = [r for r in records if r['event'] != 'hook_connected']
        self.assertFalse(validate(without_connection, phase='reading')['ok'])

    def test_mixed_auto_progress_does_not_require_manual_pause_or_menu_selection(self):
        # Covers the complete ordinary scenario up to the pre-menu handoff.
        # All forward movement is attributed to AutoAdv; no test operator
        # intervention is required or credited as automatic advancement.
        records = contract_records(0, 20, lines=MIXED_SCENARIO)
        result = validate(records, phase='mixed')
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['confirmed_advance_to_next_hook'], 19)
        self.assertEqual(result['line_results'][-1]['scenario'], 'auto-test-end')
        self.assertEqual(result['line_results'][-1]['outcome'], 'no_autoadv_input')

        # A real menu handoff is not permission for AutoAdv to send another
        # input automatically, and must never count as a successful advance.
        invalid = contract_records(0, 21)
        handoff = next(i for i, record in enumerate(invalid)
                       if record['event'] == 'utterance' and record['line'] == 20)
        invalid[handoff + 1:handoff + 1] = [
            {'event': 'advance_attempt', 'session': 1, 'line': 20},
            {'event': 'advance_input', 'session': 1, 'line': 20},
        ]
        for i, record in enumerate(invalid):
            record['at_ns'] = i * 1_000_000_000 + 1
            if record['event'] == 'hook_output':
                record['received_ns'] = record['at_ns'] - 1
            if i:
                record['seq'] = i
        self.assertFalse(validate(invalid, phase='hook')['ok'])

    def test_mixed_stop_rejects_sending_while_paused(self):
        records = contract_records(0, 20, lines=MIXED_SCENARIO)
        point = next(i for i, record in enumerate(records)
                     if record['event'] == 'advance_attempt')
        records.insert(point, {'event': 'pause_requested', 'session': 1})
        for i, record in enumerate(records):
            record['at_ns'] = i * 1_000_000_000 + 1
            if record['event'] == 'hook_output':
                record['received_ns'] = record['at_ns'] - 1
            if i:
                record['seq'] = i
        self.assertFalse(validate(records, phase='mixed')['ok'])


class SignedLatencyContract(unittest.TestCase):
    def test_premature_decision_is_negative_not_dropped(self):
        from validation.real_game.verify_trace import _signed_elapsed_ms
        self.assertEqual(_signed_elapsed_ms(2_000_000_000, 1_750_000_000), -250.0)
        self.assertEqual(_signed_elapsed_ms(2_000_000_000, 2_500_000_000), 500.0)
        self.assertIsNone(_signed_elapsed_ms(None, 2_000_000_000))


if __name__ == '__main__':
    unittest.main()
