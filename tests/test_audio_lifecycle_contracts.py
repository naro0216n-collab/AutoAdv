"""Native audio observation lifecycle and provenance contracts."""
from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
import time

import numpy as np
import pytest

from autoadv.audio_buffer import PCMChunk
from autoadv.acoustic_observers import (
    AcousticObservationHistory, HumanVocalActivityObserver, VocalActivityFrame,
)
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


class CountingPosterior(StaticPosterior):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    def infer(self, chunks):
        self.calls += 1
        return super().infer(chunks)




class SnapshotSensitivePosterior:
    """Return different target evidence when the analyzed PCM snapshot grows."""

    frame_stride_ms = 20.0
    vocab = VOCAB

    def __init__(self):
        self.calls = 0

    def infer(self, chunks):
        self.calls += 1
        duration_ms = sum(chunk.duration_ms for chunk in chunks)
        frames = max(30, int(duration_ms // self.frame_stride_ms))
        logits = np.full((frames, len(VOCAB)), -4.0, dtype=np.float32)
        logits[:, 0] = 5.0
        if duration_ms <= 1_050:
            for phone, frame in zip(('a', 'b', 'c'), (10, 20, 30)):
                if frame < frames:
                    logits[frame, 0] = 0.0
                    logits[frame, VOCAB[phone]] = 7.0
        return logits


class HistorySensitivePosterior:
    """Locate the target only when the assessment includes earlier retained PCM."""

    frame_stride_ms = 20.0
    vocab = VOCAB

    def __init__(self):
        self.calls = 0

    def infer(self, chunks):
        self.calls += 1
        duration_ms = sum(chunk.duration_ms for chunk in chunks)
        frames = max(30, int(duration_ms // self.frame_stride_ms))
        logits = np.full((frames, len(VOCAB)), -4.0, dtype=np.float32)
        logits[:, 0] = 5.0
        if duration_ms >= 700:
            for phone, frame in zip(('a', 'b', 'c'), (5, 10, 15)):
                logits[frame, 0] = 0.0
                logits[frame, VOCAB[phone]] = 7.0
        return logits


class ActiveVocalObserver:
    def observe(self, chunk):
        start = int(round(chunk.at_ms - chunk.duration_ms))
        return tuple(VocalActivityFrame(at_ms=t, score=.9, support_start_ms=t - 16)
                     for t in range(start + 16, int(chunk.at_ms) + 1, 16))


class StaticVocalObserver:
    def observe(self, chunk):
        start = int(round(chunk.at_ms - chunk.duration_ms))
        return tuple(VocalActivityFrame(at_ms=t, score=0.0)
                     for t in range(start + 16, int(chunk.at_ms) + 1, 16))


class BlockingPosterior(StaticPosterior):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.calls = 0

    def infer(self, chunks):
        self.calls += 1
        self.started.set()
        self.release.wait(2)
        return self.logits


class RecordingBlankPosterior:
    """Record query sizes while deliberately finding no target."""

    frame_stride_ms = 20.0
    vocab = VOCAB

    def __init__(self):
        self.durations_ms = []

    def infer(self, chunks):
        duration_ms = sum(chunk.duration_ms for chunk in chunks)
        self.durations_ms.append(duration_ms)
        frames = max(30, int(duration_ms // self.frame_stride_ms))
        logits = np.full((frames, len(VOCAB)), -4.0, dtype=np.float32)
        logits[:, 0] = 5.0
        return logits


class BlockingVocalObserver:
    """Make observation arbitrarily slow without changing capture semantics."""

    def __init__(self):
        self.started = Event()
        self.release = Event()

    def observe(self, chunk):
        self.started.set()
        self.release.wait(2)
        return ()

    def reset(self):
        pass


class StaticAudioEventModel:
    def __init__(self, *, class_index=19, score=.9):
        self.class_index = class_index
        self.score = score

    def infer(self, _waveform):
        scores = np.zeros(521, dtype=np.float32)
        scores[self.class_index] = self.score
        return scores


class SilentSpeechObserver:
    def observe(self, chunk):
        return (VocalActivityFrame(
            at_ms=int(chunk.at_ms), score=0.0,
            support_start_ms=int(round(chunk.at_ms - chunk.duration_ms))),)


def stereo_chunk(end_ms: int, duration_ms: int = 600, value: float = 0.0) -> PCMChunk:
    samples = int(48 * duration_ms)
    pcm = np.full((samples, 2), value, dtype='<f4').tobytes()
    return PCMChunk(end_ms, pcm, 48000, 2, 4)


def compact_chunk(end_ms: int, duration_ms: int = 1000) -> PCMChunk:
    """Tiny PCM for scheduler tests whose posterior does not inspect samples."""
    return PCMChunk(end_ms, bytes(duration_ms), 1000, 1, 1)


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
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with native._lock:
            if (not native._alignment_inflight
                    and native._pending_alignment is None
                    and not native._observer_inflight
                    and not native._observer_queue):
                return
        time.sleep(.002)
    raise TimeoutError('native audio analysis did not settle')


def make_native(model, observations, errors=None, reports=None, *, clock=lambda: 1000, vocal_observer=None):
    return NativeAudio(
        42, Path('unused-helper.dll'), Path('unused.onnx'), Path('unused.json'),
        lambda token, reason: (errors if errors is not None else []).append((token, reason)),
        model_backend=model, g2p=StaticG2P(), clock=clock, vocal_observer=vocal_observer or StaticVocalObserver(),
        on_progress=lambda token, _progress, item: observations.append((token, item)),
        on_inference=(lambda token, item: reports.append((token, item))) if reports is not None else None,
    )



def test_monitoring_start_does_not_invent_a_target_causal_boundary(monkeypatch):
    observations = []
    native = make_native(StaticPosterior(), observations, clock=lambda: 1000)
    started = []
    monkeypatch.setattr(native._capture, "start", lambda: started.append(True))
    try:
        native.start()
        assert started == [True]
        assert native.causal_lower_bound_ms is None
    finally:
        native.close()

def test_slow_alignment_reaches_required_frontier_without_draining_intermediate_queries():
    observations, reports = [], []
    model = BlockingPosterior()
    native = make_native(model, observations, reports=reports)
    token = Token(1, 1)
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        first = stereo_chunk(1600)
        native.history.push(first, target_pid=42)
        native.feed(first)
        assert model.started.wait(1)

        # Capture can outrun one expensive whole-snapshot inference. Periodic
        # intermediate frontiers are not Application obligations.
        for end_ms in (2200, 2800, 3400):
            chunk = stereo_chunk(end_ms, 600)
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)

        # A fallback/deadline fence is an obligation: once the worker becomes
        # available, the latest captured frontier must be evaluated directly
        # rather than draining every obsolete intermediate snapshot first.
        native.request_assessment_through(token, 3400)
        with native._lock:
            assert native._pending_alignment is not None
            assert native._pending_alignment[2] == 3400
        model.release.set()
        wait_idle(native)

        # Continuous human-vocal observation may publish cheap progress while
        # alignment is busy.  The expensive target query coalesces to one newest
        # pending fence.  If the first query commits the lexical endpoint, that
        # pending query becomes unnecessary rather than being drained anyway.
        aligned_frontiers = [item['capture_end_ms'] for observed, item in reports
                             if observed == token]
        assert aligned_frontiers == [1600]
        assert model.calls == 1
        assert any(item.capture_end_ms == 3400 for observed, item in observations
                   if observed == token)
    finally:
        model.release.set()
        native.close()


def test_replaced_target_cannot_publish_stale_alignment_result():
    observations = []
    model = BlockingPosterior()
    native = make_native(model, observations)
    old, newest = Token(1, 1), Token(1, 2)
    try:
        native.history.start(42)
        native.begin(old, 'target', causal_lower_bound_ms=1000)
        chunk = stereo_chunk(1600)
        native.history.push(chunk, target_pid=42)
        native.feed(chunk)
        assert model.started.wait(1)

        before_replacement = len(observations)
        native.begin(newest, 'other')
        model.release.set()
        wait_idle(native)

        assert observations[before_replacement:]
        assert all(token == newest for token, _ in observations[before_replacement:])
    finally:
        model.release.set()
        native.close()


def test_native_audio_maps_model_time_to_capture_time_without_using_transport_as_endpoint():
    observations, reports = [], []
    native = make_native(StaticPosterior(frames=49, starts=(10, 20, 30)), observations, reports=reports)
    token = Token(2, 1)
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        chunk = stereo_chunk(2000, 1000)
        native.history.push(chunk, target_pid=42)
        native.feed(chunk)
        wait_idle(native)
        item = observations[-1][1]
        assert item.lexical is not None
        assert item.lexical.target_end_ms == 1620
        assert item.lexical.complete_alignment and item.lexical.committed_target_end
        assert item.vocal is not None

        before = len(observations)
        native.health(CaptureHealth(2100, True, '', pcm_frames=0))
        wait_idle(native)
        assert not any(item.kind == 'transport' for _, item in observations[before:])
    finally:
        native.close()


def test_pcm_timeline_query_crops_causal_boundary_without_replaying_capture_state():
    observations = []
    native = make_native(StaticPosterior(), observations, clock=lambda: 2000)
    token = Token(4, 2)
    try:
        native.history.start(42)
        old = stereo_chunk(900, 200)
        # This packet starts before the known causal lower bound and ends after
        # it.  The alignment query crops earlier frames directly from the PCM
        # timeline; attaching the target does not replay capture state.
        kept = stereo_chunk(1400, 500)
        native.history.push(old, target_pid=42)
        native.feed(old)
        native.history.push(kept, target_pid=42)
        native.feed(kept)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        wait_idle(native)
        item = observations[-1][1]
        assert (item.capture_start_ms, item.capture_end_ms) == (1000, 1400)
        assert item.causal_lower_bound_ms == 1000
    finally:
        native.close()


def test_external_input_candidates_narrow_initial_search_without_becoming_causal_bounds():
    observations = []
    native = make_native(StaticPosterior(), observations, clock=lambda: 2000)
    token = Token(4, 3)
    try:
        native.history.start(42)
        for chunk in (stereo_chunk(1000, 300), stereo_chunk(1500, 500)):
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
        native.begin(token, 'target', input_candidates_ms=(1200, 1400))
        wait_idle(native)
        item = observations[-1][1]
        assert item.causal_lower_bound_ms is None
        assert item.input_candidates_ms == (1200, 1400)
        assert item.capture_start_ms == 1200
    finally:
        native.close()


def test_delayed_hook_attachment_does_not_change_retained_acoustic_meaning():
    def observe_at(hook_clock: int):
        observations = []
        native = make_native(StaticPosterior(), observations, clock=lambda: hook_clock)
        token = Token(6, 1)
        try:
            native.history.start(42)
            # Process Loopback and compact observers run before the HOOK/text is
            # attached. The same captured series must mean the same thing even
            # if the HOOK arrives much later.
            for chunk in (stereo_chunk(1300, 300), stereo_chunk(1600, 300)):
                native.history.push(chunk, target_pid=42)
                native.feed(chunk)
            native.begin(token, 'target', causal_lower_bound_ms=1000)
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


def test_candidate_search_widens_to_retained_pcm_only_when_target_is_not_found():
    observations, reports = [], []
    model = HistorySensitivePosterior()
    native = make_native(model, observations, reports=reports, clock=lambda: 2_000)
    token = Token(7, 1)
    try:
        native.history.start(42)
        for chunk in (stereo_chunk(1000, 300), stereo_chunk(1500, 500)):
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
        native.begin(token, 'target', input_candidates_ms=(1200,))
        wait_idle(native)

        assert native.causal_lower_bound_ms is None
        assert model.calls == 2
        assert observations[-1][1].capture_start_ms == 700
        assert observations[-1][1].lexical is not None
        assert observations[-1][1].lexical.target_associated
    finally:
        native.close()


def test_heartbeat_can_flush_pending_pcm_but_never_becomes_voice_completion_evidence():
    observations = []
    model = CountingPosterior()
    native = make_native(model, observations)
    token = Token(8, 1)
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        short = stereo_chunk(1200, 200)
        native.history.push(short, target_pid=42)
        native.feed(short)
        assert not any(item.kind == 'acoustic' for _, item in observations)

        # A policy boundary requests exactly the already-captured snapshot. With
        # less than the alignment minimum it can still publish feature evidence
        # without inventing a lexical result.
        assert native.observation_fence(token) == 1200
        native.request_assessment_through(token, 1200)
        wait_idle(native)
        snapshots = [item for _, item in observations if item.kind == 'snapshot']
        assert snapshots and snapshots[-1].capture_end_ms == 1200
        before = len(snapshots)

        # A heartbeat can request the newest already-captured frontier, but with
        # no new PCM or measurement it must not manufacture another observation.
        native.health(CaptureHealth(1300, True, '', pcm_frames=0))
        wait_idle(native)
        assert len([item for _, item in observations if item.kind == 'snapshot']) == before
        assert not any(item.kind == 'transport' for _, item in observations)
        status = native.capture_status()
        assert 'pcm_frames' in status and status['causal_lower_bound_ms'] == 1000
    finally:
        native.close()


def test_committed_target_mapping_is_reused_while_later_pcm_only_updates_endpoint_evidence():
    observations = []
    model = SnapshotSensitivePosterior()
    native = make_native(model, observations)
    token = Token(9, 1)
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        first = stereo_chunk(2000, 1000)
        native.history.push(first, target_pid=42)
        native.feed(first)
        wait_idle(native)
        first_snapshot = observations[-1][1]
        assert first_snapshot.capture_end_ms == 2000
        assert first_snapshot.lexical is not None
        assert first_snapshot.lexical.committed_target_end

        tail = stereo_chunk(2600, 600)
        native.history.push(tail, target_pid=42)
        native.feed(tail)
        wait_idle(native)
        second_snapshot = observations[-1][1]
        assert second_snapshot.capture_end_ms == 2600
        assert second_snapshot.lexical is not None
        assert second_snapshot.lexical == first_snapshot.lexical
        assert model.calls == 1
    finally:
        native.close()


def test_recurrence_is_queried_only_after_direct_endpoint_evidence_remains_open():
    observations = []
    native = make_native(
        StaticPosterior(frames=49, starts=(10, 20, 30)), observations,
        vocal_observer=ActiveVocalObserver())
    token = Token(9, 2)
    recurrence_calls = []
    original = native.features.recurrence

    def counted(*, start_ms, end_ms):
        recurrence_calls.append((start_ms, end_ms))
        return original(start_ms=start_ms, end_ms=end_ms)

    native.features.recurrence = counted
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        first = stereo_chunk(2000, 1000, value=.05)
        native.history.push(first, target_pid=42)
        native.feed(first)
        wait_idle(native)
        assert observations[-1][1].lexical is not None
        assert observations[-1][1].lexical.committed_target_end
        assert recurrence_calls == []

        tail = stereo_chunk(3200, 1200, value=.05)
        native.history.push(tail, target_pid=42)
        native.feed(tail)
        wait_idle(native)
        assert recurrence_calls
        assert recurrence_calls[-1][0] == observations[-1][1].lexical.target_end_ms
    finally:
        native.close()


def test_causal_lower_bound_is_explicit_per_target_and_does_not_leak():
    observations = []
    native = make_native(StaticPosterior(), observations)
    first, second, third = Token(10, 1), Token(10, 2), Token(10, 3)
    try:
        native.history.start(42)
        native.begin(first, 'target', causal_lower_bound_ms=1000)
        assert native.causal_lower_bound_ms == 1000
        native.finish_line(first)

        native.begin(second, 'target')
        assert native.causal_lower_bound_ms is None
        native.finish_line(second)

        native.begin(third, 'target', causal_lower_bound_ms=2000)
        assert native.causal_lower_bound_ms == 2000
    finally:
        native.close()


def test_capture_callback_is_not_blocked_by_human_vocal_inference():
    observations = []
    observer = BlockingVocalObserver()
    native = make_native(StaticPosterior(), observations, vocal_observer=observer)
    chunk = stereo_chunk(1600)
    try:
        native.history.start(42)
        native.history.push(chunk, target_pid=42)
        feeder = Thread(target=native.feed, args=(chunk,))
        feeder.start()
        assert observer.started.wait(1)
        feeder.join(.2)
        assert not feeder.is_alive()
        assert native.history.latest_ms == 1600
    finally:
        observer.release.set()
        native.close()


def test_alignment_never_compresses_pcm_gaps_into_one_acoustic_snapshot():
    observations, reports = [], []
    model = HistorySensitivePosterior()
    native = make_native(model, observations, reports=reports)
    token = Token(8, 1)
    try:
        native.history.start(42)
        # Each 400 ms span is too short for this posterior to find the target;
        # concatenating across the 400 ms capture gap would incorrectly create
        # an 800 ms target-bearing snapshot.
        native.history.push(stereo_chunk(1000, 400), target_pid=42)
        native.history.push(stereo_chunk(1800, 400), target_pid=42)
        native.begin(token, 'target')
        wait_idle(native)

        assert model.calls == 2
        assert reports
        assert reports[-1][1]['target_associated'] is False
    finally:
        native.close()


def test_retention_is_search_capacity_not_the_default_alignment_work_unit():
    observations = []
    model = RecordingBlankPosterior()
    native = make_native(model, observations)
    token = Token(9, 1)
    try:
        native.history.start(42)
        for end_ms in range(1000, 60_001, 1000):
            native.history.push(compact_chunk(end_ms), target_pid=42)

        native.begin(token, 'target')
        wait_idle(native)
        initial = tuple(model.durations_ms)
        assert 7_000 <= initial[0] <= 9_000
        assert len(initial) <= 4

        # Once the retained history has been exhausted, new PCM reopens only a
        # bounded overlapping query.  It must not make every 500 ms frontier
        # trigger another complete 60 s scan.
        fresh = compact_chunk(60_500, 500)
        native.history.push(fresh, target_pid=42)
        native.feed(fresh)
        wait_idle(native)
        additional = tuple(model.durations_ms[len(initial):])
        assert additional
        assert max(additional) <= 10_000
    finally:
        native.close()


def test_human_vocal_observers_keep_independent_score_series_and_time_support():
    observer = HumanVocalActivityObserver(
        StaticAudioEventModel(class_index=19, score=.9),
        speech_observer=SilentSpeechObserver())
    history = AcousticObservationHistory()
    frames = observer.observe(stereo_chunk(1240, 480, value=.05))
    history.add_vocal(frames)

    event_frames = [frame for frame in frames if frame.source == 'nonlexical']
    speech_frames = [frame for frame in frames if frame.source == 'speech']
    assert event_frames and speech_frames
    assert event_frames[0].support_start_ms == 760
    vocal = history.summarize_vocal(start_ms=760, end_ms=1240)
    by_source = {item.source: item for item in vocal.series}
    assert by_source['nonlexical'].peak_score == pytest.approx(.9)
    assert by_source['nonlexical'].samples[-1].component_scores[19] == pytest.approx(.9)
    assert by_source['nonlexical'].active_count > 0
    assert by_source['nonlexical'].samples
    assert by_source['nonlexical'].samples[0].support_start_ms == 760
    assert by_source['speech'].active_count == 0
    # No cross-model mean/"combined probability" exists; only the union of
    # independently thresholded time support is exposed for temporal reasoning.
    assert any(item.active_intervals for item in vocal.series)


def test_vocal_history_is_a_queryable_measurement_cache_not_a_boundary_state_machine():
    history = AcousticObservationHistory()
    history.add_vocal(tuple(
        VocalActivityFrame(at_ms=at_ms, score=probability, source='speech')
        for at_ms, probability in (
            (960, .9), (976, .9), (992, .9),
            (1008, .9), (1024, .9), (1040, .1), (1056, .1),
            (1072, .9), (1088, .9),
        )
    ))

    full = history.summarize_vocal(start_ms=950, end_ms=1100)
    clipped = history.summarize_vocal(start_ms=1000, end_ms=1100)
    assert full.series[0].active_intervals == ((960, 1024), (1072, 1088))
    assert clipped.series[0].active_intervals == ((1008, 1024), (1072, 1088))
    # Causal-boundary meaning is intentionally absent from the measurement
    # cache; the assessment request applies that fact later.
    assert not hasattr(full, 'active_across_causal_boundary')
    assert not hasattr(full, 'post_causal_onset_ms')


def test_vocal_recurrence_is_conditioned_on_human_vocal_score_structure():
    def recurrence_for_cycles(cycles, *, cadence_ms=100):
        history = AcousticObservationHistory()
        frames = []
        at = 1000
        for pattern in cycles:
            for score in pattern:
                frames.append(VocalActivityFrame(
                    at_ms=at, score=score, support_start_ms=at - cadence_ms, source='speech'))
                at += cadence_ms
        history.add_vocal(tuple(frames))
        return history.recurrence(start_ms=1000, end_ms=at - cadence_ms)

    repeated = (.92, .88, .2, .1, .86, .9)
    periodic = recurrence_for_cycles((repeated,) * 5)
    flat_finite_risk = recurrence_for_cycles(((.9,) * 6,) * 5)
    changing = recurrence_for_cycles((
        repeated,
        (.75, .25, .8, .15, .3, .9),
        (.2, .9, .35, .8, .1, .4),
        (.85, .15, .45, .9, .25, .7),
        (.3, .75, .1, .55, .9, .2),
    ))

    assert periodic.period_ms is not None
    assert periodic.strength > .75
    assert periodic.recent_strength > .75
    # A sustained flat human-vocal score is persistence, not recurrence; this
    # prevents a long finite cry from being released merely for being steady.
    assert flat_finite_risk.period_ms is None
    assert changing.strength < periodic.strength
