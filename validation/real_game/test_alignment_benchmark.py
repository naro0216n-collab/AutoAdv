"""Alignment benchmark tooling contract; real model inference is explicitly opt-in."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np

from autoadv.g2p import PronunciationCandidate
from autoadv import native_runtime
from validation.real_game.benchmark_alignment import (
    _load_endpoint_oracle,
    contract_failures, run,
)


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


class OnDemandAlignmentBenchmarkContracts(unittest.TestCase):
    def test_benchmark_defaults_to_product_managed_alignment_runtime(self):
        paths = native_runtime.managed_paths()
        repo = Path(__file__).resolve().parents[2]
        self.assertFalse(Path(paths.alignment_model).resolve().is_relative_to(repo))
        self.assertEqual(Path(paths.alignment_model).name, 'model.q4.onnx')
        self.assertEqual(Path(paths.alignment_vocab).name, 'vocab.json')
        with TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                native_runtime.prepare_alignment_model(runtime_root=Path(tmp), allow_download=False)


    def test_benchmark_measures_product_alignment_contract_not_cer(self):
        with TemporaryDirectory() as root:
            model, vocab = Path(root) / 'model', Path(root) / 'vocab'
            model.write_bytes(b'model placeholder')
            vocab.write_bytes(b'vocab placeholder')
            report = run(model, vocab, conditions=('clean',), rounds=1, seed=10,
                         model_backend=StaticModel(), g2p=StaticG2P())
        self.assertEqual(report['schema'], 'autoadv-alignment-benchmark-v3')
        self.assertGreater(report['conditions']['clean']['runs'], 0)
        self.assertEqual(len(report['samples']), report['conditions']['clean']['runs'])
        self.assertEqual(report['conditions']['clean']['associated'],
                         report['conditions']['clean']['runs'])
        self.assertNotIn('cer', report['conditions']['clean'])
        self.assertNotIn('recognized_hash', report['samples'][0])
        self.assertFalse(report['real_game_verified'])
        self.assertEqual(report['endpoint_oracle']['schema'], 'autoadv-alignment-endpoint-oracle-v1')
        self.assertIn('endpoint_error_ms', report['conditions']['clean'])
        self.assertIn('early_commits', report['conditions']['clean'])
        self.assertIn('commit_delay_ms', report['conditions']['clean'])
        self.assertIn('capture_commit_delay_ms', report['conditions']['clean'])
        self.assertIn('early_decisions', report['conditions']['clean'])
        self.assertIn('cumulative_compute_ratio', report['conditions']['clean'])
        self.assertIn('oracle_end_ms', report['samples'][0])
        self.assertIn('oracle_uncertainty_ms', report['samples'][0])
        self.assertGreater(report['samples'][0]['snapshot_count'], 1)
        self.assertIn('commit_capture_end_ms', report['samples'][0])
        self.assertIn('commit_observed_at_ms', report['samples'][0])


    def test_benchmark_contract_requires_clean_and_bgm_and_rejects_missing_suffix_commit(self):
        passing = {
            'conditions': {
                'clean': {'runs': 2, 'associated': 2, 'committed': 2,
                          'early_commits': 0, 'early_decisions': 0},
                'bgm': {'runs': 2, 'associated': 2, 'committed': 2,
                        'early_commits': 0, 'early_decisions': 0},
                'truncated': {'runs': 2, 'committed': 0},
            }
        }
        self.assertEqual(contract_failures(passing), [])
        unsafe = {
            'conditions': {
                'clean': {'runs': 1, 'associated': 1, 'committed': 1,
                          'early_commits': 1, 'early_decisions': 0},
                'bgm': {'runs': 1, 'associated': 1, 'committed': 0,
                        'early_commits': 0, 'early_decisions': 0},
                'truncated': {'runs': 1, 'committed': 1},
            }
        }
        failures = contract_failures(unsafe)
        self.assertTrue(any('early committed' in item for item in failures))
        self.assertTrue(any('bgm target end' in item for item in failures))
        self.assertTrue(any('missing target suffix' in item for item in failures))






    def test_endpoint_oracle_covers_every_voice_and_is_not_file_end(self):
        import json
        manifest = json.loads((Path(__file__).parent / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
        meta, oracle = _load_endpoint_oracle(manifest)
        self.assertEqual(meta['schema'], 'autoadv-alignment-endpoint-oracle-v1')
        self.assertEqual(set(oracle), {sample['id'] for sample in manifest['samples']})
        for sample in manifest['samples']:
            endpoint = oracle[sample['id']]['speech_end_ms']
            self.assertLess(endpoint, sample['duration_s'] * 1000)


if __name__ == '__main__':
    unittest.main()
