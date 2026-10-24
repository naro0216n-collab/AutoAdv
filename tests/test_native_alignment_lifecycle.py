"""NativeAudio owns one target-conditioned observation generation at a time."""
from __future__ import annotations

from pathlib import Path
from threading import Event
import time
import unittest

import numpy as np

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_knowledge import NativeVoiceKnowledge, reduce_native_voice_knowledge
from autoadv.conversation import Token
from autoadv.forced_alignment import TargetTimeAlignment
from autoadv.g2p import PronunciationCandidate
from autoadv.native_audio import NativeAudio
from autoadv.process_audio import CaptureHealth


VOCAB = {'PAD': 0, 'UNK': 1, 'SOS': 2, 'EOS': 3, 'a': 4, 'b': 5, 'c': 6}


class StaticG2P:
    def candidates(self, text, *, max_candidates=4):
        readings = {
            'target': ('a', 'b', 'c'),
            'other': ('c', 'b', 'a'),
        }
        phones = readings.get(text, ())
        return (PronunciationCandidate(phones),) if phones and max_candidates else ()


class StaticPosterior:
    frame_stride_ms = 20.0
    vocab = VOCAB

    def __init__(self, *, terminal=True):
        frames = 30 if terminal else 16
        self.logits = np.full((frames, len(VOCAB)), -4.0, dtype=np.float32)
        self.logits[:, 0] = 5.0
        for phone, frame in zip(('a', 'b', 'c'), (5, 10, 15)):
            if frame < frames:
                self.logits[frame, 0] = 0.0
                self.logits[frame, VOCAB[phone]] = 7.0

    def infer(self, chunks):
        return self.logits


class BlockingPosterior(StaticPosterior):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()

    def infer(self, chunks):
        self.started.set()
        self.release.wait(2)
        return self.logits


def stereo_chunk(end_ms: int, duration_ms: int = 600, value: float = 0.0) -> PCMChunk:
    samples = int(48 * duration_ms)
    pcm = np.full((samples, 2), value, dtype='<f4').tobytes()
    return PCMChunk(end_ms, pcm, 48000, 2, 4)


def wait_idle(native: NativeAudio) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with native._lock:
            if not native._inflight:
                return
        time.sleep(.002)
    raise TimeoutError('alignment inference did not settle')


def make_native(model, observations, errors=None, reports=None, *, clock=lambda: 1000):
    return NativeAudio(
        42, Path('unused-helper.dll'), Path('unused.onnx'), Path('unused.json'),
        lambda token, reason: (errors if errors is not None else []).append((token, reason)),
        model_backend=model, g2p=StaticG2P(), clock=clock,
        on_observation=lambda token, item: observations.append((token, item)),
        on_inference=(lambda token, item: reports.append((token, item))) if reports is not None else None,
    )


