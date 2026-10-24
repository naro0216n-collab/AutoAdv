"""Alignment benchmark tooling contract; real model inference is explicitly opt-in."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from autoadv.g2p import PronunciationCandidate
from autoadv import native_runtime
from validation.real_game.benchmark_alignment import (
    _degrade, _load_endpoint_oracle, _snapshot_sample_ends, _stream_align, run,
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

    def test_deterministic_audio_degradation(self):
        mono = np.linspace(-.7, .7, 480, dtype='float32')
        self.assertTrue(np.array_equal(_degrade(mono, 'clean', 7), mono))
        self.assertTrue(np.array_equal(_degrade(mono, 'noise', 7), _degrade(mono, 'noise', 7)))
        self.assertFalse(np.array_equal(_degrade(mono, 'quiet', 7), mono))
        self.assertTrue(np.array_equal(_degrade(mono, 'bgm', 7, mono),
                                       _degrade(mono, 'bgm', 7, mono)))
        self.assertTrue(np.array_equal(_degrade(mono, 'truncated', 7)[:120],
                                       np.zeros(120, dtype='float32')))

    def test_benchmark_measures_product_alignment_contract_not_cer(self):
        with TemporaryDirectory() as root:
            model, vocab = Path(root) / 'model', Path(root) / 'vocab'
            model.write_bytes(b'model placeholder')
            vocab.write_bytes(b'vocab placeholder')
            report = run(model, vocab, conditions=('clean',), rounds=1, seed=10,
                         model_backend=StaticModel(), g2p=StaticG2P())
        self.assertEqual(report['schema'], 'autoadv-alignment-benchmark-v3')
        self.assertEqual(report['conditions']['clean']['runs'], 14)
        self.assertEqual(len(report['samples']), 14)
        self.assertEqual(report['conditions']['clean']['associated'], 14)
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

    def test_snapshot_schedule_matches_native_cumulative_cadence(self):
        ends = _snapshot_sample_ends(1600 * 48)
        self.assertEqual(tuple(round(value / 48) for value in ends), (300, 800, 1300))
        self.assertEqual(_snapshot_sample_ends(200 * 48), ())

    def test_stream_scheduler_uses_idle_drain_only_for_unprocessed_tail(self):
        aligner = __import__('autoadv.forced_alignment', fromlist=['TargetConditionedAligner']) \
            .TargetConditionedAligner(StaticModel(), g2p=StaticG2P())
        aligner.set_target('target')
        mono = np.zeros(1600 * 48, dtype='float32')
        snapshots = _stream_align(aligner, mono, idle_drain=True)
        self.assertEqual(
            tuple(round(item['capture_end_ms']) for item in snapshots),
            (300, 800, 1300, 1600))
        self.assertEqual(tuple(item['trigger'] for item in snapshots),
                         ('cadence', 'cadence', 'cadence', 'idle_drain'))
        self.assertGreaterEqual(snapshots[-1]['observed_at_ms'], 1700)

        continuous = _stream_align(aligner, mono, idle_drain=False)
        self.assertEqual(
            tuple(round(item['capture_end_ms']) for item in continuous),
            (300, 800, 1300))

    def test_stream_scheduler_applies_single_worker_backpressure(self):
        class FakeAligner:
            def align(self, chunks):
                return object()

        mono = np.zeros(1000 * 48, dtype='float32')
        # First inference takes 600 ms.  The nominal 800 ms submission cannot
        # run while it is still in flight, so the next cumulative capture is
        # taken at 900 ms and cadence restarts from that actual submission.
        with patch('validation.real_game.benchmark_alignment.time.perf_counter_ns',
                   side_effect=(0, 600_000_000, 700_000_000, 800_000_000)):
            snapshots = _stream_align(FakeAligner(), mono, idle_drain=False)
        self.assertEqual(tuple(round(item['capture_end_ms']) for item in snapshots),
                         (300, 900))
        self.assertEqual(tuple(round(item['observed_at_ms']) for item in snapshots),
                         (300, 900))

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
