"""Benchmark tooling contract; real model inference is explicitly opt-in."""
from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from validation.real_game.benchmark_asr import _degrade, _edit_distance, run
from validation.real_game import prepare_asr_model


class OnDemandBenchmarkContracts(unittest.TestCase):
    def test_pinned_model_uses_external_validation_cache_and_never_requires_irodori(self):
        directory = prepare_asr_model.MODEL_DIR.resolve()
        repo = Path(__file__).resolve().parents[2]
        self.assertFalse(directory.is_relative_to(repo), f'ASR model cache must be outside source tree: {directory}')
        self.assertTrue(str(directory).endswith(('validation/asr', 'validation\\asr')))
        with self.assertRaisesRegex(ValueError, 'outside the repository'):
            prepare_asr_model.prepare(Path(__file__).parent / 'asr-model', allow_download=False)
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                prepare_asr_model.prepare(path, allow_download=False)
            self.assertFalse(any(path.iterdir()))

    def test_levenshtein_and_deterministic_audio_degradation(self):
        import numpy as np
        self.assertEqual(_edit_distance('あいう', 'あう'), 1)
        mono = np.linspace(-.7, .7, 480, dtype='float32')
        self.assertTrue(np.array_equal(_degrade(mono, 'clean', 7), mono))
        self.assertTrue(np.array_equal(_degrade(mono, 'noise', 7), _degrade(mono, 'noise', 7)))
        self.assertFalse(np.array_equal(_degrade(mono, 'quiet', 7), mono))
        self.assertTrue(np.array_equal(_degrade(mono, 'bgm', 7, mono),
                                       _degrade(mono, 'bgm', 7, mono)))
        self.assertTrue(np.array_equal(_degrade(mono, 'truncated', 7)[:120],
                                       np.zeros(120, dtype='float32')))

    def test_benchmark_invokes_the_product_recognizer_without_enforcing_accuracy(self):
        class FakeRecognizer:
            def __init__(self, model, tokens):
                pass
            def recognize(self, samples):
                self_test.assertTrue(samples)
                return '認識不可'  # Measuring error is a valid result, not a test failure.
        self_test = self
        with TemporaryDirectory() as root:
            model, tokens = Path(root) / 'model', Path(root) / 'tokens'
            model.write_bytes(b'model placeholder')
            tokens.write_bytes(b'tokens placeholder')
            with patch('validation.real_game.benchmark_asr.JapaneseASR', FakeRecognizer):
                report = run(model, tokens, conditions=('clean',), rounds=1, seed=10)
            self.assertEqual(report['conditions']['clean']['runs'], 14)
            self.assertEqual(len(report['samples']), 14)
            self.assertIn('model_sha256', report)
            self.assertIn('decode_ms', report['conditions']['clean'])
            self.assertTrue(all(row['cer'] > 0 for row in report['samples']))


if __name__ == '__main__':
    unittest.main()
