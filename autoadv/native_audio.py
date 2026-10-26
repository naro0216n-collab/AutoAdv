"""Process-scoped acoustic measurement over one retained PCM timeline.

Capture transport owns only raw timestamped PCM.  Target-independent human-vocal
observation and target-conditioned alignment consume that timeline independently;
neither can block capture or recreate a second PCM frontier.  This layer emits
measurements and one integrated UtteranceProgress result, but never owns game
input or fallback policy.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable
import time

import numpy as np

from .acoustic_observers import (
    AcousticObservationHistory, HumanVocalActivityObserver, VocalActivityObserver,
)
from .audio_buffer import AudioHistory, PCMChunk
from .conversation import Token
from .forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner, TargetTimeAlignment
from .g2p import PronunciationCandidate
from .process_audio import CaptureHealth, ProcessCapture
from .utterance_progress import (
    LexicalProgressEvidence, NativeAudioObservation, UtteranceProgress,
    evaluate_utterance_progress,
)


@dataclass(frozen=True)
class _AlignmentResult:
    alignment: TargetTimeAlignment | None
    samples: tuple[PCMChunk, ...]
    ambiguous: bool = False


class NativeAudio:
    """Own one process PCM timeline and its independent measurement schedulers."""

    RETENTION_MS = 60_000
    INFERENCE_INTERVAL_MS = 500
    MIN_AUDIO_MS = 300
    # Scheduler budget only.  It is not a Presentation/audio-duration contract:
    # unsuccessful searches expand geometrically until the retained floor.
    INITIAL_SEARCH_WINDOW_MS = 8_000
    FEATURE_CONTEXT_LOOKBACK_MS = 8_000

    def __init__(self, pid: int, helper: Path, model: Path, vocab: Path,
                 on_error: Callable[[Token | None, str], None],
                 *, model_backend=None, g2p=None, clock=None,
                 human_vocal_model: Path | None = None,
                 vocal_observer: VocalActivityObserver | None = None,
                 on_pcm: Callable[[Token, int], None] | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None,
                 on_progress: Callable[[Token, UtteranceProgress, NativeAudioObservation], None] | None = None):
        self.pid = pid
        self.on_error = on_error
        self.on_pcm = on_pcm
        self.on_inference = on_inference
        self.on_progress = on_progress
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
        self._alignment_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoadv-align")
        self._observer_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoadv-vocal")
        self._capture = ProcessCapture(pid, helper, self.history, self.feed, self.health, self.clock)

        self.token: Token | None = None
        self.aligner: TargetConditionedAligner | None = None
        self.causal_lower_bound_ms: int | None = None
        self.input_candidates_ms: tuple[int, ...] = ()
        self.target_attached_ms: int | None = None
        self._semantic_search_floor_ms = 0
        self._search_floor_ms = 0
        self._search_start_ms = 0
        self._search_exhausted_at: int | None = None
        self._latest_lexical: LexicalProgressEvidence | None = None
        self._committed_lexical: LexicalProgressEvidence | None = None
        self._lexical_capture_start_ms: int | None = None
        self._lexical_revision = 0

        self._alignment_inflight = False
        self._alignment_generation: int | None = None
        self._pending_alignment: tuple[Token, int, int, TargetConditionedAligner, int] | None = None
        self._last_alignment_query: tuple[int, int] | None = None

        # Capture callbacks append references only.  A single observer worker
        # drains this queue; references are bounded to the same retention horizon
        # as the PCM timeline, so slow analysis cannot turn into unbounded memory.
        self._observer_queue: deque[PCMChunk] = deque()
        self._observer_inflight = False
        self._observer_reset_pending = False
        self._observer_epoch = 0
        self._observer_frontier_ms: int | None = None
        self._required_assessment_ms: int | None = None
        self._last_progress_key: tuple[int, int | None, int] | None = None

        self._generation = 0
        self._closed = False
        self._capture_pcm_packets = 0
        self._capture_pcm_frames = 0
        self._capture_heartbeats = 0
        self._capture_error: str | None = None
        self._capture_stereo_peak = 0.0
        self._pcm_reported_generation: int | None = None

    def start(self) -> None:
        # Monitoring start is not a Presentation boundary.  Capture and target-
        # independent observation deliberately begin before the first HOOK.
        self._capture.start()

    def _retained_start_locked(self, end_ms: int | None = None) -> int:
        earliest = self.history.earliest_ms
        if earliest is not None:
            return int(earliest)
        if end_ms is None:
            end_ms = self.clock()
        return max(0, int(end_ms) - self.RETENTION_MS)

    def _initial_search_start_locked(self, end_ms: int) -> int:
        retained = self._retained_start_locked(end_ms)
        floor = retained
        if self.causal_lower_bound_ms is not None:
            floor = max(floor, int(self.causal_lower_bound_ms))
        self._semantic_search_floor_ms = floor
        self._search_floor_ms = floor
        start = max(floor, int(end_ms) - self.INITIAL_SEARCH_WINDOW_MS)
        usable = [
            value for value in self.input_candidates_ms
            if floor <= value <= int(end_ms) - self.MIN_AUDIO_MS
        ]
        if usable:
            # This is a cheap search seed only.  Failure expands behind it; the
            # selected stamp never becomes a Presentation boundary.
            start = max(start, max(usable))
        return min(start, int(end_ms))

    def _expand_search_locked(self, boundary: int) -> bool:
        current = max(self._search_floor_ms, int(self._search_start_ms))
        if current <= self._search_floor_ms:
            self._search_exhausted_at = int(boundary)
            return False
        span = max(self.MIN_AUDIO_MS, int(boundary) - current)
        expanded = max(
            self._search_floor_ms,
            current - max(self.INITIAL_SEARCH_WINDOW_MS, span),
        )
        if expanded >= current:
            self._search_exhausted_at = int(boundary)
            return False
        self._search_start_ms = expanded
        self._search_exhausted_at = None
        return True

    def _prepare_new_audio_search_locked(self, boundary: int) -> None:
        """After an exhausted search, inspect only evidence that can be new.

        History before the previous exhausted frontier has already been queried.
        Keep overlap for a target that was only beginning there, but do not turn
        retention into a repeated whole-history workload.
        """
        exhausted = self._search_exhausted_at
        if exhausted is None or int(boundary) <= exhausted:
            return
        retained = self._retained_start_locked(boundary)
        semantic_floor = max(retained, int(self._semantic_search_floor_ms))
        lexical = self._latest_lexical
        if (lexical is not None and lexical.aligned_target_units > 0
                and lexical.target_start_ms is not None):
            overlap_floor = int(lexical.target_start_ms) - self.INITIAL_SEARCH_WINDOW_MS
        else:
            overlap_floor = int(exhausted) - self.INITIAL_SEARCH_WINDOW_MS
        self._search_floor_ms = max(semantic_floor, overlap_floor)
        self._search_start_ms = max(
            self._search_floor_ms,
            int(boundary) - self.INITIAL_SEARCH_WINDOW_MS,
        )
        self._search_exhausted_at = None

    def _narrow_search_locked(
        self, lexical: LexicalProgressEvidence, *, frame_ms: float, capture_end_ms: int,
    ) -> None:
        if not lexical.target_associated or lexical.target_start_ms is None:
            return
        retained = self._retained_start_locked(capture_end_ms)
        if self.causal_lower_bound_ms is not None:
            desired = max(retained, int(self.causal_lower_bound_ms))
        else:
            target_start = int(lexical.target_start_ms)
            usable = [
                stamp for stamp in self.input_candidates_ms
                if retained <= stamp <= target_start
            ]
            if usable:
                desired = max(usable)
            else:
                desired = max(retained, target_start - max(1, int(round(frame_ms))))
        self._semantic_search_floor_ms = max(self._semantic_search_floor_ms, retained)
        self._search_floor_ms = max(self._search_floor_ms, int(desired))
        self._search_start_ms = max(int(self._search_start_ms), int(desired))

    def begin(self, token: Token, alignment_text: str, *,
              candidates: tuple[PronunciationCandidate, ...] | None = None,
              causal_lower_bound_ms: int | None = None,
              input_candidates_ms: tuple[int, ...] = ()) -> None:
        """Attach known text to retained PCM without replaying capture state."""
        lower_bound = int(causal_lower_bound_ms) if causal_lower_bound_ms is not None else None
        if lower_bound is not None and lower_bound <= 0:
            raise ValueError("audio causal lower bound must be a positive monotonic timestamp")
        input_candidates = tuple(sorted({
            int(value) for value in input_candidates_ms if int(value) > 0
        }))
        aligner: TargetConditionedAligner | None
        try:
            candidate = TargetConditionedAligner(self.model, g2p=self.g2p)
            candidate.set_target(alignment_text, candidates=candidates)
            aligner = candidate
        except (ValueError, KeyError):
            # Non-lexical material remains a first-class audio case.
            aligner = None

        work = None
        progress_item = None
        pcm_notice = None
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._pending_alignment = None
            self.token = token
            self.aligner = aligner
            self.causal_lower_bound_ms = lower_bound
            self.input_candidates_ms = input_candidates
            self.target_attached_ms = int(self.clock())
            self._latest_lexical = None
            self._committed_lexical = None
            self._lexical_capture_start_ms = None
            self._lexical_revision += 1
            self._last_alignment_query = None
            self._last_progress_key = None
            self._required_assessment_ms = None
            self._pcm_reported_generation = None

            frontier = self.history.latest_ms
            end = int(frontier if frontier is not None else self.target_attached_ms)
            self._search_start_ms = self._initial_search_start_locked(end)
            self._search_exhausted_at = None

            retained = self.history.slice(self._search_start_ms, frontier) if frontier is not None else ()
            if retained:
                first = retained[0]
                self._pcm_reported_generation = generation
                pcm_notice = (
                    "pcm", token, generation,
                    len(first.frames) // (first.channels * first.sample_width_bytes),
                )
            if (frontier is not None and aligner is not None
                    and frontier - self._search_start_ms >= self.MIN_AUDIO_MS):
                work = self._request_alignment_locked(int(frontier))
            elif frontier is not None:
                progress_item = self._progress_item_locked(int(frontier), force=True)

        self._dispatch(pcm_notice)
        self._dispatch(work)
        self._dispatch(progress_item)
        with self._lock:
            failure = self._capture_error if self.token == token else None
        if failure:
            self.on_error(token, failure)

    def _retire_target_locked(self) -> None:
        self._pending_alignment = None
        self.token = None
        self.aligner = None
        self.causal_lower_bound_ms = None
        self.input_candidates_ms = ()
        self.target_attached_ms = None
        self._latest_lexical = None
        self._committed_lexical = None
        self._lexical_capture_start_ms = None
        self._required_assessment_ms = None
        self._last_progress_key = None

    def finish_line(self, token: Token) -> bool:
        with self._lock:
            if token != self.token:
                return False
            self._retire_target_locked()
            return True

    def cancel(self) -> None:
        with self._lock:
            if self.token is not None or self.aligner is not None or self._pending_alignment is not None:
                self._retire_target_locked()

    def health(self, status: CaptureHealth) -> None:
        work = None
        token = None
        with self._lock:
            if status.healthy:
                if status.pcm_frames:
                    self._capture_pcm_packets += 1
                    self._capture_pcm_frames += status.pcm_frames
                else:
                    self._capture_heartbeats += 1
                    token = self.token
                    if token is not None:
                        work = self._idle_drain_locked()
            else:
                self._capture_error = status.reason
                token = self.token
                self.features.reset()
                self._observer_queue.clear()
                self._observer_frontier_ms = None
                self._observer_reset_pending = True
                self._observer_epoch += 1
        self._dispatch(work)
        if not status.healthy and token is not None:
            self.on_error(token, status.reason)

    def _idle_drain_locked(self):
        frontier = self.history.latest_ms
        if self._closed or self.token is None or frontier is None:
            return None
        if self.aligner is not None:
            return self._request_alignment_locked(int(frontier), queue_if_busy=True)
        return self._progress_item_locked(int(frontier), force=True)

    def observation_fence(self, token: Token) -> int | None:
        """Snapshot the raw PCM timeline frontier for synchronization only."""
        with self._lock:
            if self._closed or token != self.token:
                return None
            frontier = self.history.latest_ms
            return int(frontier) if frontier is not None else None

    def request_assessment_through(self, token: Token, through_ms: int) -> bool:
        work = None
        progress_item = None
        schedule_observer = False
        with self._lock:
            if self._closed or token != self.token:
                return False
            frontier = self.history.latest_ms
            if frontier is None:
                return False
            boundary = min(int(through_ms), int(frontier))
            if self.aligner is not None:
                work = self._request_alignment_locked(boundary, queue_if_busy=True)
            self._required_assessment_ms = max(
                boundary,
                self._required_assessment_ms if self._required_assessment_ms is not None else boundary,
            )
            if self._observer_frontier_ms is not None and self._observer_frontier_ms >= boundary:
                progress_item = self._progress_item_locked(boundary, force=True)
            elif self._observer_queue and not self._observer_inflight:
                self._observer_inflight = True
                schedule_observer = True
        if schedule_observer:
            self._submit_observer()
        self._dispatch(work)
        self._dispatch(progress_item)
        return bool(work is not None or progress_item is not None or schedule_observer or self._alignment_inflight)

    def capture_status(self) -> dict:
        with self._lock:
            return dict(
                scope="capture_since_start",
                pcm_packets=self._capture_pcm_packets,
                pcm_frames=self._capture_pcm_frames,
                heartbeats=self._capture_heartbeats,
                capture_stereo_peak=round(self._capture_stereo_peak, 6),
                causal_lower_bound_ms=self.causal_lower_bound_ms,
                pcm_frontier_ms=self.history.latest_ms,
                observer_frontier_ms=self._observer_frontier_ms,
                failure=self._capture_error,
            )

    @staticmethod
    def _chunk_peak(chunk: PCMChunk) -> float:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            return 0.0
        values = np.frombuffer(chunk.frames, dtype="<f4")
        return float(np.max(np.abs(values))) if values.size else 0.0

    def feed(self, chunk: PCMChunk) -> None:
        """Capture callback: queue analysis and return without running a model."""
        work = None
        schedule_observer = False
        with self._lock:
            if self._closed:
                return
            self._observer_queue.append(chunk)
            cutoff = int(chunk.at_ms) - self.RETENTION_MS
            dropped = False
            while self._observer_queue and self._observer_queue[0].at_ms < cutoff:
                self._observer_queue.popleft()
                dropped = True
            if dropped:
                self._observer_reset_pending = True
                self._observer_epoch += 1
            if not self._observer_inflight:
                self._observer_inflight = True
                schedule_observer = True

            if self.token is not None and self.aligner is not None:
                self._prepare_new_audio_search_locked(int(chunk.at_ms))
                query = self._last_alignment_query
                last_boundary = query[1] if query is not None else -1
                if (int(chunk.at_ms) - self._search_start_ms >= self.MIN_AUDIO_MS
                        and (last_boundary < 0
                             or int(chunk.at_ms) - last_boundary >= self.INFERENCE_INTERVAL_MS)):
                    work = self._request_alignment_locked(int(chunk.at_ms))
        if schedule_observer:
            self._submit_observer()
        self._dispatch(work)

    def _submit_observer(self) -> None:
        try:
            self._observer_pool.submit(self._drain_observer)
        except RuntimeError:
            with self._lock:
                self._observer_inflight = False

    def _drain_observer(self) -> None:
        error: Exception | None = None
        try:
            while True:
                with self._lock:
                    if self._closed:
                        self._observer_queue.clear()
                        self._observer_inflight = False
                        return
                    if not self._observer_queue:
                        self._observer_inflight = False
                        return
                    chunk = self._observer_queue.popleft()
                    reset_needed = self._observer_reset_pending
                    self._observer_reset_pending = False
                    observer_epoch = self._observer_epoch
                if reset_needed:
                    reset = getattr(self.vocal_observer, "reset", None)
                    if callable(reset):
                        reset()
                    with self._lock:
                        self.features.reset()
                        self._observer_frontier_ms = None
                frames = self.vocal_observer.observe(chunk)
                progress_item = None
                pcm_notice = None
                with self._lock:
                    if self._closed or observer_epoch != self._observer_epoch:
                        continue
                    self.features.add_vocal(frames)
                    self._observer_frontier_ms = int(chunk.at_ms)
                    self._capture_stereo_peak = max(
                        self._capture_stereo_peak, self._chunk_peak(chunk))
                    if (self.token is not None
                            and self._pcm_reported_generation != self._generation):
                        self._pcm_reported_generation = self._generation
                        pcm_notice = (
                            "pcm", self.token, self._generation,
                            len(chunk.frames) // (chunk.channels * chunk.sample_width_bytes),
                        )
                    if self.token is not None:
                        required = self._required_assessment_ms
                        due = (
                            required is not None and int(chunk.at_ms) >= required
                        )
                        last_boundary = self._last_progress_key[0] if self._last_progress_key else -1
                        periodic = int(chunk.at_ms) - last_boundary >= self.INFERENCE_INTERVAL_MS
                        if due or periodic:
                            boundary = min(
                                int(self.history.latest_ms or chunk.at_ms),
                                int(required) if due and required is not None else int(chunk.at_ms),
                            )
                            progress_item = self._progress_item_locked(boundary, force=due)
                            if due and required is not None and int(chunk.at_ms) >= required:
                                self._required_assessment_ms = None
                self._dispatch(pcm_notice)
                self._dispatch(progress_item)
        except Exception as exc:  # observer failure invalidates physical-risk evidence
            error = exc
        finally:
            if error is not None:
                with self._lock:
                    token = self.token
                    self._observer_queue.clear()
                    self._observer_inflight = False
                if token is not None:
                    self.on_error(token, "human-vocal observer failure: " + str(error))

    def _start_alignment_locked(self, token: Token, generation: int, boundary: int,
                                aligner: TargetConditionedAligner,
                                requested_ns: int = 0):
        if (self._closed or token != self.token or generation != self._generation
                or aligner is not self.aligner):
            return None
        start = max(self._retained_start_locked(boundary), int(self._search_start_ms))
        segments = self.history.contiguous_slices(start, boundary)
        if not segments:
            return None
        query = (start, int(boundary))
        self._last_alignment_query = query
        self._alignment_inflight = True
        self._alignment_generation = generation
        return ("infer", token, generation, segments, aligner, requested_ns, query)

    def _request_alignment_locked(self, boundary: int, *, queue_if_busy: bool = False):
        if self._closed or self.token is None or self.aligner is None:
            return None
        boundary = int(boundary)
        if boundary - int(self._search_start_ms) < self.MIN_AUDIO_MS:
            return None
        query = (max(self._retained_start_locked(boundary), int(self._search_start_ms)), boundary)
        if query == self._last_alignment_query and not queue_if_busy:
            return None
        token, generation, aligner = self.token, self._generation, self.aligner
        requested_ns = time.perf_counter_ns() if self.on_inference else 0
        if self._alignment_inflight:
            pending = self._pending_alignment
            start_changed = (
                self._last_alignment_query is None
                or query[0] != self._last_alignment_query[0]
            )
            if (queue_if_busy or self._alignment_generation != generation
                    or pending is None or boundary >= pending[2] or start_changed):
                self._pending_alignment = (
                    token, generation, boundary, aligner, requested_ns)
            return None
        return self._start_alignment_locked(
            token, generation, boundary, aligner, requested_ns)

    def _take_pending_locked(self):
        pending, self._pending_alignment = self._pending_alignment, None
        if pending is None:
            return None
        token, generation, boundary, aligner, requested_ns = pending
        if (self._closed or token != self.token or generation != self._generation
                or aligner is not self.aligner):
            return None
        return self._start_alignment_locked(
            token, generation, boundary, aligner, requested_ns)

    def _dispatch(self, item) -> None:
        if item is None:
            return
        kind = item[0]
        if kind == "progress":
            _, token, generation, progress, observation = item
            with self._lock:
                if generation != self._generation or token != self.token:
                    return
            if self.on_progress is not None:
                self.on_progress(token, progress, observation)
            return
        if kind == "pcm":
            _, token, generation, frames = item
            with self._lock:
                if generation != self._generation or token != self.token:
                    return
            if self.on_pcm is not None:
                self.on_pcm(token, frames)
            return
        if kind != "infer":
            raise RuntimeError(f"unknown native-audio work item: {kind}")
        _, token, generation, segments, aligner, requested_ns, query = item
        with self._lock:
            if generation != self._generation or token != self.token:
                if self._alignment_generation == generation:
                    self._alignment_inflight = False
                    self._alignment_generation = None
                    next_item = self._take_pending_locked()
                else:
                    next_item = None
            else:
                next_item = None
        if next_item is not None:
            self._dispatch(next_item)
            return
        with self._lock:
            if generation != self._generation or token != self.token:
                return
        try:
            self._alignment_pool.submit(
                self._infer, token, generation, segments, aligner, requested_ns, query)
        except RuntimeError:
            with self._lock:
                if self._alignment_generation == generation:
                    self._alignment_inflight = False
                    self._alignment_generation = None
                    next_item = self._take_pending_locked()
                else:
                    next_item = None
            self._dispatch(next_item)

    @staticmethod
    def _audio_offset_to_wall_ms(samples: tuple[PCMChunk, ...], offset_ms: float) -> float | None:
        if offset_ms < 0:
            return None
        consumed = 0.0
        for chunk in samples:
            duration = chunk.duration_ms
            if offset_ms <= consumed + duration + 1e-6:
                inside = max(0.0, min(duration, offset_ms - consumed))
                return chunk.start_ms + inside
            consumed += duration
        return None

    @classmethod
    def _lexical_from_alignment(
        cls, alignment: TargetTimeAlignment, samples: tuple[PCMChunk, ...],
    ) -> LexicalProgressEvidence:
        target_start_ms = None
        aligned_end_ms = None
        target_end_ms = None
        if alignment.target_unit_spans:
            wall = cls._audio_offset_to_wall_ms(
                samples, alignment.target_unit_spans[0].start * alignment.frame_ms)
            if wall is not None:
                target_start_ms = int(round(wall))
            wall = cls._audio_offset_to_wall_ms(
                samples, alignment.target_unit_spans[-1].end * alignment.frame_ms)
            if wall is not None:
                aligned_end_ms = int(round(wall))
        if alignment.target_end_frame is not None:
            wall = cls._audio_offset_to_wall_ms(
                samples, alignment.target_end_frame * alignment.frame_ms)
            if wall is not None:
                target_end_ms = int(round(wall))
        return LexicalProgressEvidence.from_alignment(
            alignment,
            target_start_ms=target_start_ms,
            aligned_end_ms=aligned_end_ms,
            target_end_ms=target_end_ms,
        )

    @staticmethod
    def _select_alignment(
        aligner: TargetConditionedAligner,
        segments: tuple[tuple[PCMChunk, ...], ...],
    ) -> tuple[_AlignmentResult, list[tuple[TargetTimeAlignment, tuple[PCMChunk, ...]]]]:
        results: list[tuple[TargetTimeAlignment, tuple[PCMChunk, ...]]] = []
        for samples in segments:
            if not samples:
                continue
            try:
                results.append((aligner.align(samples), samples))
            except ValueError:
                # A very short contiguous span may not produce a posterior.  It
                # remains retained PCM but supplies no lexical measurement.
                continue
        if not results:
            return _AlignmentResult(None, ()), results
        associated = [item for item in results if item[0].target_associated]
        if len(associated) > 1:
            # Multiple retained locations remain unresolved.  Do not choose by
            # recency or HOOK proximity and turn a search heuristic into truth.
            return _AlignmentResult(None, (), ambiguous=True), results
        if len(associated) == 1:
            alignment, samples = associated[0]
            return _AlignmentResult(alignment, samples), results
        alignment, samples = max(
            results,
            key=lambda item: (
                item[0].aligned_target_units,
                item[0].fit.mean_unit_peak_margin,
            ),
        )
        return _AlignmentResult(alignment, samples), results

    def _feature_start_locked(self, lexical: LexicalProgressEvidence | None, end_ms: int) -> int | None:
        retained = self._retained_start_locked(end_ms)
        if lexical is not None and lexical.target_start_ms is not None:
            target_start = int(lexical.target_start_ms)
            if self.causal_lower_bound_ms is not None:
                return max(retained, int(self.causal_lower_bound_ms))
            earlier_inputs = [
                stamp for stamp in self.input_candidates_ms if stamp <= target_start
            ]
            if earlier_inputs:
                return max(retained, max(earlier_inputs))
            return max(retained, target_start - self.FEATURE_CONTEXT_LOOKBACK_MS)
        if self.causal_lower_bound_ms is not None:
            return max(retained, int(self.causal_lower_bound_ms))
        if self.input_candidates_ms:
            return max(retained, min(self.input_candidates_ms))
        return None

    def _assess_locked(
        self, *, capture_start_ms: int | None, capture_end_ms: int,
        lexical: LexicalProgressEvidence | None,
    ) -> tuple[NativeAudioObservation, UtteranceProgress]:
        feature_end = self.features.latest_ms
        if feature_end is not None:
            feature_end = min(int(feature_end), int(capture_end_ms))
        vocal = None
        feature_start = self._feature_start_locked(lexical, capture_end_ms)
        if feature_start is not None and feature_end is not None and feature_end >= feature_start:
            vocal = self.features.summarize_vocal(
                start_ms=int(feature_start), end_ms=int(feature_end))

        # Physical-risk decisions may advance only through observed acoustic
        # measurements.  Raw PCM can legitimately be ahead while the observer
        # worker catches up; that lag becomes UNKNOWN/older evidence, never quiet.
        evidence_end = int(feature_end) if vocal is not None else int(capture_end_ms)
        recurrence = None

        def recurrence_lookup(start_ms: int, end_ms: int):
            nonlocal recurrence
            recurrence = self.features.recurrence(
                start_ms=min(int(start_ms), int(end_ms)), end_ms=int(end_ms))
            return recurrence

        progress, recurrence = evaluate_utterance_progress(
            capture_end_ms=evidence_end,
            lexical=lexical,
            vocal=vocal,
            causal_lower_bound_ms=self.causal_lower_bound_ms,
            input_candidates_ms=self.input_candidates_ms,
            recurrence_lookup=recurrence_lookup,
        )
        observation = NativeAudioObservation(
            kind="snapshot",
            capture_start_ms=capture_start_ms,
            capture_end_ms=int(capture_end_ms),
            lexical=lexical,
            vocal=vocal,
            recurrence=recurrence,
            causal_lower_bound_ms=self.causal_lower_bound_ms,
            input_candidates_ms=self.input_candidates_ms,
            target_attached_ms=self.target_attached_ms,
            history_truncated=(
                capture_start_ms is not None
                and self.causal_lower_bound_ms is not None
                and int(capture_start_ms) > int(self.causal_lower_bound_ms) + 1
            ),
            pcm_last_frame_ms=int(capture_end_ms),
        )
        return observation, progress

    def _progress_item_locked(
        self, boundary: int, *, force: bool = False,
        capture_start_override: int | None = None,
    ):
        if self._closed or self.token is None:
            return None
        boundary = int(boundary)
        lexical = self._committed_lexical or self._latest_lexical
        feature_end = self.features.latest_ms
        if feature_end is not None:
            feature_end = min(int(feature_end), boundary)
        key = (boundary, feature_end, self._lexical_revision)
        # A fence may force scheduling, but identical evidence is already a
        # completed assessment and must not be republished as a new observation.
        if key == self._last_progress_key:
            return None
        capture_start = (
            int(capture_start_override) if capture_start_override is not None else (
                int(self._lexical_capture_start_ms)
                if lexical is not None and self._lexical_capture_start_ms is not None
                else self._feature_start_locked(lexical, boundary)
            )
        )
        observation, progress = self._assess_locked(
            capture_start_ms=capture_start,
            capture_end_ms=boundary,
            lexical=lexical,
        )
        self._last_progress_key = key
        return ("progress", self.token, self._generation, progress, observation)

    def _infer(
        self, token: Token, generation: int,
        segments: tuple[tuple[PCMChunk, ...], ...],
        aligner: TargetConditionedAligner, submitted_ns: int,
        query: tuple[int, int],
    ) -> None:
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        selection: _AlignmentResult | None = None
        all_results: list[tuple[TargetTimeAlignment, tuple[PCMChunk, ...]]] = []
        progress_item = None
        retry_item = None
        metrics = None
        try:
            selection, all_results = self._select_alignment(aligner, segments)
            decoded_ns = time.perf_counter_ns() if self.on_inference else 0
            boundary = int(query[1])
            lexical = None
            if selection.alignment is not None and selection.samples:
                lexical = self._lexical_from_alignment(selection.alignment, selection.samples)

            with self._lock:
                current = bool(
                    not self._closed
                    and generation == self._generation
                    and token == self.token
                    and aligner is self.aligner
                    and self.history.healthy
                )
                if current:
                    associated = bool(
                        selection.alignment is not None
                        and selection.alignment.target_associated
                    )
                    if not associated and not selection.ambiguous:
                        if self._expand_search_locked(boundary):
                            self._pending_alignment = (
                                token, generation, boundary, aligner,
                                time.perf_counter_ns() if self.on_inference else 0,
                            )
                        # At the retained floor the search result becomes an
                        # ordinary unresolved measurement; future new PCM starts
                        # a bounded recent query again.
                    if lexical is not None:
                        self._latest_lexical = lexical
                        self._lexical_capture_start_ms = int(query[0])
                        self._lexical_revision += 1
                        if selection.alignment is not None:
                            self._narrow_search_locked(
                                lexical,
                                frame_ms=selection.alignment.frame_ms,
                                capture_end_ms=boundary,
                            )
                        if (selection.alignment is not None
                                and selection.alignment.committed_target_end):
                            self._committed_lexical = lexical
                            self.aligner = None
                            self._pending_alignment = None
                    if self._pending_alignment is None or self.aligner is None:
                        progress_item = self._progress_item_locked(
                            boundary, force=True, capture_start_override=query[0])
                        # Alignment and continuous observation are independent.
                        # If the observer has not yet consumed this PCM frontier,
                        # require one follow-up assessment when it catches up; do
                        # not let thread scheduling change retained evidence.
                        if (self._observer_frontier_ms is None
                                or self._observer_frontier_ms < boundary):
                            self._required_assessment_ms = max(
                                boundary,
                                self._required_assessment_ms
                                if self._required_assessment_ms is not None else boundary,
                            )

                    if self.on_inference is not None:
                        sample_count_ms = sum(
                            chunk.duration_ms for span in segments for chunk in span)
                        chosen = selection.alignment
                        metrics = {
                            "queue_ms": round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                            "align_ms": round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                            "audio_ms": round(sample_count_ms, 3),
                            "capture_start_ms": query[0],
                            "capture_end_ms": query[1],
                            "observation_end_ms": (
                                progress_item[4].capture_end_ms if progress_item is not None else query[1]),
                            "causal_lower_bound_ms": self.causal_lower_bound_ms,
                            "input_candidates_ms": list(self.input_candidates_ms),
                            "target_attached_ms": self.target_attached_ms,
                            "history_truncated": bool(
                                self.causal_lower_bound_ms is not None
                                and query[0] > int(self.causal_lower_bound_ms) + 1),
                            "continuity_spans": len(segments),
                            "ambiguous_alignment_spans": bool(selection.ambiguous),
                        }
                        if chosen is not None:
                            fit = chosen.fit
                            chosen_lexical = lexical
                            metrics.update({
                                "frame_count": chosen.frame_count,
                                "target_units": chosen.target_units,
                                "aligned_target_units": chosen.aligned_target_units,
                                "target_coverage": round(chosen.coverage, 4),
                                "target_associated": chosen.target_associated,
                                "complete_alignment": chosen.complete_alignment,
                                "committed_target_end": chosen.committed_target_end,
                                "target_end_frame": chosen.target_end_frame,
                                "reading_rank": chosen.reading_rank,
                                "path_margin": round(fit.path_margin, 4),
                                "boundary_margin": round(fit.boundary_margin, 4),
                                "lexical_target_end_ms": (
                                    chosen_lexical.target_end_ms if chosen_lexical is not None else None),
                                "rejection": chosen.rejection,
                            })
            if metrics is not None:
                try:
                    self.on_inference(token, metrics)
                except Exception:
                    pass
            self._dispatch(progress_item)
        except Exception as exc:
            self.on_error(token, "acoustic inference failure: " + str(exc))
        finally:
            with self._lock:
                if self._alignment_generation == generation:
                    self._alignment_inflight = False
                    self._alignment_generation = None
                    retry_item = self._take_pending_locked()
            self._dispatch(retry_item)

    def close(self) -> None:
        self.cancel()
        with self._lock:
            self._closed = True
            self._observer_queue.clear()
        self._capture.close()
        self._alignment_pool.shutdown(wait=True, cancel_futures=True)
        self._observer_pool.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self.features.reset()
