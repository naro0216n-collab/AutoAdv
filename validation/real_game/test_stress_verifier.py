"""Stress verifier invariants; synthetic records are not real-game evidence."""
import json
from pathlib import Path
import unittest
from autoadv.validation_trace import text_digest
from validation.real_game.verify_trace import validate as _validate
from validation.real_game._synthetic_checker_oracle import synthetic_game_observations


def validate(records, *, phase='mixed', **kwargs):
    # Both inputs are deliberately synthetic and used ONLY to unit-test the
    # verifier's error handling. Not an AutoAdv/Windows acceptance result.
    return _validate(records, phase=phase,
                     oracle_records=synthetic_game_observations(records, phase=phase),
                     **kwargs)


LINES = json.loads((Path(__file__).parent / 'ASR_STRESS_SCENARIO.json').read_text('utf-8'))['lines']


def checker_only_records():
    records = [{'event': 'trace_started', 'at_ns': 1, 'version': 2}]
    def add(event, **fields):
        at = len(records) * 1_000_000_000
        if event == 'hook_output':
            fields['received_ns'] = at - 100
        records.append({'event': event, 'at_ns': at, 'seq': len(records), **fields})
    add('hook_connected', pid=41, generation='host', connection=2)
    add('monitor_started', session=2, source='hook', game_id='x', profile_id='p',
        hook_pid=41, input_pid=41, audio_pid=41, generation='host', connection=2)
    add('native_ready', session=2, audio_pid=41)
    add('permit_requested', session=2)
    for i, case in enumerate(LINES, 1):
        key = {'session': 2, 'line': i}
        raw_hash = text_digest(case['text'])
        add('hook_output', session=2, pid=41, generation='host', connection=2,
            source='H0', address=143, hook_sequence=i, raw_hash=raw_hash)
        add('utterance', **key, source='H0', hook_sequence=i, raw_hash=raw_hash,
            body_hash=raw_hash, advance_mode=case['advance_mode'],
            use_tts=case['use_tts'], text_wait_ms=500, characters=5,
            uncertain_text_fallback=False)
        if case['advance_mode'] == 'audio':
            add('native_pcm', **key, audio_pid=41, frames=960)
            add('asr_inference', **key, audio_ms=1200, decode_ms=121, queue_ms=6)
            if case['id'] == 'clean_control':
                add('native_event', **key, status='started', method='asr')
                add('native_event', **key, status='completed', method='asr')
        if case['id'] in ('stress-intro', 'clean_control'):
            add('presentation_complete', **key, success=True,
                reason='text' if case['advance_mode'] == 'text' else 'native')
            add('advance_attempt', **key)
            add('advance_input', **key)
        # The unit fixture contains later-game records but no AutoAdv sends.
        # Such game progression is not credited to the application.
    return records


class StressTraceVerifierTests(unittest.TestCase):
    def test_stress_cannot_pass_when_later_dialogues_depend_on_external_progress(self):
        result = validate(checker_only_records(), phase='stress')
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertTrue(any('dialogue observed without preceding AutoAdv SendInput' in e
                            for e in result['diagnostics']))
        self.assertEqual(result['confirmed_advance_to_next_hook'], 2)
        self.assertEqual(result['confirmed_tts'], 0)
        self.assertTrue(all('expected_hold' not in c for c in LINES))

    def test_identical_audio_lexical_content_cannot_prove_background_owner(self):
        cases = json.loads((Path(__file__).parent / 'ASR_STRESS.json').read_text('utf-8'))['cases']
        identical = next(c for c in cases if c['id'] == 'background_only_same_text')
        self.assertEqual(identical['attribution'], 'unverified')
        self.assertEqual(identical['display'], 'ヒューズが飛んだ。')
        # No verifier rule treats this authored label as proof of unsafe input.

    def test_missing_challenge_and_unjustified_advance_rejected(self):
        records = checker_only_records()
        partial = validate(records[:-4], phase='stress')
        self.assertTrue(partial['collection_valid'], partial['errors'])
        self.assertFalse(partial['advancement_observed'] and
                         partial['confirmed_advance_to_next_hook'] >= 3)
        self.assertEqual(partial['observed_lines'], len(LINES) - 1)
        faulty = [dict(r) for r in records]
        target = next(r for r in faulty if r.get('event') == 'utterance' and
                      r['body_hash'] == text_digest('これは表示されただけの文章です。'))
        point = faulty.index(target)
        faulty.insert(point + 1, {'event': 'advance_attempt', 'session': 2, 'line': target['line']})
        faulty.insert(point + 2, {'event': 'advance_input', 'session': 2, 'line': target['line']})
        for i, r in enumerate(faulty):
            r['seq'] = i
            r['at_ns'] = i * 1_000_000_000 + 1
            if r['event'] == 'hook_output':
                r['received_ns'] = r['at_ns'] - 100
        self.assertFalse(validate(faulty, phase='stress')['ok'])


if __name__ == '__main__':
    unittest.main()
