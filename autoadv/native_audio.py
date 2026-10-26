"""Continuous process-scoped acoustic observation for UtteranceProgress.

Process Loopback is active for the monitoring session, not only after HOOK
arrival.  Known text is attached retrospectively to retained PCM, while cheap
target-independent vocal/fingerprint measurements are cached continuously.
Target alignment is narrowed as evidence arrives and recurrence is queried only
when direct endpoint evidence remains ambiguous.  This layer never decides
presentation completion and never owns game input or fallback policy.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import Callable
import time

import numpy as np

from .acoustic_observers import AcousticObservationHistory, HumanVocalActivityObserver, VocalActivityObserver
from .audio_buffer import AudioHistory, PCMChunk
from .conversation import Token
from .forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from .g2p import PronunciationCandidate
from .process_audio import ProcessCapture, CaptureHealth
from .utterance_progress import (
    LexicalProgressEvidence, NativeAudioObservation, recurrence_query_start,
)


class NativeAudio:
    """Own one process capture and continuous observations; never inject input."""

    RETENTION_MS = 60_000
    INFERENCE_INTERVAL_MS = 500
    MIN_AUDIO_MS = 300

    def __init__(self, pid: int, helper: Path, model: Path, vocab: Path,
                 on_error: Callable[[Token | None, str], None],
                 *, model_backend=None, g2p=None, clock=None,
                 human_vocal_model: Path | None = None,
                 vocal_observer: VocalActivityObserver | None = None,
                 on_pcm: Callable[[Token, int], None] | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None,
                 on_observation: Callable[[Token, NativeAudioObservation], None] | None = None):
        self.pid = pid
        self.on_error = on_error
        self.on_pcm = on_pcm
        self.on_inference = on_inference
        self.on_observation = on_observation
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.history = AudioHistory(horizon_ms=self.RETENTION_MS)
        self.features = AcousticObservationHistory(horizon_ms=self.RETENTION_MS)
        if vocal_observer is not None:
            self.vocal_observer = vocal_observer
        else:
            if human_vocal_model is None:
                raise FileNotFoundError("managed human-vocal model required")
            self.vocal_observer = HumanVocalActivityObserver(human_vocal_model)
        self.model = model_backend if model_backend is not None else OnnxPhoneCTCModel(model, vocab)
        if g2p is None:
            from .g2p import default_g2p
            g2p = default_g2p()
        self.g2p = g2p
        self._lock = RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoadv-progress")
        self._capture = ProcessCapture(pid, helper, self.history, self.feed, self.health, self.clock)

        self.token: Token | None = None
        self.aligner: TargetConditionedAligner | None = None
        self.start_ms = 0
        self.causal_lower_bound_ms: int | None = None
        self.input_candidates_ms: tuple[int, ...] = ()
        self.target_attached_ms: int | None = None
        self.window_start_ms = 0
        self._committed_lexical: LexicalProgressEvidence | None = None
        self._last_processed_ms = -1
        self._last_processed_chunk: PCMChunk | None = None
        # ``_inflight`` describes the physical single worker, not whether the
        # current generation has work. Invalidating a target cannot cancel an
        # ONNX call already executing in that worker.
        self._inference_at = -1
        self._observation_at = -1
        self._inflight = False
        self._inflight_generation: int | None = None
        self._pending_inference: tuple[Token, int, int, TargetConditionedAligner, int] | None = None
        self._generation = 0
        self._closed = False
        self._capture_pcm_packets = 0
        self._capture_pcm_frames = 0
        self._capture_heartbeats = 0
        self._capture_error: str | None = None
        self._capture_stereo_peak = 0.0
        self._pcm_reported = False
        self._last_pcm_end_ms = -1

    def start(self) -> None:
        # Capture starts before the first HOOK, but monitoring start is not a
        # causal boundary for a game line: the current voice may already be in
        # progress. A lower bound exists after AutoAdv itself successfully sends
        # progression input; monitoring start is never one.
        self._capture.start()

    def _retire_target_locked(self) -> None:
        # In-flight ONNX work cannot be cancelled here. Only its result becomes
        # stale. At most one not-yet-started request is retained and it belongs
        # to the newest target. Generation advances exactly once when ``begin``
        # attaches the next target.
        self._pending_inference = None
        self.token = None
        self.aligner = None
        self.causal_lower_bound_ms = None
        self.input_candidates_ms = ()
        self.target_attached_ms = None
        self._committed_lexical = None
        self._last_processed_ms = -1
        self._last_processed_chunk = None

    def _reset_observation(self) -> None:
        self._inference_at = -1
        self._observation_at = -1
        self._pcm_reported = False
        self._last_pcm_end_ms = -1
        self._capture_stereo_peak = 0.0

    def _retained_start_locked(self, end_ms: int | None = None) -> int:
        if self.history.earliest_ms is not None:
            return int(self.history.earliest_ms)
        if self.features.earliest_ms is not None:
            return int(self.features.earliest_ms)
        if end_ms is None:
            end_ms = self.clock()
        return max(0, int(end_ms) - self.RETENTION_MS)

    def _initial_analysis_start_locked(self, end_ms: int) -> int:
        retained_start = self._retained_start_locked(end_ms)
        if self.causal_lower_bound_ms is not None:
            return max(retained_start, int(self.causal_lower_bound_ms))
        usable_inputs = tuple(
            value for value in self.input_candidates_ms
            if retained_start <= value <= int(end_ms) - self.MIN_AUDIO_MS
        )
        if usable_inputs:
            # External inputs are not promoted to a semantic boundary.  They
            # merely give the first, cheapest search range after the text has
            # actually been admitted.  If the target is not found there, the
            # same retained history is widened on the next assessment.
            return min(usable_inputs)
        return retained_start

    def _history_truncated_locked(self, capture_start_ms: int | None) -> bool:
        if capture_start_ms is None or self.causal_lower_bound_ms is None:
            return False
        return int(capture_start_ms) > int(self.causal_lower_bound_ms) + 1

    def _narrow_alignment_start_locked(
        self,
        lexical: LexicalProgressEvidence,
        *,
        frame_ms: float,
        capture_end_ms: int,
    ) -> None:
        """Keep later target assessments inside the smallest evidenced range.

        A successful AutoAdv input is already a strong lower bound.  For
        external inputs we wait until the known text has actually located
        itself in PCM, then the latest stamp not later than that acoustic start
        is safe as an analysis bound without claiming that the stamp caused the
        transition.  With no usable stamp, the located acoustic start itself is
        enough to avoid rescanning unrelated retained history.
        """
        if not lexical.target_associated or lexical.target_start_ms is None:
            return
        retained_start = self._retained_start_locked(capture_end_ms)
        if self.causal_lower_bound_ms is not None:
            desired = max(retained_start, int(self.causal_lower_bound_ms))
        else:
            target_start = int(lexical.target_start_ms)
            usable = [
                stamp for stamp in self.input_candidates_ms
                if retained_start <= stamp <= target_start
            ]
            if usable:
                desired = max(usable)
            else:
                # Keep one model frame of context so rounding the located onset
                # cannot crop the first phone on the next assessment.
                desired = max(retained_start, target_start - max(1, int(round(frame_ms))))
        self.window_start_ms = max(int(self.window_start_ms), int(desired))

    def begin(self, token: Token, alignment_text: str, *,
              candidates: tuple[PronunciationCandidate, ...] | None = None,
              causal_lower_bound_ms: int | None = None,
              input_candidates_ms: tuple[int, ...] = ()) -> None:
        """Attach delayed known text to the current Presentation Epoch history."""
        lower_bound = int(causal_lower_bound_ms) if causal_lower_bound_ms is not None else None
        if lower_bound is not None and lower_bound <= 0:
            raise ValueError("audio causal lower bound must be a positive monotonic timestamp")
        input_candidates = tuple(sorted({
            int(value) for value in input_candidates_ms if int(value) > 0
        }))
        aligner: TargetConditionedAligner | None = None
        try:
            candidate = TargetConditionedAligner(self.model, g2p=self.g2p)
            candidate.set_target(alignment_text, candidates=candidates)
            aligner = candidate
        except (ValueError, KeyError):
            # Non-lexical material is a first-class case. Failure to construct a
            # lexical target removes only lexical evidence, not native audio.
            aligner = None

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._pending_inference = None
            self.token = token
            self.aligner = aligner
            self.causal_lower_bound_ms = lower_bound
            self.input_candidates_ms = input_candidates
            self.start_ms = self.clock()
            self.target_attached_ms = self.start_ms
            analysis_end = self.features.latest_ms or self.start_ms
            self.window_start_ms = self._initial_analysis_start_locked(int(analysis_end))
            self._committed_lexical = None
            self._last_processed_ms = -1
            self._last_processed_chunk = None
            self._reset_observation()

            buffered = self.history.since(self.window_start_ms)
            pending = []
            for chunk in buffered:
                self._consume_locked(chunk, replay=True)
            if buffered:
                first = buffered[0]
                pending.append((
                    "pcm", token, generation,
                    len(first.frames) // (first.channels * first.sample_width_bytes)))
            feature_end = self.features.latest_ms
            if (buffered and aligner is not None
                    and buffered[-1].at_ms - self.window_start_ms >= self.MIN_AUDIO_MS):
                item = self._request_alignment_locked(buffered[-1].at_ms)
                if item is not None:
                    pending.append(item)
            elif buffered:
                observation = self._feature_observation_locked(
                    token, generation, buffered[-1].at_ms, history_truncated=False)
                if observation is not None:
                    pending.append(observation)
        for item in pending:
            self._dispatch(item)
        with self._lock:
            failure = self._capture_error if self.token == token else None
        if failure:
            self.on_error(token, failure)

    def finish_line(self, token: Token) -> bool:
        with self._lock:
            if token != self.token:
                return False
            self._retire_target_locked()
            self._reset_observation()
            return True

    def cancel(self) -> None:
        with self._lock:
            if self.token is not None or self.aligner is not None or self._pending_inference is not None:
                self._retire_target_locked()
            # Do not erase the Presentation Epoch: a delayed/replacement HOOK may
            # still need the same continuous history.
            self._reset_observation()

    def health(self, status: CaptureHealth) -> None:
        token = None
        drain = None
        with self._lock:
            if status.healthy:
                if status.pcm_frames:
                    self._capture_pcm_packets += 1
                    self._capture_pcm_frames += status.pcm_frames
                else:
                    self._capture_heartbeats += 1
                    if self.token is not None and self._last_pcm_end_ms >= 0:
                        token = self.token
                        drain = self._idle_drain_locked()
            else:
                self._capture_error = status.reason
                token = self.token
        if drain is not None:
            self._dispatch(drain)
        if not status.healthy and token is not None:
            self.on_error(token, status.reason)

    def _idle_drain_locked(self):
        """Use a heartbeat only to request the newest already-captured snapshot."""
        if self._closed or self.token is None or self._last_pcm_end_ms < 0:
            return None
        if self.aligner is not None:
            if self._last_pcm_end_ms <= self._inference_at:
                return None
            return self._request_alignment_locked(self._last_pcm_end_ms, queue_if_busy=True)
        if self._last_pcm_end_ms <= self._observation_at:
            return None
        return self._feature_observation_locked(
            self.token, self._generation, self._last_pcm_end_ms,
            history_truncated=False)

    def observation_fence(self, token: Token) -> int | None:
        """Snapshot the newest captured PCM timestamp for one current target.

        The value is only a synchronization fence. It does not mean that the
        PCM contains speech, silence, or an utterance endpoint.
        """
        with self._lock:
            if self._closed or token != self.token or self._last_pcm_end_ms < 0:
                return None
            return int(self._last_pcm_end_ms)

    def request_assessment_through(self, token: Token, through_ms: int) -> bool:
        """Request one assessment of the retained PCM snapshot through a fence."""
        item = None
        with self._lock:
            if self._closed or token != self.token:
                return False
            boundary = min(int(through_ms), self._last_pcm_end_ms)
            if boundary < 0:
                return False
            if (self.aligner is not None
                    and boundary - self.window_start_ms >= self.MIN_AUDIO_MS):
                item = self._request_alignment_locked(boundary, queue_if_busy=True)
            else:
                item = self._feature_observation_locked(
                    token, self._generation, boundary, history_truncated=False)
        self._dispatch(item)
        return item is not None

    def capture_status(self) -> dict:
        with self._lock:
            return dict(
                scope="capture_since_start",
                pcm_packets=self._capture_pcm_packets,
                pcm_frames=self._capture_pcm_frames,
                heartbeats=self._capture_heartbeats,
                capture_stereo_peak=round(self._capture_stereo_peak, 6),
                causal_lower_bound_ms=self.causal_lower_bound_ms,
                failure=self._capture_error,
            )

    @staticmethod
    def _chunk_peak(chunk: PCMChunk) -> float:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            return 0.0
        values = np.frombuffer(chunk.frames, dtype="<f4")
        return float(np.max(np.abs(values))) if values.size else 0.0

    def _observe_continuous(self, chunk: PCMChunk) -> None:
        frames = self.vocal_observer.observe(chunk)
        with self._lock:
            self.features.add_vocal(frames)
            self.features.add_fingerprint(chunk)

    def _start_alignment_locked(self, token: Token, generation: int, boundary: int,
                                aligner: TargetConditionedAligner,
                                requested_ns: int = 0):
        if (self._closed or token != self.token or generation != self._generation
                or aligner is not self.aligner):
            return None
        start = max(self._retained_start_locked(boundary), int(self.window_start_ms))
        samples = tuple(
            chunk for chunk in self.history.since(start)
            if chunk.at_ms <= boundary)
        if not samples:
            return None
        self.window_start_ms = start
        self._inflight = True
        self._inflight_generation = generation
        return ("infer", token, generation, samples, aligner, requested_ns)

    def _request_alignment_locked(self, boundary: int, *, queue_if_busy: bool = False):
        if (self._closed or self.token is None or self.aligner is None):
            return None
        boundary = int(boundary)
        if boundary <= self._inference_at:
            return None
        token, generation, aligner = self.token, self._generation, self.aligner
        requested_ns = time.perf_counter_ns() if self.on_inference else 0
        if self._inflight:
            # Intermediate snapshots are observations, not obligations.  When
            # the same target already has ONNX work in flight, continuously
            # queueing every 500 ms makes the worker permanently lag behind the
            # PCM frontier.  Drop those redundant intermediate requests without
            # advancing ``_inference_at``: a later explicit fence must still be
            # able to request that PCM frontier.  A generation replacement or
            # synchronization fence keeps exactly one newest pending request.
            if queue_if_busy or self._inflight_generation != generation:
                self._inference_at = boundary
                self._pending_inference = (
                    token, generation, boundary, aligner, requested_ns)
            return None
        self._inference_at = boundary
        return self._start_alignment_locked(
            token, generation, boundary, aligner, requested_ns)

    def _feature_observation_locked(self, token: Token, generation: int,
                                    boundary: int, *, history_truncated: bool):
        if (self._closed or token != self.token or generation != self._generation):
            return None
        boundary = int(boundary)
        if boundary <= self._observation_at:
            return None
        observation = self._build_observation_locked(
            kind="snapshot",
            capture_start_ms=self.window_start_ms,
            capture_end_ms=boundary,
            lexical=self._committed_lexical,
            history_truncated=(
                history_truncated or self._history_truncated_locked(self.window_start_ms)
            ),
            pcm_last_frame_ms=boundary,
        )
        self._observation_at = boundary
        return ("observe", token, generation, observation)

    def _take_pending_locked(self):
        pending, self._pending_inference = self._pending_inference, None
        if pending is None:
            return None
        token, generation, boundary, aligner, requested_ns = pending
        if (self._closed or token != self.token or generation != self._generation
                or aligner is not self.aligner):
            return None
        return self._start_alignment_locked(
            token, generation, boundary, aligner, requested_ns)

    def _consume_locked(self, chunk: PCMChunk, *, replay: bool = False):
        if (self._closed or self.token is None
                or chunk.at_ms < self.window_start_ms
                or chunk.at_ms < self._last_processed_ms
                or (chunk.at_ms == self._last_processed_ms and chunk is self._last_processed_chunk)):
            return ()
        self._last_processed_ms = chunk.at_ms
        self._last_processed_chunk = chunk
        self._last_pcm_end_ms = max(self._last_pcm_end_ms, int(chunk.at_ms))
        self._capture_stereo_peak = max(self._capture_stereo_peak, self._chunk_peak(chunk))
        pcm_frames = None
        if not self._pcm_reported:
            self._pcm_reported = True
            pcm_frames = len(chunk.frames) // (chunk.channels * chunk.sample_width_bytes)

        if replay:
            return ()

        token, generation = self.token, self._generation
        items = []
        if pcm_frames is not None:
            items.append(("pcm", token, generation, pcm_frames))

        enough_pcm = chunk.at_ms - self.window_start_ms >= self.MIN_AUDIO_MS
        if not enough_pcm:
            return tuple(items)

        if self.aligner is not None:
            inference_due = (self._inference_at < 0
                             or chunk.at_ms - self._inference_at >= self.INFERENCE_INTERVAL_MS)
            if inference_due:
                item = self._request_alignment_locked(chunk.at_ms)
                if item is not None:
                    items.append(item)
        else:
            observation_due = (self._observation_at < 0
                               or chunk.at_ms - self._observation_at >= self.INFERENCE_INTERVAL_MS)
            if observation_due:
                item = self._feature_observation_locked(
                    token, generation, chunk.at_ms, history_truncated=False)
                if item is not None:
                    items.append(item)
        return tuple(items)

    def _dispatch(self, item) -> None:
        if item is None:
            return
        kind = item[0]
        if kind == "observe":
            _, token, generation, observation = item
            with self._lock:
                if generation != self._generation or token != self.token:
                    return
            if self.on_observation is not None:
                self.on_observation(token, observation)
            return
        if kind == "pcm":
            _, token, generation, pcm_frames = item
            with self._lock:
                if generation != self._generation or token != self.token:
                    return
            if self.on_pcm is not None:
                self.on_pcm(token, pcm_frames)
            return
        if kind != "infer":
            raise RuntimeError(f"unknown native-audio work item: {kind}")
        _, token, generation, samples, aligner, requested_ns = item
        next_item = None
        with self._lock:
            if generation != self._generation or token != self.token:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None
                    next_item = self._take_pending_locked()
        if next_item is not None:
            self._dispatch(next_item)
            return
        with self._lock:
            if generation != self._generation or token != self.token:
                return
        try:
            self._pool.submit(self._infer, token, generation, samples, aligner, requested_ns)
        except RuntimeError:
            with self._lock:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None
                    next_item = self._take_pending_locked()
            if next_item is not None:
                self._dispatch(next_item)

    def feed(self, chunk: PCMChunk) -> None:
        try:
            self._observe_continuous(chunk)
        except Exception as exc:
            # A broken vocal observer invalidates progression evidence; do not
            # silently degrade to lexical-only evidence.
            with self._lock:
                token = self.token
            if token is not None:
                self.on_error(token, "human-vocal observer failure: " + str(exc))
            return
        with self._lock:
            items = self._consume_locked(chunk)
        for item in items:
            self._dispatch(item)

    @staticmethod
    def _audio_offset_to_wall_ms(samples: tuple[PCMChunk, ...], offset_ms: float) -> float | None:
        if offset_ms < 0:
            return None
        consumed = 0.0
        for chunk in samples:
            duration = chunk.duration_ms
            if offset_ms <= consumed + duration + 1e-6:
                inside = max(0.0, min(duration, offset_ms - consumed))
                return (chunk.at_ms - duration) + inside
            consumed += duration
        return None

    def _build_observation_locked(self, *, kind: str, capture_start_ms: int | None,
                                  capture_end_ms: int | None,
                                  lexical: LexicalProgressEvidence | None,
                                  history_truncated: bool = False,
                                  pcm_last_frame_ms: int | None = None,
                                  heartbeat_at_ms: int | None = None) -> NativeAudioObservation:
        feature_end = capture_end_ms if capture_end_ms is not None else self.features.latest_ms
        vocal = None
        if feature_end is not None:
            if lexical is not None and lexical.target_start_ms is not None:
                feature_start = int(lexical.target_start_ms)
            elif self.causal_lower_bound_ms is not None:
                feature_start = int(self.causal_lower_bound_ms)
            elif self.input_candidates_ms:
                feature_start = int(min(self.input_candidates_ms))
            else:
                feature_start = None
            if feature_start is not None:
                feature_start = min(int(feature_start), int(feature_end))
                vocal = self.features.summarize_vocal(
                    start_ms=feature_start, end_ms=feature_end)

        observation = NativeAudioObservation(
            kind=kind, capture_start_ms=capture_start_ms, capture_end_ms=capture_end_ms,
            lexical=lexical, vocal=vocal, recurrence=None,
            causal_lower_bound_ms=self.causal_lower_bound_ms,
            input_candidates_ms=self.input_candidates_ms,
            target_attached_ms=self.target_attached_ms,
            history_truncated=history_truncated,
            pcm_last_frame_ms=pcm_last_frame_ms,
            heartbeat_at_ms=heartbeat_at_ms,
        )
        recurrence_start = recurrence_query_start(observation)
        if recurrence_start is None or feature_end is None:
            return observation
        recurrence = self.features.recurrence(
            start_ms=min(int(recurrence_start), int(feature_end)),
            end_ms=int(feature_end))
        return NativeAudioObservation(
            kind=kind, capture_start_ms=capture_start_ms, capture_end_ms=capture_end_ms,
            lexical=lexical, vocal=vocal, recurrence=recurrence,
            causal_lower_bound_ms=self.causal_lower_bound_ms,
            input_candidates_ms=self.input_candidates_ms,
            target_attached_ms=self.target_attached_ms,
            history_truncated=history_truncated,
            pcm_last_frame_ms=pcm_last_frame_ms,
            heartbeat_at_ms=heartbeat_at_ms,
        )

    def _infer(self, token: Token, generation: int, samples: tuple[PCMChunk, ...],
               aligner: TargetConditionedAligner | None, submitted_ns: int = 0) -> None:
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        observation = None
        alignment = None
        target_end_ms = None
        current_for_metrics = False
        retry_wider_search = False
        follow_up_item = None
        try:
            capture_start_ms = int(round(samples[0].at_ms - samples[0].duration_ms)) if samples else None
            capture_end_ms = int(samples[-1].at_ms) if samples else None
            alignment = aligner.align(samples) if aligner is not None else None
            decoded_ns = time.perf_counter_ns() if self.on_inference else 0
            audio_ms = sum(chunk.duration_ms for chunk in samples)
            lexical = None
            target_start_ms = None
            aligned_end_ms = None
            target_end_ms = None
            if alignment is not None:
                if alignment.target_unit_spans:
                    wall = self._audio_offset_to_wall_ms(
                        samples, alignment.target_unit_spans[0].start * alignment.frame_ms)
                    if wall is not None:
                        target_start_ms = int(round(wall))
                    wall = self._audio_offset_to_wall_ms(
                        samples, alignment.target_unit_spans[-1].end * alignment.frame_ms)
                    if wall is not None:
                        aligned_end_ms = int(round(wall))
                if alignment.target_end_frame is not None:
                    wall = self._audio_offset_to_wall_ms(
                        samples, alignment.target_end_frame * alignment.frame_ms)
                    if wall is not None:
                        target_end_ms = int(round(wall))
                lexical = LexicalProgressEvidence.from_alignment(
                    alignment, target_start_ms=target_start_ms,
                    aligned_end_ms=aligned_end_ms, target_end_ms=target_end_ms)

            with self._lock:
                if (self._closed or generation != self._generation or token != self.token
                        or aligner is not self.aligner or not self.history.healthy):
                    current_for_metrics = False
                else:
                    current_for_metrics = True
                    retained_start = self._retained_start_locked(capture_end_ms)
                    if (alignment is not None and not alignment.target_associated
                            and self.causal_lower_bound_ms is None
                            and self.input_candidates_ms
                            and int(self.window_start_ms) > retained_start):
                        # The cheap candidate-anchored search did not locate the
                        # target.  Widen once to retained evidence instead of
                        # declaring the candidate range authoritative.
                        self.window_start_ms = retained_start
                        retry_wider_search = True
                        if capture_end_ms is not None:
                            self._pending_inference = (
                                token, generation, int(capture_end_ms), aligner,
                                time.perf_counter_ns() if self.on_inference else 0,
                            )
                    elif lexical is not None and alignment is not None:
                        self._narrow_alignment_start_locked(
                            lexical, frame_ms=alignment.frame_ms,
                            capture_end_ms=int(capture_end_ms or self.clock()))
                    # Keep every published assessment tied to the exact PCM
                    # snapshot used by its target-conditioned measurement. A
                    # newer feature snapshot is a separate query, not a reason
                    # to rewrite this result's time frontier.
                    if not retry_wider_search:
                        observation_end_ms = capture_end_ms
                        history_truncated = self._history_truncated_locked(capture_start_ms)
                        observation = self._build_observation_locked(
                            kind="snapshot", capture_start_ms=capture_start_ms,
                            capture_end_ms=observation_end_ms, lexical=lexical,
                            history_truncated=history_truncated,
                            pcm_last_frame_ms=observation_end_ms,
                        )
                        if observation_end_ms is not None:
                            self._observation_at = max(self._observation_at, observation_end_ms)
                        if (lexical is not None and alignment is not None
                                and alignment.committed_target_end):
                            # A committed target end already contains the
                            # aligner's right-context/boundary evidence.  Future
                            # PCM changes only the physical tail assessment; do
                            # not repeatedly remap the same known text over an
                            # ever-growing window.
                            self._committed_lexical = lexical
                            pending_boundary = (
                                self._pending_inference[2]
                                if self._pending_inference is not None
                                and self._pending_inference[0] == token
                                and self._pending_inference[1] == generation
                                else None
                            )
                            self.aligner = None
                            self._pending_inference = None
                            if (pending_boundary is not None and observation_end_ms is not None
                                    and int(pending_boundary) > int(observation_end_ms)):
                                follow_up_item = self._feature_observation_locked(
                                    token, generation, int(pending_boundary),
                                    history_truncated=False)

            if current_for_metrics and observation is not None and self.on_inference is not None:
                metrics = {
                    "queue_ms": round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    "align_ms": round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    "audio_ms": round(audio_ms, 3),
                    "capture_start_ms": capture_start_ms,
                    "capture_end_ms": capture_end_ms,
                    "observation_end_ms": observation.capture_end_ms,
                    "causal_lower_bound_ms": self.causal_lower_bound_ms,
                    "input_candidates_ms": list(self.input_candidates_ms),
                    "target_attached_ms": self.target_attached_ms,
                    "history_truncated": observation.history_truncated,
                    "vocal_series": ([{
                        "source": item.source,
                        "frame_count": item.frame_count,
                        "active_count": item.active_count,
                        "peak_score": round(item.peak_score, 4),
                        "active_score_threshold": item.active_score_threshold,
                        "measurement_support_start_ms": min(
                            (sample.support_start_ms for sample in item.samples), default=None),
                        "measurement_support_end_ms": max(
                            (sample.at_ms for sample in item.samples), default=None),
                        "active_intervals": list(item.active_intervals),
                    } for item in observation.vocal.series] if observation.vocal else []),
                    "recurrence_strength": round(observation.recurrence.strength, 4) if observation.recurrence else 0.0,
                    "recurrence_period_ms": observation.recurrence.period_ms if observation.recurrence else None,
                    "recurrence_stability": round(observation.recurrence.period_stability, 4) if observation.recurrence else 0.0,
                    "recent_recurrence_strength": round(observation.recurrence.recent_strength, 4) if observation.recurrence else 0.0,
                    "recurrence_level_consistency": round(observation.recurrence.level_consistency, 4) if observation.recurrence else 0.0,
                    "recurrence_low_vocal_reference_similarity": round(observation.recurrence.low_vocal_reference_similarity, 4) if observation.recurrence else 0.0,
                }
                if alignment is not None:
                    fit = alignment.fit
                    metrics.update({
                        "frame_count": alignment.frame_count,
                        "target_units": alignment.target_units,
                        "aligned_target_units": alignment.aligned_target_units,
                        "target_coverage": round(alignment.coverage, 4),
                        "target_associated": alignment.target_associated,
                        "complete_alignment": alignment.complete_alignment,
                        "committed_target_end": alignment.committed_target_end,
                        "target_end_frame": alignment.target_end_frame,
                        "reading_rank": alignment.reading_rank,
                        "path_margin": round(fit.path_margin, 4),
                        "boundary_margin": round(fit.boundary_margin, 4),
                        "lexical_target_end_ms": target_end_ms,
                        "rejection": alignment.rejection,
                    })
                try:
                    self.on_inference(token, metrics)
                except Exception:
                    pass
            if current_for_metrics and observation is not None and self.on_observation is not None:
                self.on_observation(token, observation)
            if follow_up_item is not None:
                self._dispatch(follow_up_item)
        except Exception as exc:
            self.on_error(token, "acoustic inference failure: " + str(exc))
        finally:
            next_item = None
            with self._lock:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None
                    next_item = self._take_pending_locked()
            if next_item is not None:
                self._dispatch(next_item)

    def close(self) -> None:
        self.cancel()
        with self._lock:
            self._closed = True
            self.features.reset()
        self._capture.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
