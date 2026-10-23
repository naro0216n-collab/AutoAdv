"""Benchmark tooling contract; real model inference is explicitly opt-in."""
from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autoadv.native_audio import ASRResult
from autoadv.audio_match import surface_key
from autoadv.g2p import PronunciationCandidate

from validation.real_game.benchmark_asr import _degrade, _edit_distance, run
from autoadv import native_runtime


class IdentityG2P:
    def candidates(self, text, *, max_candidates=4):
        units = tuple(surface_key(text))
        return (PronunciationCandidate(units),) if units and max_candidates else ()


TEST_G2P = IdentityG2P()


class OnDemandBenchmarkContracts(unittest.TestCase):
    def test_benchmark_defaults_to_the_product_managed_asr_runtime(self):
        paths = native_runtime.managed_paths()
        repo = Path(__file__).resolve().parents[2]
        self.assertFalse(Path(paths.asr_model).resolve().is_relative_to(repo))
        self.assertEqual(Path(paths.asr_model).name, 'model.int8.onnx')
        self.assertEqual(Path(paths.asr_tokens).name, 'tokens.txt')
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                native_runtime.prepare_asr_model(runtime_root=Path(tmp), allow_download=False)


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
                return ASRResult('認識不可')  # Measuring error is a valid result, not a test failure.
        self_test = self
        with TemporaryDirectory() as root:
            model, tokens = Path(root) / 'model', Path(root) / 'tokens'
            model.write_bytes(b'model placeholder')
            tokens.write_bytes(b'tokens placeholder')
            with patch('validation.real_game.benchmark_asr.JapaneseASR', FakeRecognizer), \
                 patch('autoadv.g2p.default_g2p', return_value=TEST_G2P):
                report = run(model, tokens, conditions=('clean',), rounds=1, seed=10)
            self.assertEqual(report['conditions']['clean']['runs'], 14)
            self.assertEqual(len(report['samples']), 14)
            self.assertIn('model_sha256', report)
            self.assertIn('decode_ms', report['conditions']['clean'])
            self.assertTrue(all(row['cer'] > 0 for row in report['samples']))


if __name__ == '__main__':
    unittest.main()
