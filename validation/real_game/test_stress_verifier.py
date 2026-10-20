"""Stress-trace *checker* contracts. Fabricated records never certify game execution."""
import json
from pathlib import Path
import unittest
from autoadv.validation_trace import text_digest
from validation.real_game.verify_trace import validate

LINES = json.loads((Path(__file__).parent / 'ASR_STRESS_SCENARIO.json').read_text(encoding='utf-8'))['lines']


def checker_only_records():
    records = [{'event': 'trace_started', 'at_ns': 1, 'version': 2}]
    def add(event, **fields):
        at = len(records) * 1000000
        if event == 'hook_output':
            fields['received_ns'] = at - 100
        records.append({'event': event, 'at_ns': at, 'seq': len(records), **fields})
    add('hook_connected', pid=41, generation='host', connection=2)
    add('monitor_started', session=2, source='hook', game_id='x', profile_id='p',
        hook_pid=41, input_pid=41, audio_pid=41, generation='host', connection=2)
    add('native_ready', session=2, audio_pid=41, vad_only=False)
    add('permit_requested', session=2)
    for i, case in enumerate(LINES, 1):
        key = {'session': 2, 'line': i}
        raw_hash = text_digest(case['text'])
        add('hook_output', session=2, pid=41, generation='host', connection=2,
            source='H0', address=143, hook_sequence=i, raw_hash=raw_hash)
        add('utterance', **key, source='H0', hook_sequence=i, raw_hash=raw_hash,
            body_hash=raw_hash, mode=case['suggested_mode'])
        if case['suggested_mode'] == 'native':
            add('native_pcm', **key, audio_pid=41, frames=960)
            add('asr_inference', **key, audio_ms=1200, decode_ms=121, queue_ms=6)
            if not case.get('expected_hold'):
                add('native_event', **key, status='started', method='asr')
                add('native_event', **key, status='completed', method='asr')
        if case.get('expected_hold'):
            add('permit_requested', session=2)
            # Actual inference must occur after authorization for this line.
            inference = records[-2] if records[-2]['event'] == 'asr_inference' else None
            if inference:
                records.pop()  # move permit in front of inference
                records.insert(-1, {'event':'permit_requested', 'session':2})
                # Normalize sequence and timestamp below
            add('pause_requested', session=2)
        elif i < len(LINES):
            add('presentation_complete', **key, success=True)
            add('advance_attempt', **key)
            add('advance_input', **key)
    # Normalize order, then place each hold authorization before actual infer.
    for i, record in enumerate(records):
        record['seq'] = i
        record['at_ns'] = i * 1000000 + 1
        if record['event'] == 'hook_output':
            record['received_ns'] = record['at_ns'] - 100
    return records


class StressTraceVerifierTests(unittest.TestCase):
    def test_expected_manual_intervention_does_not_need_irodori(self):
        records = checker_only_records()
        result = validate(records, phase='stress')
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['confirmed_advance_to_next_hook'], 2)
        self.assertEqual(result['confirmed_tts'], 0)

    def test_background_only_false_completion_is_rejected(self):
        records = checker_only_records()
        key = next(r for r in records if r.get('event') == 'utterance' and
                   r['body_hash'] == text_digest('ヒューズが飛んだ。'))
        idx = records.index(key)
        records.insert(idx + 1, {'event':'native_event','session':2,'line':key['line'],
                                 'status':'completed','method':'asr'})
        for i, r in enumerate(records):
            r['seq'] = i
            r['at_ns'] = i * 1000000 + 1
            if r['event'] == 'hook_output':
                r['received_ns'] = r['at_ns'] - 100
        self.assertFalse(validate(records, phase='stress')['ok'])

    def test_missing_challenge_and_premature_input_fail(self):
        records = checker_only_records()
        self.assertFalse(validate(records[:-5], phase='stress')['ok'])
        incorrect = [dict(r) for r in records]
        target = next(r for r in incorrect if r.get('event') == 'utterance' and
                      r['body_hash'] == text_digest('これは表示されただけの文章です。'))
        idx = incorrect.index(target)
        incorrect.insert(idx+1, {'event':'advance_attempt', 'session':2, 'line':target['line']})
        for i, r in enumerate(incorrect):
            r['seq'] = i
            r['at_ns'] = i * 1000000 + 1
            if r['event'] == 'hook_output':
                r['received_ns'] = r['at_ns'] - 100
        self.assertFalse(validate(incorrect, phase='stress')['ok'])


if __name__ == '__main__':
    unittest.main()