class NativeAlignmentLifecycleTests(unittest.TestCase):
    def test_target_setup_rejects_unrepresentable_reading_before_audio_inference(self):
        observations, errors = [], []
        native = make_native(StaticPosterior(), observations, errors)
        try:
            with self.assertRaisesRegex(ValueError, 'no pronunciation representable'):
                native.begin(Token(1, 1), 'unknown')
            self.assertFalse(observations)
        finally:
            native.close()

    def test_one_cumulative_alignment_observation_can_confirm_end(self):
        observations, reports = [], []
        native = make_native(StaticPosterior(), observations, reports=reports)
        token = Token(1, 1)
        try:
            native.history.start(42)
            native.boundary(1000)
            native.begin(token, 'target')
            chunk = stereo_chunk(1600)
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
            wait_idle(native)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0][0], token)
            alignment = observations[0][1].alignment
            self.assertTrue(alignment.target_associated)
            self.assertTrue(alignment.complete_alignment)
            self.assertTrue(alignment.committed_end)
            knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observations[0][1])
            self.assertTrue(knowledge.target_end_confirmed)
            self.assertEqual(reports[0][1]['target_units'], 3)
            self.assertIn('align_ms', reports[0][1])
            self.assertNotIn('transcript', reports[0][1])
        finally:
            native.close()

    def test_endpoint_wall_time_uses_model_stride_not_capture_frame_ratio(self):
        class FortyNineFramePosterior(StaticPosterior):
            def __init__(self):
                self.logits = np.full((49, len(VOCAB)), -4.0, dtype=np.float32)
                self.logits[:, 0] = 5.0
                for phone, frame in zip(('a', 'b', 'c'), (10, 20, 30)):
                    self.logits[frame, 0] = 0.0
                    self.logits[frame, VOCAB[phone]] = 7.0

        observations = []
        native = make_native(FortyNineFramePosterior(), observations)
        token = Token(2, 1)
        try:
            native.history.start(42)
            native.boundary(1000)
            native.begin(token, 'target')
            chunk = stereo_chunk(2000, 1000)
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
            wait_idle(native)
            observation = observations[-1][1]
            self.assertEqual(observation.alignment.frame_ms, 20.0)
            self.assertEqual(observation.alignment.target_end_frame, 31)
            self.assertEqual(observation.target_acoustic_end_ms, 1620)
        finally:
            native.close()

    def test_complete_path_at_capture_right_edge_is_not_end_knowledge(self):
        observations = []
        native = make_native(StaticPosterior(terminal=False), observations)
        token = Token(1, 1)
        try:
            native.history.start(42)
            native.boundary(1000)
            native.begin(token, 'target')
            chunk = stereo_chunk(1320, 320)
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
            wait_idle(native)
            alignment = observations[-1][1].alignment
            self.assertTrue(alignment.complete_alignment)
            self.assertFalse(alignment.committed_end)
            knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observations[-1][1])
            self.assertFalse(knowledge.target_end_confirmed)
        finally:
            native.close()

    def test_pre_hook_pcm_is_replayed_only_from_bound_generation(self):
        observations = []
        native = make_native(StaticPosterior(), observations, clock=lambda: 2000)
        token = Token(4, 2)
        try:
            native.history.start(42)
            native.boundary(1000)
            old = stereo_chunk(900, 200)
            kept = stereo_chunk(1400, 400)
            native.history.push(old, target_pid=42)
            native.history.push(kept, target_pid=42)
            native.begin(token, 'target')
            wait_idle(native)
            self.assertEqual(native.window_start_ms, 1000)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0][1].capture_start_ms, 1000)
            self.assertEqual(observations[0][1].capture_end_ms, 1400)
        finally:
            native.close()

    def test_late_result_from_old_generation_is_discarded(self):
        observations = []
        model = BlockingPosterior()
        native = make_native(model, observations)
        old = Token(1, 1)
        new = Token(1, 2)
        try:
            native.history.start(42)
            native.boundary(1000)
            native.begin(old, 'target')
            chunk = stereo_chunk(1600)
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
            self.assertTrue(model.started.wait(1))
            native.begin(new, 'other', replay_from_ms=1600)
            model.release.set()
            wait_idle(native)
            self.assertFalse(any(token == old for token, _ in observations))
        finally:
            model.release.set()
            native.close()

    def test_transport_heartbeat_remains_transport_observation(self):
        observations = []
        native = make_native(StaticPosterior(), observations)
        token = Token(1, 1)
        try:
            native.history.start(42)
            native.begin(token, 'target')
            native._last_pcm_end_ms = 1200
            native.health(CaptureHealth(1300, True, '', pcm_frames=0))
            self.assertEqual(observations[-1][1].kind, 'transport')
            knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observations[-1][1])
            self.assertFalse(knowledge.target_associated)
            self.assertFalse(knowledge.target_end_confirmed)
        finally:
            native.close()

    def test_concatenated_model_time_maps_across_capture_gap_without_compressing_wall_time(self):
        first = stereo_chunk(100, 20)
        second = stereo_chunk(200, 20)
        self.assertEqual(NativeAudio._audio_offset_to_wall_ms((first, second), 10), 90)
        self.assertEqual(NativeAudio._audio_offset_to_wall_ms((first, second), 25), 185)
        self.assertEqual(NativeAudio._audio_offset_to_wall_ms((first, second), 40), 200)

    def test_capture_summary_has_transport_measurements_but_no_vad_fields(self):
        observations = []
        native = make_native(StaticPosterior(), observations)
        try:
            native.health(CaptureHealth(100, True, '', pcm_frames=960))
            status = native.capture_status()
            self.assertEqual(status['pcm_frames'], 960)
            self.assertNotIn('vad_positive_packets', status)
            self.assertNotIn('vad_input_peak', status)
        finally:
            native.close()


if __name__ == '__main__':
    unittest.main()
