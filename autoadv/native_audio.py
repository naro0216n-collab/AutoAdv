"""Continuous process-scoped acoustic observation for UtteranceProgress.

Process Loopback is active for the monitoring session, not only after HOOK
arrival.  Known text is attached retrospectively to retained PCM, while vocal
activity and recurrence features are observed continuously.  This layer never
decides presentation completion and never owns game input or fallback policy.
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
from .utterance_progress import LexicalProgressEvidence, NativeAudioObservation


class NativeAudio:
    """Own one process capture and continuous observations; never inject input."""

    RAW_HISTORY_MS = 180_000
    MAX_ALIGNMENT_WINDOW_MS = 60_000
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
        self.history = AudioHistory(horizon_ms=self.RAW_HISTORY_MS)
        self.features = AcousticObservationHistory()
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
        self._next_causal_lower_bound_ms: int | None = None
        self.target_attached_ms: int | None = None
        self.window_start_ms = 0
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
        self._lexical_committed_generation: int | None = None
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

    def set_causal_lower_bound(self, at_ms: int | None = None) -> None:
        """Record an observed input lower bound for the next target's audio.

        The bound limits retained-audio identification; it does not assert the
        exact voice start time. AutoAdv-owned input installs it only after the
        targeted Windows input call succeeds.
        """
        lower_bound_ms = self.clock() if at_ms is None else int(at_ms)
        if lower_bound_ms <= 0:
            raise ValueError("audio causal lower bound must be a positive monotonic timestamp")
        with self._lock:
            # The successful input belongs to the transition *after* the line
            # that just completed. Recording that fact must not mutate worker
            # state or create an intermediate target generation.
            self._next_causal_lower_bound_ms = lower_bound_ms

    def external_transition(self) -> None:
        """Record that the new target has no observed input lower bound.

        HOOK arrival remains a separate factual timestamp and is never promoted
        to the missing bound. Retained audio stays available for known-text
        association where that association can be established independently.
        """
        with self._lock:
            # A replacement HOOK without an observed input invalidates any
            # pending AutoAdv lower bound. ``begin`` performs the one actual
            # target-generation transition.
            self._next_causal_lower_bound_ms = None

    def _retire_target_locked(self) -> None:
        # In-flight ONNX work cannot be cancelled here. Only its result becomes
        # stale. At most one not-yet-started request is retained and it belongs
        # to the newest target. Generation advances exactly once when ``begin``
        # attaches the next target.
        self._pending_inference = None
        self._lexical_committed_generation = None
        self.token = None
        self.aligner = None
        self.causal_lower_bound_ms = None
        self.target_attached_ms = None
        self._last_processed_ms = -1
        self._last_processed_chunk = None

    def _reset_observation(self) -> None:
        self._inference_at = -1
        self._observation_at = -1
        self._pcm_reported = False
        self._last_pcm_end_ms = -1
        self._capture_stereo_peak = 0.0

    def _raw_analysis_start_locked(self, end_ms: int | None = None) -> tuple[int, bool]:
        if self.causal_lower_bound_ms is not None:
            semantic_start = self.causal_lower_bound_ms
        else:
            semantic_start = self.features.earliest_ms or max(0, self.clock() - self.RAW_HISTORY_MS)
        if end_ms is None:
            end_ms = self.features.latest_ms or self.clock()
        resource_start = max(0, int(end_ms) - self.MAX_ALIGNMENT_WINDOW_MS)
        start = max(int(semantic_start), resource_start)
        return start, start > int(semantic_start)

    def begin(self, token: Token, alignment_text: str, *,
              candidates: tuple[PronunciationCandidate, ...] | None = None) -> None:
        """Attach delayed known text to the current Presentation Epoch history."""
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
            self._lexical_committed_generation = None
            self.token = token
            self.aligner = aligner
            self.causal_lower_bound_ms = self._next_causal_lower_bound_ms
            self._next_causal_lower_bound_ms = None
            self.start_ms = self.clock()
            self.target_attached_ms = self.start_ms
            analysis_end = self.features.latest_ms or self.start_ms
            self.window_start_ms, _ = self._raw_analysis_start_locked(analysis_end)
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
            if buffered and aligner is not None:
                item = self._request_alignment_locked(buffered[-1].at_ms)
                if item is not None:
                    pending.append(item)
            elif buffered:
                observation = self._feature_observation_locked(
                    token, generation, buffered[-1].at_ms, history_truncated=False)
                if observation is not None:
                    pending.append(observation)
            elif feature_end is not None:
                # Raw history may have expired while compact evidence remains.
                # Emit the evidence without fabricating lexical alignment.
                observation = self._feature_observation_locked(
                    token, generation, feature_end, history_truncated=True)
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
            self._next_causal_lower_bound_ms = None
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
        """Use a heartbeat only to schedule analysis of already captured PCM."""
        if self._closed or self.token is None or self._last_pcm_end_ms < 0:
            return None
        if (self.aligner is not None
                and self._lexical_committed_generation != self._generation):
            if self._last_pcm_end_ms <= self._inference_at:
                return None
            return self._request_alignment_locked(self._last_pcm_end_ms)
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

    def request_observation_through(self, token: Token, through_ms: int) -> bool:
        """Publish continuous evidence through an already captured PCM fence.

        This synchronization request never starts target-conditioned alignment.
        A fallback deadline must account for human-vocal evidence that was
        already captured, but it is not a second lexical-analysis scheduler.
        """
        item = None
        with self._lock:
            if self._closed or token != self.token:
                return False
            boundary = min(int(through_ms), self._last_pcm_end_ms)
            if boundary < 0 or self._observation_at >= boundary:
                return False
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
        # A spectral fingerprint spans the entire process-audio packet. One
        # strong human-vocal observation inside that packet is enough to make
        # it relevant to recurrence; averaging sparse AudioSet evidence into
        # dense VAD frames would erase exactly the non-lexical evidence added
        # by the second acoustic measurement.
        relevance = max((frame.probability for frame in frames), default=0.0)
        with self._lock:
            self.features.add_vocal(frames)
            self.features.add_fingerprint(chunk, vocal_relevance=relevance)

    def _start_alignment_locked(self, token: Token, generation: int, boundary: int,
                                aligner: TargetConditionedAligner,
                                requested_ns: int = 0):
        if (self._closed or token != self.token or generation != self._generation
                or aligner is not self.aligner
                or self._lexical_committed_generation == generation):
            return None
        start, _ = self._raw_analysis_start_locked(boundary)
        samples = tuple(
            chunk for chunk in self.history.since(start)
            if chunk.at_ms <= boundary)
        if not samples:
            return None
        self.window_start_ms = start
        self._inflight = True
        self._inflight_generation = generation
        return ("infer", token, generation, samples, aligner, requested_ns)

    def _request_alignment_locked(self, boundary: int):
        if (self._closed or self.token is None or self.aligner is None
                or self._lexical_committed_generation == self._generation):
            return None
        boundary = int(boundary)
        if boundary <= self._inference_at:
            return None
        self._inference_at = boundary
        token, generation, aligner = self.token, self._generation, self.aligner
        requested_ns = time.perf_counter_ns() if self.on_inference else 0
        if self._inflight:
            # One physical worker means queued generations can only make the
            # newest target older. Keep a single latest request instead.
            self._pending_inference = (
                token, generation, boundary, aligner, requested_ns)
            return None
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
            kind="acoustic_features",
            capture_start_ms=self.window_start_ms,
            capture_end_ms=boundary,
            lexical=None,
            history_truncated=history_truncated,
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
        if self._lexical_committed_generation == generation:
            return self._feature_observation_locked(
                token, generation, boundary, history_truncated=False)
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

        if (self.aligner is not None
                and self._lexical_committed_generation != generation):
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
        recurrence = None
        if feature_end is not None:
            if lexical is not None and lexical.target_associated and lexical.target_start_ms is not None:
                feature_start = lexical.target_start_ms
            elif self.causal_lower_bound_ms is not None:
                feature_start = self.causal_lower_bound_ms
            elif self.target_attached_ms is not None:
                # Target attachment is not a presentation boundary.  It is only
                # a factual floor for new non-lexical observations when no
                # independent causal lower bound exists.
                feature_start = self.target_attached_ms
            else:
                feature_start = self.features.earliest_ms or max(0, feature_end - self.features.horizon_ms)
            feature_start = min(int(feature_start), int(feature_end))
            vocal = self.features.summarize_vocal(
                start_ms=feature_start, end_ms=feature_end,
                causal_boundary_ms=self.causal_lower_bound_ms)

            if lexical is not None and lexical.target_end_ms is not None:
                recurrence_start = lexical.target_end_ms
            elif lexical is not None and lexical.target_start_ms is not None:
                recurrence_start = lexical.target_start_ms
            elif self.causal_lower_bound_ms is not None:
                recurrence_start = self.causal_lower_bound_ms
            else:
                recurrence_start = feature_start
            recurrence_start = min(int(recurrence_start), int(feature_end))
            recurrence = self.features.recurrence(
                start_ms=recurrence_start, end_ms=feature_end)
        return NativeAudioObservation(
            kind=kind, capture_start_ms=capture_start_ms, capture_end_ms=capture_end_ms,
            lexical=lexical, vocal=vocal, recurrence=recurrence,
            causal_lower_bound_ms=self.causal_lower_bound_ms,
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
                    if (alignment is not None and alignment.complete_alignment
                            and alignment.committed_target_end):
                        self._lexical_committed_generation = generation
                    observation_end_ms = capture_end_ms
                    if observation_end_ms is not None and self._observation_at >= 0:
                        # A lightweight deadline/feature snapshot may already
                        # have advanced continuous evidence beyond the PCM used
                        # by this lexical inference. Publish the late lexical
                        # fact on that newer acoustic frontier instead of
                        # regressing UtteranceProgress in time.
                        observation_end_ms = max(observation_end_ms, self._observation_at)
                    semantic_start = self.causal_lower_bound_ms
                    history_truncated = bool(
                        semantic_start is not None and capture_start_ms is not None and
                        capture_start_ms > semantic_start + 1)
                    observation = self._build_observation_locked(
                        kind="acoustic", capture_start_ms=capture_start_ms,
                        capture_end_ms=observation_end_ms, lexical=lexical,
                        history_truncated=history_truncated,
                        pcm_last_frame_ms=observation_end_ms,
                    )
                    if observation_end_ms is not None:
                        self._observation_at = max(self._observation_at, observation_end_ms)

            if current_for_metrics and observation is not None and self.on_inference is not None:
                metrics = {
                    "queue_ms": round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    "align_ms": round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    "audio_ms": round(audio_ms, 3),
                    "capture_start_ms": capture_start_ms,
                    "capture_end_ms": capture_end_ms,
                    "observation_end_ms": observation.capture_end_ms,
                    "causal_lower_bound_ms": self.causal_lower_bound_ms,
                    "target_attached_ms": self.target_attached_ms,
                    "history_truncated": observation.history_truncated,
                    "vocal_mean_probability": round(observation.vocal.mean_probability, 4) if observation.vocal else 0.0,
                    "vocal_active_fraction": round(observation.vocal.active_fraction, 4) if observation.vocal else 0.0,
                    "vocal_last_active_ms": observation.vocal.last_active_ms if observation.vocal else None,
                    "vocal_active_across_causal_boundary": (
                        observation.vocal.active_across_causal_boundary if observation.vocal else False),
                    "vocal_post_causal_onset_ms": (
                        observation.vocal.post_causal_onset_ms if observation.vocal else None),
                    "vocal_active_intervals": (
                        list(observation.vocal.active_intervals) if observation.vocal else []),
                    "recurrence_strength": round(observation.recurrence.strength, 4) if observation.recurrence else 0.0,
                    "recurrence_period_ms": observation.recurrence.period_ms if observation.recurrence else None,
                    "recurrence_stability": round(observation.recurrence.period_stability, 4) if observation.recurrence else 0.0,
                    "recent_recurrence_strength": round(observation.recurrence.recent_strength, 4) if observation.recurrence else 0.0,
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
