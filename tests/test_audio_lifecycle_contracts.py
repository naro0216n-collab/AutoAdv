"""Native audio observation lifecycle and provenance contracts."""
from __future__ import annotations

from pathlib import Path
from threading import Event
import time

import numpy as np
import pytest

from autoadv.audio_buffer import PCMChunk
from autoadv.acoustic_observers import AcousticObservationHistory, VocalActivityFrame
from autoadv.conversation import Token
from autoadv.g2p import PronunciationCandidate
from autoadv.native_audio import NativeAudio
from autoadv.process_audio import CaptureHealth

VOCAB = {'PAD': 0, 'UNK': 1, 'SOS': 2, 'EOS': 3, 'a': 4, 'b': 5, 'c': 6}


class StaticG2P:
    def candidates(self, text, *, max_candidates=4):
        readings = {'target': ('a', 'b', 'c'), 'other': ('c', 'b', 'a')}
        phones = readings.get(text, ())
        return (PronunciationCandidate(phones),) if phones and max_candidates else ()


class StaticPosterior:
    frame_stride_ms = 20.0
    vocab = VOCAB

    def __init__(self, *, frames=30, starts=(5, 10, 15)):
        self.logits = np.full((frames, len(VOCAB)), -4.0, dtype=np.float32)
        self.logits[:, 0] = 5.0
        for phone, frame in zip(('a', 'b', 'c'), starts):
            if frame < frames:
                self.logits[frame, 0] = 0.0
                self.logits[frame, VOCAB[phone]] = 7.0

    def infer(self, chunks):
        return self.logits


class StaticVocalObserver:
    def observe(self, chunk):
        start = int(round(chunk.at_ms - chunk.duration_ms))
        return tuple(VocalActivityFrame(at_ms=t, probability=0.0)
                     for t in range(start + 16, int(chunk.at_ms) + 1, 16))


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


def tonal_chunk(end_ms: int, components: tuple[tuple[float, float], ...],
                duration_ms: int = 400) -> PCMChunk:
    samples = int(48 * duration_ms)
    seconds = np.arange(samples, dtype=np.float64) / 48000.0
    mono = np.zeros(samples, dtype=np.float64)
    for frequency, amplitude in components:
        mono += np.sin(2.0 * np.pi * frequency * seconds) * amplitude
    mono = np.clip(mono, -1.0, 1.0).astype('<f4')
    stereo = np.column_stack((mono, mono)).astype('<f4', copy=False)
    return PCMChunk(end_ms, stereo.tobytes(), 48000, 2, 4)


def wait_idle(native: NativeAudio) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with native._lock:
            if not native._inflight:
                return
        time.sleep(.002)
    raise TimeoutError('alignment inference did not settle')


def make_native(model, observations, errors=None, reports=None, *, clock=lambda: 1000, vocal_observer=None):
    return NativeAudio(
        42, Path('unused-helper.dll'), Path('unused.onnx'), Path('unused.json'),
        lambda token, reason: (errors if errors is not None else []).append((token, reason)),
        model_backend=model, g2p=StaticG2P(), clock=clock, vocal_observer=vocal_observer or StaticVocalObserver(),
        on_observation=lambda token, item: observations.append((token, item)),
        on_inference=(lambda token, item: reports.append((token, item))) if reports is not None else None,
    )


def test_native_audio_generation_owns_target_and_discards_stale_inference():
    observations = []
    model = BlockingPosterior()
    native = make_native(model, observations)
    old, new = Token(1, 1), Token(1, 2)
    try:
        native.begin(old, 'unknown')
        assert native.aligner is None  # non-lexical audio remains observable without alignment
        native.history.start(42)
        native.boundary(1000)
        native.begin(old, 'target')
        chunk = stereo_chunk(1600)
        native.history.push(chunk, target_pid=42)
        native.feed(chunk)
        assert model.started.wait(1)
        native.begin(new, 'other')
        model.release.set()
        wait_idle(native)
        assert not any(token == old for token, _ in observations)
    finally:
        model.release.set()
        native.close()


def test_native_audio_maps_model_time_to_capture_time_without_using_transport_as_endpoint():
    observations, reports = [], []
    native = make_native(StaticPosterior(frames=49, starts=(10, 20, 30)), observations, reports=reports)
    token = Token(2, 1)
    try:
        native.history.start(42)
        native.boundary(1000)
        native.begin(token, 'target')
        chunk = stereo_chunk(2000, 1000)
        native.history.push(chunk, target_pid=42)
        native.feed(chunk)
        wait_idle(native)
        item = observations[-1][1]
        assert item.lexical is not None
        assert item.lexical.target_end_ms == 1620
        assert item.lexical.complete_alignment and item.lexical.committed_target_end
        assert item.vocal is not None

        native.health(CaptureHealth(2100, True, '', pcm_frames=0))
        transport = observations[-1][1]
        assert transport.kind == 'transport'
        assert transport.lexical is None
        assert transport.heartbeat_at_ms == 2100
    finally:
        native.close()


