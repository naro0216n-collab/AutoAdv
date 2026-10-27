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



class SnapshotSensitivePosterior:
    """Return different target evidence when the analyzed PCM snapshot grows."""

    frame_stride_ms = 20.0
    vocab = VOCAB

    def infer(self, chunks):
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

    def infer(self, chunks):
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


class PeriodicVocalObserver:
    """Emit a target-independent repeating human-vocal score pattern."""

    pattern = (.92, .88, .2, .1, .86, .9)
    cadence_ms = 100

    def observe(self, chunk):
        start = int(round(chunk.at_ms - chunk.duration_ms))
        first = ((start // self.cadence_ms) + 1) * self.cadence_ms
        return tuple(
            VocalActivityFrame(
                at_ms=t,
                score=self.pattern[(t // self.cadence_ms) % len(self.pattern)],
                support_start_ms=t - self.cadence_ms,
                observer='speech',
            )
            for t in range(first, int(chunk.at_ms) + 1, self.cadence_ms)
        )


class RecordingVocalObserver(StaticVocalObserver):
    def __init__(self):
        self.observed_until = []

    def observe(self, chunk):
        self.observed_until.append(int(chunk.at_ms))
        return super().observe(chunk)


class BlockingPosterior(StaticPosterior):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()

    def infer(self, chunks):
        self.started.set()
        self.release.wait(2)
        return self.logits


class BlockingBlankPosterior(BlockingPosterior):
    def __init__(self):
        super().__init__()
        self.logits[:] = -4.0
        self.logits[:, 0] = 5.0
        self.initial_capture_start_ms = None
        self.widened_started = Event()
        self.widened_release = Event()

    def infer(self, chunks):
        capture_start_ms = min(chunk.start_ms for chunk in chunks)
        if self.initial_capture_start_ms is None:
            self.initial_capture_start_ms = capture_start_ms
            self.started.set()
            self.release.wait(2)
        elif capture_start_ms < self.initial_capture_start_ms:
            self.widened_started.set()
            self.widened_release.wait(2)
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


class RecordingWeakPosterior(RecordingBlankPosterior):
    """Record searches while producing only an unassociated target fragment."""

    def infer(self, chunks):
        logits = super().infer(chunks)
        frame = max(0, logits.shape[0] - 5)
        logits[frame, 0] = 0.0
        logits[frame, VOCAB['a']] = 7.0
        return logits


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


def wait_until(predicate, message='native audio result did not arrive') -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.002)
    raise TimeoutError(message)


def make_native(model, observations, errors=None, reports=None, *, clock=lambda: 1000, vocal_observer=None):
    return NativeAudio(
        42, Path('unused-helper.dll'), Path('unused.onnx'), Path('unused.json'),
        lambda token, reason: (errors if errors is not None else []).append((token, reason)),
        model_backend=model, g2p=StaticG2P(), clock=clock, vocal_observer=vocal_observer or StaticVocalObserver(),
        on_progress=lambda token, progress, item: observations.append((token, progress, item)),
        on_inference=(lambda token, item: reports.append((token, item))) if reports is not None else None,
    )



def test_monitoring_start_does_not_invent_a_target_causal_boundary(monkeypatch):
    import autoadv.native_audio as native_audio_module

    class CaptureBoundary:
        instances = []

        def __init__(self, pid, helper, history, on_chunk, on_health, clock):
            self.started = False
            self.closed = False
            self.__class__.instances.append(self)

        def start(self):
            self.started = True

        def close(self):
            self.closed = True

    monkeypatch.setattr(native_audio_module, 'ProcessCapture', CaptureBoundary)
    observations = []
    native = make_native(StaticPosterior(), observations, clock=lambda: 1000)
    try:
        native.start()
        assert CaptureBoundary.instances[-1].started
        assert native.causal_lower_bound_ms is None
    finally:
        native.close()
    assert CaptureBoundary.instances[-1].closed

def test_alignment_scheduler_keeps_fallback_fence_separate_from_history_discovery():
    observations = []
    model = BlockingBlankPosterior()
    observer = RecordingVocalObserver()
    native = make_native(model, observations, vocal_observer=observer, clock=lambda: 20_000)
    token = Token(1, 1)
    try:
        native.history.start(42)
        retained = compact_chunk(20_000, 20_000)
        native.history.push(retained, target_pid=42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        assert model.started.wait(1)
        wait_until(lambda: observer.observed_until and observer.observed_until[-1] >= 20_000)

        # The captured frontier is not assessed before its bounded lexical query.
        assert not observations

        # Completing that bounded query acknowledges the current evidence even
        # though older retained history remains searchable. The optional backward
        # search may start immediately afterward but cannot withhold UNKNOWN.
        model.release.set()
        wait_until(lambda: any(
            observed == token and progress.evidence_at_ms == 20_000
            for observed, progress, _item in observations))
        assert model.widened_started.wait(1)

        # New captured PCM gets priority once the already-running bounded history
        # query finishes; the scheduler does not chain retained-history exhaustion
        # ahead of a required newer assessment.
        fresh = compact_chunk(20_600, 600)
        native.history.push(fresh, target_pid=42)
        native.feed(fresh)
        native.request_assessment_through(token, 20_600)
        time.sleep(.02)
        assert not any(progress.evidence_at_ms == 20_600
                       for _observed, progress, _item in observations)

        model.widened_release.set()
        wait_until(lambda: any(
            observed == token and progress.evidence_at_ms == 20_600
            for observed, progress, _item in observations))

        # An already-assessed fence remains an idempotent obligation.
        before = len(observations)
        native.request_assessment_through(token, 20_000)
        wait_until(lambda: any(
            progress.evidence_at_ms == 20_000
            for _observed, progress, _item in observations[before:]))
    finally:
        model.release.set()
        model.widened_release.set()
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
        wait_until(lambda: bool(observations[before_replacement:]))

        assert observations[before_replacement:]
        assert all(token == newest for token, _progress, _item in observations[before_replacement:])
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
        wait_until(lambda: bool(observations) and observations[-1][2].lexical is not None)
        item = observations[-1][2]
        assert item.lexical is not None
        assert item.lexical.target_end_ms == 1620
        assert item.lexical.complete_alignment and item.lexical.committed_target_end
        assert item.vocal is not None

        before = len(observations)
        native.health(CaptureHealth(2100, True, ''))
        time.sleep(.02)
        assert len(observations) == before
    finally:
        native.close()


def test_pcm_timeline_preserves_causal_cropping_and_discontinuity_for_alignment():
    observations, reports = [], []
    model = HistorySensitivePosterior()
    observer = RecordingVocalObserver()
    native = make_native(
        model, observations, reports=reports, vocal_observer=observer, clock=lambda: 2200)
    token = Token(4, 2)
    try:
        native.history.start(42)
        # The causal lower bound falls inside retained PCM.  A later transport
        # gap is part of the same timeline contract and must be visible to both
        # target-conditioned alignment and the streaming observer.
        chunks = (
            stereo_chunk(900, 200),      # 700..900
            stereo_chunk(1400, 500),     # 900..1400
            stereo_chunk(2200, 400),     # 1800..2200: discontinuous
        )
        for chunk in chunks:
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
        wait_until(lambda: observer.observed_until and observer.observed_until[-1] >= 2200)

        native.begin(token, 'target', causal_lower_bound_ms=1000)
        wait_until(lambda: bool(reports) and bool(observations))

        assert reports[0][1]['capture_start_ms'] == 1000
        # Each contiguous span is too short for HistorySensitivePosterior to
        # associate the target.  Association here would mean the gap was
        # compressed away before alignment.
        assert not any(item['target_associated'] for _token, item in reports)
        # Target-independent measurements on both sides of the transport gap
        # remain factual cache entries; the gap is not filled with invented PCM.
        vocal = native.features.summarize_vocal(start_ms=700, end_ms=2200)
        times = tuple(sample.at_ms for series in vocal.series for sample in series.samples)
        assert min(times) < 1000 < max(times)
        assert not any(1400 < at_ms < 1800 for at_ms in times)
    finally:
        native.close()

def test_external_input_candidates_narrow_initial_search_without_becoming_causal_bounds():
    observations, reports = [], []
    native = make_native(StaticPosterior(), observations, reports=reports, clock=lambda: 2000)
    token = Token(4, 3)
    try:
        native.history.start(42)
        for chunk in (stereo_chunk(1000, 300), stereo_chunk(1500, 500)):
            native.history.push(chunk, target_pid=42)
            native.feed(chunk)
        native.begin(token, 'target', input_candidates_ms=(1200, 1400))
        wait_until(lambda: bool(observations))
        item = observations[-1][2]
        assert item.causal_lower_bound_ms is None
        assert item.input_candidates_ms == (1200, 1400)
        assert reports[0][1]["capture_start_ms"] == 1200
    finally:
        native.close()


def test_delayed_hook_attachment_does_not_change_retained_acoustic_meaning():
    def observe_at(hook_clock: int):
        observations = []
        native = make_native(
            StaticPosterior(), observations, clock=lambda: hook_clock,
            vocal_observer=ActiveVocalObserver())
        token = Token(6, 1)
        try:
            native.history.start(42)
            # Target-independent observation starts before the causal bound.
            # Alignment may crop raw PCM at that bound, but locating the text
            # must not crop the already-measured activity series and recreate
            # the same physical run as a post-boundary onset.
            for chunk in (stereo_chunk(1300, 500), stereo_chunk(1600, 300)):
                native.history.push(chunk, target_pid=42)
                native.feed(chunk)
            native.begin(token, 'target', causal_lower_bound_ms=1000)
            wait_until(lambda: any(progress.evidence_at_ms == 1600 for _token, progress, _item in observations))
            return [item for _token, progress, item in observations if progress.evidence_at_ms == 1600][-1]
        finally:
            native.close()

    early = observe_at(1700)
    late = observe_at(5200)
    assert early.lexical == late.lexical
    assert early.vocal == late.vocal
    assert early.recurrence == late.recurrence
    assert early.vocal is not None
    assert min(
        sample.at_ms for series in early.vocal.series for sample in series.samples
    ) < early.causal_lower_bound_ms


def test_candidate_search_widens_only_until_target_context_is_located():
    observations, reports = [], []
    native = make_native(HistorySensitivePosterior(), observations, reports=reports, clock=lambda: 20_000)
    token = Token(7, 1)
    long_target = (PronunciationCandidate(('a', 'b', 'c', 'a', 'b', 'c')),)
    try:
        native.history.start(42)
        retained = compact_chunk(20_000, 20_000)
        native.history.push(retained, target_pid=42)
        native.begin(
            token, 'target', candidates=long_target,
            causal_lower_bound_ms=1000, input_candidates_ms=(19_700,),
        )
        wait_until(lambda: any(
            item.lexical is not None and item.lexical.target_associated
            for _observed, _progress, item in observations))

        associated_report = next(
            item for _token, item in reports if item.get('target_associated'))
        located_start = associated_report['capture_start_ms']

        # The external input is only a search seed. A miss expands into older
        # retained PCM, but each discovery query remains bounded.
        assert reports[0][1]['capture_start_ms'] == 19_700
        assert located_start < 19_700
        assert associated_report['audio_ms'] <= NativeAudio.INITIAL_SEARCH_WINDOW_MS

        # Once a bounded snapshot has actually located the target, later PCM is
        # followed from that proven context rather than from the much older causal
        # lower bound or the whole retained history.
        before = len(reports)
        fresh = compact_chunk(20_500, 500)
        native.history.push(fresh, target_pid=42)
        native.feed(fresh)
        wait_until(lambda: len(reports) > before)
        follow = reports[before][1]
        assert follow['capture_start_ms'] == located_start
        assert follow['capture_start_ms'] > 1000
    finally:
        native.close()


def test_heartbeat_is_transport_health_only_and_never_advances_acoustic_evidence():
    observations = []
    model = BlockingPosterior()
    native = make_native(model, observations)
    token = Token(8, 1)
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        chunk = stereo_chunk(1600, 600)
        native.history.push(chunk, target_pid=42)
        native.feed(chunk)
        assert model.started.wait(1)
        assert native.observation_fence(token) == 1600

        # Transport liveness at a later wall-clock time is not new PCM and cannot
        # become a later acoustic-evidence frontier.
        native.health(CaptureHealth(1700, True, ''))
        assert native.observation_fence(token) == 1600
        model.release.set()
        wait_until(lambda: bool(observations))
        assert all(progress.evidence_at_ms is None or progress.evidence_at_ms <= 1600
                   for observed, progress, _item in observations if observed == token)
    finally:
        model.release.set()
        native.close()


def test_committed_target_mapping_is_reused_while_later_pcm_only_updates_endpoint_evidence():
    observations = []
    native = make_native(SnapshotSensitivePosterior(), observations)
    token = Token(9, 1)
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        first = stereo_chunk(2000, 1000)
        native.history.push(first, target_pid=42)
        native.feed(first)
        wait_until(lambda: bool(observations) and observations[-1][2].lexical is not None
                   and observations[-1][2].lexical.committed_target_end)
        first_progress, first_snapshot = observations[-1][1], observations[-1][2]
        assert first_progress.evidence_at_ms == 2000
        assert first_snapshot.lexical is not None
        assert first_snapshot.lexical.committed_target_end

        tail = stereo_chunk(2600, 600)
        native.history.push(tail, target_pid=42)
        native.feed(tail)
        wait_until(lambda: bool(observations) and observations[-1][1].evidence_at_ms == 2600)
        second_progress, second_snapshot = observations[-1][1], observations[-1][2]
        assert second_progress.evidence_at_ms == 2600
        assert second_snapshot.lexical == first_snapshot.lexical
    finally:
        native.close()


def test_recurrence_remains_optional_evidence_when_direct_endpoint_risk_stays_open():
    observations = []
    native = make_native(
        StaticPosterior(frames=49, starts=(10, 20, 30)), observations,
        vocal_observer=PeriodicVocalObserver())
    token = Token(9, 2)
    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        first = stereo_chunk(2000, 1000, value=.05)
        native.history.push(first, target_pid=42)
        native.feed(first)
        wait_until(lambda: bool(observations) and observations[-1][2].lexical is not None
                   and observations[-1][2].lexical.committed_target_end)
        first_snapshot = observations[-1][2]
        assert first_snapshot.lexical is not None
        assert first_snapshot.lexical.committed_target_end

        tail = stereo_chunk(4000, 2000, value=.05)
        native.history.push(tail, target_pid=42)
        native.feed(tail)
        wait_until(lambda: any(
            item.recurrence is not None and item.recurrence.period_ms is not None
            for _observed, _progress, item in observations))
        recurrent = next(
            item.recurrence for _observed, _progress, item in reversed(observations)
            if item.recurrence is not None and item.recurrence.period_ms is not None)
        assert recurrent.period_ms is not None
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


def test_capture_callback_is_not_blocked_by_acoustic_analysis():
    observations = []
    native = make_native(
        StaticPosterior(frames=49, starts=(10, 20, 30)), observations,
        vocal_observer=ActiveVocalObserver())
    token = Token(11, 1)
    recurrence_started = Event()
    recurrence_release = Event()
    original_recurrence = native.features.recurrence

    def blocking_recurrence(*, start_ms, end_ms, observer=None):
        recurrence_started.set()
        recurrence_release.wait(2)
        return original_recurrence(
            start_ms=start_ms, end_ms=end_ms, observer=observer)

    try:
        native.history.start(42)
        native.begin(token, 'target', causal_lower_bound_ms=1000)
        first = stereo_chunk(2000, 1000, value=.05)
        native.history.push(first, target_pid=42)
        native.feed(first)
        wait_until(lambda: bool(observations) and observations[-1][2].lexical is not None
                   and observations[-1][2].lexical.committed_target_end)

        native.features.recurrence = blocking_recurrence
        tail = stereo_chunk(3200, 1200, value=.05)
        native.history.push(tail, target_pid=42)
        native.feed(tail)
        assert recurrence_started.wait(1)

        newest = stereo_chunk(3400, 200, value=.05)
        native.history.push(newest, target_pid=42)
        feeder = Thread(target=native.feed, args=(newest,))
        feeder.start()
        feeder.join(.2)
        assert not feeder.is_alive()
        assert native.history.latest_ms == 3400
    finally:
        recurrence_release.set()
        native.close()


def test_retention_is_search_capacity_not_the_default_alignment_work_unit():
    observations, reports = [], []
    model = RecordingWeakPosterior()
    native = make_native(model, observations, reports=reports)
    token = Token(9, 1)
    try:
        native.history.start(42)
        for end_ms in range(1000, 60_001, 1000):
            native.history.push(compact_chunk(end_ms), target_pid=42)

        native.begin(token, 'target')
        wait_until(lambda: reports and min(
            item['capture_start_ms'] for _token, item in reports) <= 1000
            and any(item.lexical is not None for _token, _progress, item in observations))
        initial = tuple(model.durations_ms)
        initial_reports = len(reports)
        weak_starts = [
            item.lexical.target_start_ms
            for _token, _progress, item in observations
            if item.lexical is not None and item.lexical.aligned_target_units > 0
            and not item.lexical.target_associated and item.lexical.target_start_ms is not None
        ]
        assert initial and weak_starts
        # Retention can be searched to its old edge without ever becoming one
        # giant HuBERT inference request.
        assert max(initial) <= NativeAudio.INITIAL_SEARCH_WINDOW_MS

        # New PCM reopens only the bounded current window. An unassociated phone
        # fragment remains a measurement and never becomes a persistent floor.
        weak_start = max(weak_starts)
        fresh = compact_chunk(60_500, 500)
        native.history.push(fresh, target_pid=42)
        native.feed(fresh)
        wait_until(lambda: len(reports) > initial_reports)
        additional = tuple(model.durations_ms[len(initial):])
        additional_reports = reports[initial_reports:]
        assert additional and additional_reports
        assert additional_reports[0][1]['capture_start_ms'] < weak_start
        assert max(additional) <= NativeAudio.INITIAL_SEARCH_WINDOW_MS
    finally:
        native.close()


def test_human_vocal_observers_keep_independent_score_series_and_time_support():
    observer = HumanVocalActivityObserver(
        StaticAudioEventModel(class_index=19, score=.9),
        speech_observer=SilentSpeechObserver())
    history = AcousticObservationHistory()
    frames = observer.observe(stereo_chunk(1240, 480, value=.05))
    history.add_vocal(frames)

    event_frames = [frame for frame in frames if frame.observer == 'nonlexical']
    speech_frames = [frame for frame in frames if frame.observer == 'speech']
    assert event_frames and speech_frames
    assert event_frames[0].support_start_ms == 760
    vocal = history.summarize_vocal(start_ms=760, end_ms=1240)
    by_observer = {item.observer: item for item in vocal.series}
    assert max(sample.score for sample in by_observer['nonlexical'].samples) == pytest.approx(.9)
    assert by_observer['nonlexical'].samples[-1].component_scores[19] == pytest.approx(.9)
    assert any(sample.score >= by_observer['nonlexical'].active_score_threshold for sample in by_observer['nonlexical'].samples)
    assert by_observer['nonlexical'].samples
    assert by_observer['nonlexical'].samples[0].support_start_ms == 760
    assert not any(sample.score >= by_observer['speech'].active_score_threshold for sample in by_observer['speech'].samples)
    # No cross-model mean/combined probability or cached event interval is created.
    assert all(item.samples for item in vocal.series)


def test_vocal_history_is_a_queryable_measurement_cache_not_a_boundary_state_machine():
    history = AcousticObservationHistory()
    history.add_vocal(tuple(
        VocalActivityFrame(at_ms=at_ms, score=probability, observer='speech')
        for at_ms, probability in (
            (960, .9), (976, .9), (992, .9),
            (1008, .9), (1024, .9), (1040, .1), (1056, .1),
            (1072, .9), (1088, .9),
        )
    ))

    full = history.summarize_vocal(start_ms=950, end_ms=1100)
    clipped = history.summarize_vocal(start_ms=1000, end_ms=1100)
    assert tuple(sample.at_ms for sample in full.series[0].samples) == (
        960, 976, 992, 1008, 1024, 1040, 1056, 1072, 1088)
    assert tuple(sample.at_ms for sample in clipped.series[0].samples) == (
        1008, 1024, 1040, 1056, 1072, 1088)
    # The cache stores measurements, not derived event/boundary labels.


def test_vocal_recurrence_is_conditioned_on_human_vocal_score_structure():
    def recurrence_for_cycles(cycles, *, cadence_ms=100):
        history = AcousticObservationHistory()
        frames = []
        at = 1000
        for pattern in cycles:
            for score in pattern:
                frames.append(VocalActivityFrame(
                    at_ms=at, score=score, support_start_ms=at - cadence_ms, observer='speech'))
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
    # A sustained flat human-vocal score is persistence, not recurrence; this
    # prevents a long finite cry from being released merely for being steady.
    assert flat_finite_risk.period_ms is None
    assert changing.strength < periodic.strength
    assert changing.recent_strength < periodic.recent_strength
