"""Stress fixture and reporting contracts; neither synthetic transcripts nor a game E2E."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from validation.real_game import stress_asr
from validation.real_game.prepare_stress_audio import STRESS


class StressScenarios(unittest.TestCase):
    def test_background_voices_are_real_recordings_and_not_automatically_attributed(self):
        import numpy as np
        cases, recordings = stress_asr._load_cases(selection='background')
        self.assertEqual(len(cases), 5)
        for case in cases:
            with self.subTest(case=case['id']):
                waveform = stress_asr._mix(case, recordings)
                self.assertTrue(np.any(waveform != 0))
                self.assertTrue(np.isfinite(waveform).all())
                if case['attribution'] == 'not_foreground':
                    self.assertIsNone(case['foreground'])
                if case['attribution'] != 'foreground':
                    self.assertIsNotNone(case['background'])
        outlasts = next(c for c in cases if c['id'] == 'background_outlasts_foreground')
        self.assertGreater(len(stress_asr._mix(outlasts, recordings)),
                           len(recordings[outlasts['foreground']]))

    def test_missing_actual_cry_scream_cannot_silently_pass(self):
        # The separate nonlexical source must be recorded human vocalization,
        # never synthesized noise renamed "crying" to satisfy test assertions.
        with patch.object(stress_asr, 'VOICE', Path('/missing-actual-nonlexical-assets')):
            with self.assertRaisesRegex(FileNotFoundError, 'Actual nonverbal audio required'):
                stress_asr._load_cases(selection='nonlexical')

    def test_report_exposes_background_same_text_false_attribution(self):
        class BackgroundRecognizer:
            def __init__(self, *_):
                pass
            def recognize(self, chunks):
                return 'ヒューズが飛んだ。'
        with TemporaryDirectory() as tmp:
            model, tokens = Path(tmp) / 'model', Path(tmp) / 'tokens'
            model.write_bytes(b'fake-model-only-for-checker')
            tokens.write_bytes(b'fake-tokens-only-for-checker')
            with patch.object(stress_asr, 'JapaneseASR', BackgroundRecognizer):
                data = stress_asr.run(model, tokens, selection='background')
            risk = next(c for c in data['cases'] if c['id'] == 'background_only_same_text')
            self.assertTrue(risk['false_attribution_risk'])
            self.assertIs(data['real_game_verified'], False)
            self.assertEqual(len(data['observations']), 5)
            self.assertEqual(next(o for o in data['observations'] if o['case'] == 'background_only_same_text')['cer'], None)

    def test_stress_is_native_or_reading_only_not_tts_asr(self):
        scenario = json.loads((STRESS.parent / 'ASR_STRESS_SCENARIO.json').read_text(encoding='utf-8'))
        self.assertEqual({line['suggested_mode'] for line in scenario['lines']}, {'native', 'reading'})
        # Only original-game audio is submitted to the recognizer. The TTS
        # contract waits for real playback drain and is tested separately.
        self.assertNotIn('tts', {line['suggested_mode'] for line in scenario['lines']})

    def test_nonlexical_cases_are_not_given_word_error_success_targets(self):
        cases = json.loads(STRESS.read_text(encoding='utf-8'))['cases']
        self.assertEqual({c['id'] for c in cases if c['kind'].startswith('nonlexical')},
                         {'nonlexical_cry', 'nonlexical_scream', 'cry_with_background_voice'})
        self.assertTrue(all(c['attribution'] != 'foreground' for c in cases if c['kind'].startswith('nonlexical')))


if __name__ == '__main__':
    unittest.main()