def test_pcm_history_replay_and_gap_mapping_preserve_wall_clock_provenance():
    observations = []
    native = make_native(StaticPosterior(), observations, clock=lambda: 2000)
    token = Token(4, 2)
    try:
        native.history.start(42)
        native.boundary(1000)
        old = stereo_chunk(900, 200)
        # This packet starts before the Presentation Epoch and ends after it.
        # Replay must crop the pre-boundary frames instead of leaking them into
        # the new utterance's alignment window.
        kept = stereo_chunk(1400, 500)
        native.history.push(old, target_pid=42)
        native.history.push(kept, target_pid=42)
        native.begin(token, 'target')
        wait_idle(native)
        item = observations[-1][1]
        assert (native.window_start_ms, item.capture_start_ms, item.capture_end_ms) == (1000, 1000, 1400)
        replay = native.history.since(1000)
        assert len(replay) == 1 and replay[0].duration_ms == pytest.approx(400.0)

        first = stereo_chunk(100, 20)
        second = stereo_chunk(200, 20)
        assert [NativeAudio._audio_offset_to_wall_ms((first, second), x) for x in (10, 25, 40)] == [90, 185, 200]
    finally:
        native.close()


def test_delayed_hook_attachment_does_not_change_retained_acoustic_meaning():
    def observe_at(hook_clock: int):
        observations = []
        native = make_native(StaticPosterior(), observations, clock=lambda: hook_clock)
        token = Token(6, 1)
        try:
            native.history.start(42)
            native.boundary(1000)
            # Process Loopback and compact observers run before the HOOK/text is
            # attached. The same captured series must mean the same thing even
            # if the HOOK arrives much later.
            for chunk in (stereo_chunk(1300, 300), stereo_chunk(1600, 300)):
                native.history.push(chunk, target_pid=42)
                native.feed(chunk)
            native.begin(token, 'target')
            wait_idle(native)
            return observations[-1][1]
        finally:
            native.close()

    early = observe_at(1700)
    late = observe_at(5200)
    assert (early.capture_start_ms, early.capture_end_ms) == (late.capture_start_ms, late.capture_end_ms)
    assert early.lexical == late.lexical
    assert early.vocal == late.vocal
    assert early.recurrence == late.recurrence


def test_heartbeat_can_flush_pending_pcm_but_never_becomes_voice_completion_evidence():
    observations = []
    native = make_native(StaticPosterior(), observations)
    token = Token(8, 1)
    try:
        native.history.start(42)
        native.boundary(1000)
        native.begin(token, 'target')
        short = stereo_chunk(1200, 200)
        native.history.push(short, target_pid=42)
        native.feed(short)
        assert not any(item.kind == 'acoustic' for _, item in observations)
        native.health(CaptureHealth(1300, True, '', pcm_frames=0))
        wait_idle(native)
        assert len([item for _, item in observations if item.kind == 'acoustic']) == 1
        transport = [item for _, item in observations if item.kind == 'transport'][-1]
        assert transport.heartbeat_at_ms == 1300
        assert transport.lexical is None
        status = native.capture_status()
        assert 'pcm_frames' in status and status['boundary_certainty'] == 'autoadv'
    finally:
        native.close()

def test_vocal_recurrence_discounts_periodicity_already_explained_by_background_audio():
    def history_for(current_components):
        history = AcousticObservationHistory()
        for at_ms in (600, 1000, 1400):
            history.add_fingerprint(
                tonal_chunk(at_ms, ((440.0, 0.25),)), vocal_relevance=0.05)
        for at_ms in (1800, 2200, 2600, 3000):
            history.add_fingerprint(
                tonal_chunk(at_ms, current_components), vocal_relevance=0.9)
        return history.recurrence(epoch_start_ms=1500, end_ms=3000)

    background_only = history_for(((440.0, 0.25),))
    vocal_over_background = history_for(((440.0, 0.25), (1000.0, 0.5)))

    assert background_only.strength == 0.0
    assert vocal_over_background.strength > background_only.strength
    assert vocal_over_background.period_ms is not None
