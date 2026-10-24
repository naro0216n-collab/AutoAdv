"""Stress fixture/reporting contracts for target-conditioned alignment."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from autoadv.g2p import PronunciationCandidate
from validation.real_game import stress_alignment


class StaticG2P:
    def candidates(self, text, *, max_candidates=4):
        return (PronunciationCandidate(('a', 'b', 'c')),)


class StaticModel:
    frame_stride_ms = 20.0
    vocab = {'PAD': 0, 'UNK': 1, 'SOS': 2, 'EOS': 3, 'a': 4, 'b': 5, 'c': 6}

    def infer(self, chunks):
        audio_ms = sum(chunk.duration_ms for chunk in chunks)
        frames = max(1, int(audio_ms // self.frame_stride_ms))
        logits = np.full((frames, len(self.vocab)), -4.0, dtype=np.float32)
        logits[:, 0] = 5.0
        for phone, frame in zip(('a', 'b', 'c'), (5, 10, 15)):
            if frame < frames:
                logits[frame, 0] = 0.0
                logits[frame, self.vocab[phone]] = 7.0
        return logits


class AlignmentStressScenarios(unittest.TestCase):



    def test_missing_actual_cry_scream_cannot_silently_pass(self):
        with patch.object(stress_alignment, 'VOICE', Path('/missing-actual-nonlexical-assets')):
            with self.assertRaisesRegex(FileNotFoundError, 'Actual nonverbal audio required'):
                stress_alignment._load_cases(selection='nonlexical')

    def test_same_text_background_remains_unverified_origin_even_if_target_aligns(self):
        with TemporaryDirectory() as tmp:
            model, vocab = Path(tmp) / 'model', Path(tmp) / 'vocab'
            model.write_bytes(b'fake-model')
            vocab.write_bytes(b'fake-vocab')
            report = stress_alignment.run(
                model, vocab, selection='background',
                model_backend=StaticModel(), g2p=StaticG2P())
        self.assertEqual(report['schema'], 'autoadv-alignment-stress-v2')
        case = next(c for c in report['cases'] if c['id'] == 'background_only_same_text')
        self.assertEqual(case['attribution'], 'unverified')
        observation = next(o for o in report['observations']
                           if o['case'] == 'background_only_same_text')
        self.assertTrue(observation['target_associated'])
        self.assertTrue(observation['committed_end'])
        self.assertNotIn('false_attribution_risk', observation)
        self.assertFalse(report['real_game_verified'])

    def test_contract_assertion_checks_only_cases_with_independent_origin_oracle(self):
        passing = {
            'cases': [
                {'id': 'clean_control', 'associated': 2, 'committed': 2},
                {'id': 'background_only_mismatch', 'associated': 0, 'committed': 0},
                {'id': 'background_only_same_text', 'associated': 2, 'committed': 2},
            ],
            'observations': [
                {'case': 'clean_control'}, {'case': 'clean_control'},
                {'case': 'background_only_mismatch'}, {'case': 'background_only_mismatch'},
                {'case': 'background_only_same_text'}, {'case': 'background_only_same_text'},
            ],
        }
        self.assertEqual(stress_alignment.contract_failures(passing), [])

        false_positive = json.loads(json.dumps(passing))
        false_positive['cases'][1]['associated'] = 1
        false_positive['cases'][1]['committed'] = 1
        failures = stress_alignment.contract_failures(false_positive)
        self.assertIn('unrelated background speech was associated with target', failures)
        self.assertIn('unrelated background speech committed a target end', failures)

        missed_target = json.loads(json.dumps(passing))
        missed_target['cases'][0]['committed'] = 1
        self.assertIn('clean target end was not committed on every run',
                      stress_alignment.contract_failures(missed_target))




if __name__ == '__main__':
    unittest.main()
