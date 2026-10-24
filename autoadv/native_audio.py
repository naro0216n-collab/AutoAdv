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

from .acoustic_observers import AcousticObservationHistory, TenVadActivityObserver, VocalActivityObserver
from .audio_buffer import AudioHistory, PCMChunk
from .conversation import Token
from .forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from .g2p import PronunciationCandidate
from .process_audio import ProcessCapture, CaptureHealth
from .utterance_progress import (
    LexicalProgressEvidence, NativeAudioObservation, PresentationBoundaryCertainty,
)


class NativeAudio:
    """Own one process capture and continuous observations; never inject input."""

    RAW_HISTORY_MS = 180_000
    MAX_ALIGNMENT_WINDOW_MS = 60_000
    INFERENCE_INTERVAL_MS = 500
    MIN_AUDIO_MS = 300

    def __init__(self, pid: int, helper: Path, model: Path, vocab: Path,
                 on_error: Callable[[Token | None, str], None],
                 *, model_backend=None, g2p=None, clock=None,
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
        self.vocal_observer = vocal_observer if vocal_observer is not None else TenVadActivityObserver()
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
        self.boundary_ms: int | None = None
        self.boundary_certainty = PresentationBoundaryCertainty.SESSION_START
        self.window_start_ms = 0
        self._last_processed_ms = -1
        self._last_processed_chunk: PCMChunk | None = None
        self._inference_at = -1
        self._inflight = False
        self._inflight_generation: int | None = None
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
        # Capture and compact observers begin before the first HOOK.
        self.boundary(certainty=PresentationBoundaryCertainty.SESSION_START)
        self._capture.start()

    def boundary(self, at_ms: int | None = None, *,
                 certainty: PresentationBoundaryCertainty = PresentationBoundaryCertainty.AUTOADV) -> None:
        """Open a Presentation Epoch at a known causal boundary."""
        boundary_ms = self.clock() if at_ms is None else int(at_ms)
        if boundary_ms <= 0:
            raise ValueError("audio boundary must be a positive monotonic timestamp")
        with self._lock:
            self._invalidate_target_locked()
            self.boundary_ms = boundary_ms
            self.boundary_certainty = certainty
            self.window_start_ms = boundary_ms
            # Raw PCM stays a bounded resource cache.  Semantic epoch history
            # is carried separately by compact acoustic features.
            self._reset_observation()

    def external_transition(self) -> None:
        """Record that a new presentation began at an unknown time.

        HOOK arrival is deliberately not substituted for the missing boundary.
        Raw PCM falls back to the bounded capture history while compact features
        keep their own longer history.
        """
        with self._lock:
            self._invalidate_target_locked()
            self.boundary_ms = None
            self.boundary_certainty = PresentationBoundaryCertainty.EXTERNAL_UNKNOWN
            earliest = self.features.earliest_ms
            self.window_start_ms = int(earliest if earliest is not None else max(0, self.clock() - self.RAW_HISTORY_MS))
            self._reset_observation()

    def _invalidate_target_locked(self) -> None:
        self._generation += 1
        self._inflight = False
        self._inflight_generation = None
        self.token = None
        self.aligner = None
        self._last_processed_ms = -1
        self._last_processed_chunk = None

    def _reset_observation(self) -> None:
        self._inference_at = -1
        self._pcm_reported = False
        self._last_pcm_end_ms = -1
        self._capture_stereo_peak = 0.0

    def _raw_analysis_start_locked(self, end_ms: int | None = None) -> tuple[int, bool]:
        if self.boundary_ms is not None:
            semantic_start = self.boundary_ms
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
            self._inflight = False
            self._inflight_generation = None
            self.token = token
            self.aligner = aligner
            self.start_ms = self.clock()
            analysis_end = self.features.latest_ms or self.start_ms
            self.window_start_ms, _ = self._raw_analysis_start_locked(analysis_end)
            self._last_processed_ms = -1
            self._last_processed_chunk = None
            self._reset_observation()

            buffered = self.history.since(self.window_start_ms)
            pending = [self._consume_locked(chunk, replay=True) for chunk in buffered]
            feature_end = self.features.latest_ms
            if buffered:
                samples = self.history.since(self.window_start_ms)
                if samples:
                    self._inflight = True
                    self._inflight_generation = generation
                    self._inference_at = buffered[-1].at_ms
                    pending.append((token, generation, None, samples, aligner))
            elif feature_end is not None:
                # Raw history may have expired while compact evidence remains.
                # Emit the evidence without fabricating lexical alignment.
                observation = self._build_observation_locked(
                    kind="acoustic_features", capture_start_ms=self.window_start_ms,
                    capture_end_ms=feature_end, lexical=None, history_truncated=True)
                pending.append((token, generation, None, None, None, observation))
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
            self._invalidate_target_locked()
            self._reset_observation()
            return True

    def cancel(self) -> None:
        with self._lock:
            self._invalidate_target_locked()
            # Do not erase the Presentation Epoch: a delayed/replacement HOOK may
            # still need the same continuous history.
            self._reset_observation()

    def health(self, status: CaptureHealth) -> None:
        observation = None
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
                        observation = self._build_observation_locked(
                            kind="transport", capture_start_ms=None, capture_end_ms=None,
                            lexical=None, pcm_last_frame_ms=self._last_pcm_end_ms,
                            heartbeat_at_ms=status.at_ms)
                        token = self.token
                        drain = self._idle_drain_locked()
            else:
                self._capture_error = status.reason
                token = self.token
        if observation is not None and token is not None and self.on_observation is not None:
            self.on_observation(token, observation)
        if drain is not None:
            self._dispatch(drain)
        if not status.healthy and token is not None:
            self.on_error(token, status.reason)

    def _idle_drain_locked(self):
        """Use a heartbeat only to schedule analysis of already captured PCM."""
        if (self._closed or self.token is None or self._inflight
                or self._last_pcm_end_ms < 0 or self._last_pcm_end_ms <= self._inference_at):
            return None
        start, _ = self._raw_analysis_start_locked(self._last_pcm_end_ms)
        samples = self.history.since(start)
        if not samples:
            return None
        token, generation, aligner = self.token, self._generation, self.aligner
        self.window_start_ms = start
        self._inflight = True
        self._inflight_generation = generation
        self._inference_at = self._last_pcm_end_ms
        return token, generation, None, samples, aligner

    def capture_status(self) -> dict:
        with self._lock:
            return dict(
                scope="capture_since_start",
                pcm_packets=self._capture_pcm_packets,
                pcm_frames=self._capture_pcm_frames,
                heartbeats=self._capture_heartbeats,
                capture_stereo_peak=round(self._capture_stereo_peak, 6),
                boundary_certainty=self.boundary_certainty.value,
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
        relevance = float(np.mean([frame.probability for frame in frames])) if frames else 0.0
        with self._lock:
            self.features.add_vocal(frames)
            self.features.add_fingerprint(chunk, vocal_relevance=relevance)

    def _consume_locked(self, chunk: PCMChunk, *, replay: bool = False):
        if (self._closed or self.token is None
                or chunk.at_ms < self.window_start_ms
                or chunk.at_ms < self._last_processed_ms
                or (chunk.at_ms == self._last_processed_ms and chunk is self._last_processed_chunk)):
            return None
        self._last_processed_ms = chunk.at_ms
        self._last_processed_chunk = chunk
        self._last_pcm_end_ms = max(self._last_pcm_end_ms, int(chunk.at_ms))
        self._capture_stereo_peak = max(self._capture_stereo_peak, self._chunk_peak(chunk))
        pcm_frames = None
        if not self._pcm_reported:
            self._pcm_reported = True
            pcm_frames = len(chunk.frames) // (chunk.channels * chunk.sample_width_bytes)

        samples = None
        token, generation, aligner = self.token, self._generation, self.aligner
        if not replay:
            enough_pcm = chunk.at_ms - self.window_start_ms >= self.MIN_AUDIO_MS
            inference_due = (self._inference_at < 0
                             or chunk.at_ms - self._inference_at >= self.INFERENCE_INTERVAL_MS)
            if not self._inflight and enough_pcm and inference_due:
                start, _ = self._raw_analysis_start_locked(chunk.at_ms)
                self.window_start_ms = start
                candidate = self.history.since(start)
                if candidate:
                    self._inflight = True
                    self._inflight_generation = generation
                    self._inference_at = chunk.at_ms
                    samples = candidate
        return token, generation, pcm_frames, samples, aligner

    def _dispatch(self, item) -> None:
        if item is None:
            return
        if len(item) == 6:
            token, generation, _pcm_frames, _samples, _aligner, observation = item
            with self._lock:
                if generation != self._generation or token != self.token:
                    return
            if self.on_observation is not None:
                self.on_observation(token, observation)
            return
        token, generation, pcm_frames, samples, aligner = item
        with self._lock:
            if generation != self._generation or token != self.token:
                return
        if pcm_frames is not None and self.on_pcm is not None:
            self.on_pcm(token, pcm_frames)
        if samples:
            submitted_ns = time.perf_counter_ns() if self.on_inference else 0
            try:
                self._pool.submit(self._infer, token, generation, samples, aligner, submitted_ns)
            except RuntimeError:
                with self._lock:
                    if self._inflight_generation == generation:
                        self._inflight = False
                        self._inflight_generation = None

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
            item = self._consume_locked(chunk)
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
            if self.boundary_ms is not None:
                feature_start = self.boundary_ms
            else:
                feature_start = self.features.earliest_ms or max(0, feature_end - self.features.horizon_ms)
            vocal = self.features.summarize_vocal(
                start_ms=feature_start, end_ms=feature_end, epoch_start_ms=self.boundary_ms)
            recurrence = self.features.recurrence(epoch_start_ms=self.boundary_ms, end_ms=feature_end)
        return NativeAudioObservation(
            kind=kind, capture_start_ms=capture_start_ms, capture_end_ms=capture_end_ms,
            lexical=lexical, vocal=vocal, recurrence=recurrence,
            boundary_at_ms=self.boundary_ms,
            boundary_certainty=self.boundary_certainty,
            history_truncated=history_truncated,
            pcm_last_frame_ms=pcm_last_frame_ms,
            heartbeat_at_ms=heartbeat_at_ms,
        )

    def _infer(self, token: Token, generation: int, samples: tuple[PCMChunk, ...],
               aligner: TargetConditionedAligner | None, submitted_ns: int = 0) -> None:
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        try:
            capture_start_ms = int(round(samples[0].at_ms - samples[0].duration_ms)) if samples else None
            capture_end_ms = int(samples[-1].at_ms) if samples else None
            alignment = aligner.align(samples) if aligner is not None else None
            decoded_ns = time.perf_counter_ns() if self.on_inference else 0
            audio_ms = sum(chunk.duration_ms for chunk in samples)
            lexical = None
            target_end_ms = None
            if alignment is not None:
                if alignment.target_end_frame is not None:
                    wall = self._audio_offset_to_wall_ms(
                        samples, alignment.target_end_frame * alignment.frame_ms)
                    if wall is not None:
                        target_end_ms = int(round(wall))
                lexical = LexicalProgressEvidence.from_alignment(alignment, target_end_ms)

            with self._lock:
                if (self._closed or generation != self._generation or token != self.token
                        or aligner is not self.aligner or not self.history.healthy):
                    return
                semantic_start = self.boundary_ms
                history_truncated = bool(
                    semantic_start is not None and capture_start_ms is not None and
                    capture_start_ms > semantic_start + 1)
                observation = self._build_observation_locked(
                    kind="acoustic", capture_start_ms=capture_start_ms,
                    capture_end_ms=capture_end_ms, lexical=lexical,
                    history_truncated=history_truncated,
                    pcm_last_frame_ms=capture_end_ms)

            if self.on_inference is not None:
                metrics = {
                    "queue_ms": round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    "align_ms": round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    "audio_ms": round(audio_ms, 3),
                    "capture_start_ms": capture_start_ms,
                    "capture_end_ms": capture_end_ms,
                    "boundary_certainty": self.boundary_certainty.value,
                    "history_truncated": observation.history_truncated,
                    "vocal_mean_probability": round(observation.vocal.mean_probability, 4) if observation.vocal else 0.0,
                    "vocal_active_fraction": round(observation.vocal.active_fraction, 4) if observation.vocal else 0.0,
                    "vocal_last_active_ms": observation.vocal.last_active_ms if observation.vocal else None,
                    "recurrence_strength": round(observation.recurrence.strength, 4) if observation.recurrence else 0.0,
                    "recurrence_period_ms": observation.recurrence.period_ms if observation.recurrence else None,
                    "recurrence_stability": round(observation.recurrence.period_stability, 4) if observation.recurrence else 0.0,
                    "cross_epoch_persistence": round(observation.recurrence.cross_epoch_persistence, 4) if observation.recurrence else 0.0,
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
            if self.on_observation is not None:
                self.on_observation(token, observation)
        except Exception as exc:
            self.on_error(token, "acoustic inference failure: " + str(exc))
        finally:
            with self._lock:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None

    def close(self) -> None:
        self.cancel()
        with self._lock:
            self._closed = True
            self.features.reset()
        self._capture.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
