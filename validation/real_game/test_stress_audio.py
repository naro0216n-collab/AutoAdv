"""Stress fixture and reporting contracts; neither synthetic transcripts nor a game E2E."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from validation.real_game import stress_asr
from validation.real_game.stress_asr import STRESS, VOICE


class StressScenarios(unittest.TestCase):
    def test_background_voices_are_real_recordings_and_not_automatically_attributed(self):
        import numpy as np
        cases, recordings = stress_asr._load_cases(selection='background')
        self.assertGreaterEqual(len(cases), 1)
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

    def test_pinned_real_nonlexical_recordings_are_committed_and_integral(self):
        import wave
        from validation.real_game.benchmark_asr import _load_pcm
        assets = json.loads(STRESS.read_text(encoding='utf-8'))['external_assets']
        self.assertEqual({x['id'] for x in assets}, {'stress_cry', 'stress_scream'})
        for asset in assets:
            with self.subTest(asset=asset['id']):
                file = VOICE / asset['filename']
                self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(),
                                 asset['prepared_sha256'])
                with wave.open(str(file), 'rb') as recording:
                    self.assertEqual((recording.getframerate(), recording.getnchannels(),
                                      recording.getsampwidth()), (48000, 1, 2))
                self.assertGreater(len(_load_pcm(file, asset['prepared_sha256'])), 48000)

    def test_missing_actual_cry_scream_cannot_silently_pass(self):
        # The separate nonlexical source must be recorded human vocalization,
        # never synthesized noise renamed "crying" to satisfy test assertions.
        with patch.object(stress_asr, 'VOICE', Path('/missing-actual-nonlexical-assets')):
            with self.assertRaisesRegex(FileNotFoundError, 'Actual nonverbal audio required'):
                stress_asr._load_cases(selection='nonlexical')

    def test_same_text_ambiguous_origin_is_not_fabricated_negative_acceptance(self):
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
            self.assertEqual(risk['attribution'], 'unverified')
            self.assertFalse(risk['false_attribution_risk'])
            self.assertIs(data['real_game_verified'], False)
            self.assertEqual(len(data['observations']), len(data['cases']))
            self.assertEqual(next(o for o in data['observations'] if o['case'] == 'background_only_same_text')['transcript'], 'ヒューズが飛んだ。')

    def test_stress_is_native_or_reading_only_not_tts_asr(self):
        scenario = json.loads((STRESS.parent / 'ASR_STRESS_SCENARIO.json').read_text(encoding='utf-8'))
        self.assertEqual({line['advance_mode'] for line in scenario['lines']}, {'audio', 'text'})
        # Only original-game audio is submitted to the recognizer. The TTS
        # contract waits for real playback drain and is tested separately.
        self.assertTrue(all(not line['use_tts'] for line in scenario['lines']))

    def test_nonlexical_cases_are_not_given_word_error_success_targets(self):
        cases = json.loads(STRESS.read_text(encoding='utf-8'))['cases']
        self.assertEqual({c['id'] for c in cases if c['kind'].startswith('nonlexical')},
                         {'nonlexical_cry', 'nonlexical_scream', 'cry_with_background_voice'})
        self.assertTrue(all(c['attribution'] != 'foreground' for c in cases if c['kind'].startswith('nonlexical')))


if __name__ == '__main__':
    unittest.main()
