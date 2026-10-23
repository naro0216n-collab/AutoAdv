"""Negative verifier-contract tests. Crafted evidence is not an E2E test."""
import unittest

from validation.real_game.verify_trace import validate
from validation.real_game.test_e2e_verifier import contract_records
from validation.real_game._synthetic_checker_oracle import synthetic_game_observations


class IndependentGameOracleContract(unittest.TestCase):
    def setUp(self):
        # Use audio-judgment lines; even a perfect ASR transcript must never
        # outweigh evidence of an unfinished game voice.
        self.product = contract_records(9, 12)
        self.game = synthetic_game_observations(self.product)

    def verify(self, product=None, game=None, **kwargs):
        return validate(product if product is not None else self.product,
                        phase='native', start_line=10,
                        oracle_records=game if game is not None else self.game, **kwargs)

    def test_absent_game_observer_is_never_real_acceptance(self):
        observation = validate(self.product, phase='native')
        self.assertTrue(observation['collection_valid'], observation['errors'])
        self.assertFalse(observation['voice_safety_verified'])
        self.assertTrue(any('voice safety unverified' in item
                            for item in observation['diagnostics']))
        self.assertTrue(self.verify()['ok'])  # checker-only positive fixture

    def test_attempt_without_confirmed_send_cannot_count_as_safe_input(self):
        product = [dict(x) for x in self.product if not (
            x['event'] == 'advance_input' and x.get('line') == 1)]
        for i, record in enumerate(product):
            if i:
                record['seq'] = i
        observed = self.verify(product=product)
        self.assertTrue(any('input attempt has no confirmed advance input' in d
                            for d in observed['diagnostics']))
        # The second line still has a confirmed send; the first is unverified.
        self.assertNotEqual(observed['game_evidence']['lines'][0]['outcome'],
                            'voice_end_confirmed_then_input')

    def test_voice_timing_uses_confirmed_send_not_attempt(self):
        product = [dict(x) for x in self.product]
        attempt = next(x for x in product if x['event'] == 'advance_attempt' and x['line'] == 1)
        # Preserve the event sequence; move the pre-input attempt earlier than
        # observed game playback, while confirmed advance input stays unchanged.
        attempt['at_ns'] -= 200_000_000
        for rec in product:
            if rec['event'] in ('presentation_complete',) and rec.get('line') == 1:
                rec['at_ns'] = min(rec['at_ns'], attempt['at_ns'])
        result = self.verify(product=product)
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertGreaterEqual(result['performance']['game_voice_end_to_input_ms']['p50'], 0)
        self.assertEqual(result['game_evidence']['early_inputs'], 0)

    def test_line_without_autoadv_input_is_not_an_advance_success(self):
        result = self.verify()
        # Last acquired dialogue has no controller input in this checker-only
        # record. The playback observer does not invent a second advancement
        # fact from game-internal dialogue state.
        self.assertEqual(result['game_evidence']['lines'][-1]['outcome'],
                         'no_autoadv_input')

    def test_input_before_voice_end_fails_despite_production_asr_completion(self):
        game = [dict(x) for x in self.game]
        voice_end = next(x for x in game if x['event'] == 'voice_ended' and x['line'] == 1)
        input_at = next(x['at_ns'] for x in self.product
                        if x['event'] == 'advance_input' and x['line'] == 1)
        voice_end['at_ns'] = input_at + 5000000
        voice_end['last_active_at_ns'] = input_at + 1000000
        game.sort(key=lambda x: x['at_ns'])
        result = self.verify(game=game)
        self.assertFalse(result['ok'])
        self.assertEqual(result['game_evidence']['early_inputs'], 1)
        self.assertLess(result['performance']['game_voice_end_to_input_ms']['p50'], 0)

    def test_fixture_audio_filename_is_not_an_autoadv_acceptance_condition(self):
        game = [dict(x) for x in self.game]
        for record in game:
            if record.get('line') == 1 and record['event'] in ('voice_started', 'voice_ended'):
                record['file'] = 'audio/voice/other.wav'
        result = self.verify(game=game)
        self.assertTrue(result['ok'], result['errors'])

    def test_mixed_voice_lifecycles_on_one_line_are_never_certified_safe(self):
        game = [dict(x) for x in self.game]
        first_start = next(x for x in game if x['event'] == 'voice_started' and x['line'] == 1)
        first_end = next(x for x in game if x['event'] == 'voice_ended' and x['line'] == 1)
        # Reproduce the old oracle failure mode: a previous line's channel is
        # spuriously started/ended under the current line before the current
        # foreground voice lifecycle. The verifier must not select the first
        # convenient end and certify the input as safe.
        game.extend((
            dict(first_start, at_ns=first_start['at_ns'] + 1,
                 file='audio/voice/previous.wav'),
            dict(first_end, at_ns=first_start['at_ns'] + 2,
                 last_active_at_ns=first_start['at_ns'] + 1,
                 file='audio/voice/previous.wav'),
        ))
        game.sort(key=lambda x: x['at_ns'])
        result = self.verify(game=game)
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertFalse(result['voice_safety_verified'])
        self.assertEqual(result['game_evidence']['unverified_voice_ends'], 1)
        self.assertEqual(result['game_evidence']['lines'][0]['outcome'], 'voice_end_unverified')
        self.assertTrue(any('ambiguous independent game voice lifecycle' in item
                            for item in result['diagnostics']))

    def test_input_within_observer_poll_interval_is_indeterminate_not_early_or_safe(self):
        game = [dict(x) for x in self.game]
        end = next(x for x in game if x['event'] == 'voice_ended' and x['line'] == 1)
        input_at = next(x['at_ns'] for x in self.product
                        if x['event'] == 'advance_input' and x['line'] == 1)
        end['last_active_at_ns'] = input_at - 20_000_000
        end['at_ns'] = input_at + 30_000_000
        game.sort(key=lambda x: x['at_ns'])
        result = self.verify(game=game)
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertFalse(result['voice_safety_verified'])
        self.assertEqual(result['game_evidence']['indeterminate_voice_end'], 1)
        self.assertEqual(result['game_evidence']['early_inputs'], 0)
        self.assertEqual(result['game_evidence']['confirmed_voice_safe_inputs'], 1)
        self.assertEqual(result['game_evidence']['lines'][0]['outcome'], 'voice_end_indeterminate')

    def test_silence_never_observed_is_not_positive_voice_end(self):
        game = [x for x in self.game if not (x['event'] == 'voice_ended' and x['line'] == 1)]
        result = self.verify(game=game)
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertFalse(result['voice_safety_verified'])
        self.assertEqual(result['game_evidence']['unverified_voice_ends'], 1)

    def test_separate_audio_process_is_not_arbitrarily_prohibited(self):
        product = [dict(x) for x in self.product]
        for record in product:
            if record['event'] in ('monitor_started', 'native_ready', 'native_pcm'):
                record['audio_pid'] = 51
        # Actual capture must match the selected audio source. The independent
        # game observer belongs to the input/window PID, which need not be 51.
        result = self.verify(product=product)
        self.assertTrue(result['ok'], result['errors'])

    def test_game_pid_must_match_hook_pcm_and_input_pid(self):
        game = [dict(x, pid=999) for x in self.game]
        result = self.verify(game=game)
        self.assertFalse(result['ok'])
        self.assertIn('game oracle PID does not match the selected input/window process',
                      result['errors'])

    def test_replay_or_tamper_in_game_order_never_counts_as_success(self):
        game = [dict(x) for x in self.game]
        game[1]['body_hash'] = '0'*64
        observation = self.verify(game=game)
        self.assertTrue(observation['collection_valid'], observation['errors'])
        self.assertFalse(observation['voice_safety_verified'])
        self.assertTrue(any('no unique playback observation' in item
                            for item in observation['diagnostics']))

    def test_one_unaligned_game_line_does_not_discard_other_voice_observations(self):
        game = [dict(x) for x in self.game]
        first = next(x for x in game if x['event'] == 'line_started' and x['line'] == 1)
        first['body_hash'] = '0' * 64
        observation = self.verify(game=game)
        self.assertTrue(observation['collection_valid'], observation['errors'])
        self.assertFalse(observation['voice_safety_verified'])
        self.assertEqual(observation['game_evidence']['unaligned_lines'], 1)
        self.assertEqual(observation['game_evidence']['lines'][0]['outcome'],
                         'game_playback_unaligned')
        self.assertEqual(observation['game_evidence']['lines'][1]['game_line'], 2)
        self.assertGreater(observation['game_evidence']['confirmed_voice_safe_inputs'], 0)

    def test_delay_is_measured_without_default_overhold_judgement(self):
        observed = self.verify()
        self.assertTrue(observed['collection_valid'], observed['errors'])
        self.assertEqual(observed['game_evidence']['overholds'], 0)
        self.assertIn('game_voice_end_to_input_ms', observed['performance'])

    def test_overhold_is_measured_from_independent_playback_not_vad_guess(self):
        game = [dict(x) for x in self.game]
        end = next(x for x in game if x['event'] == 'voice_ended' and x['line'] == 1)
        end['at_ns'] -= 200_000_000
        end['last_active_at_ns'] = end['at_ns'] - 1_000_000
        game.sort(key=lambda x: x['at_ns'])
        result = self.verify(game=game, max_voice_hold_ms=100)
        self.assertTrue(result['collection_valid'], result['errors'])
        self.assertGreater(result['game_evidence']['overholds'], 0)
        self.assertIn('game_voice_end_to_input_ms', result['performance'])

    def test_no_foreground_native_attribution_rejected_even_with_asr_completion(self):
        # Author-specified absence is NOT used as the only evidence: observer
        # must independently confirm the voice channel was never active.
        product = contract_records(9, 12)
        from pathlib import Path
        import json
        path = Path(__file__).with_name('SCENARIO.json')
        schema = json.loads(path.read_text('utf-8'))
        schema['lines'][9]['foreground_voice'] = False
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            temp = Path(directory) / 'scenario.json'
            temp.write_text(json.dumps(schema, ensure_ascii=False), 'utf-8')
            game = [x for x in self.game if not (x.get('line') == 1 and
                     x['event'] in ('voice_started', 'voice_ended'))]
            result = validate(product, phase='native', start_line=10,
                              oracle_records=game, scenario_path=temp)
            self.assertTrue(result['collection_valid'], result['errors'])
            self.assertFalse(result['voice_safety_verified'])
            self.assertEqual(result['game_evidence']['misattributions'], 1)


if __name__ == '__main__':
    unittest.main()
