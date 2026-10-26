"""Process-scoped acoustic measurement over one retained PCM timeline.

ProcessCapture owns acquisition and AudioHistory is the only raw-audio source of
truth. Target-independent human-vocal observation advances through that timeline
with one cursor; target-conditioned alignment issues bounded range queries over
the same timeline. Neither path recreates capture state or owns game input.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable
import time

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


@dataclass(frozen=True)
class _Assessment:
    token: Token
    generation: int
    boundary_ms: int
    capture_start_ms: int | None
    feature_start_ms: int | None
    lexical: LexicalProgressEvidence | None
    causal_lower_bound_ms: int | None
    input_candidates_ms: tuple[int, ...]
    target_attached_ms: int | None
    history_truncated: bool
    lexical_revision: int


class NativeAudio:
    """Measure one process PCM timeline and publish integrated utterance progress."""

    RETENTION_MS = 60_000
    INFERENCE_INTERVAL_MS = 500
    MIN_AUDIO_MS = 300
    # Alignment work budget only. Failure expands geometrically toward the
    # retained/causal floor; this value never means utterance duration.
    INITIAL_SEARCH_WINDOW_MS = 8_000

    def __init__(self, pid: int, helper: Path, model: Path, vocab: Path,
                 on_error: Callable[[Token | None, str], None],
                 *, model_backend=None, g2p=None, clock=None,
                 human_vocal_model: Path | None = None,
                 vocal_observer: VocalActivityObserver | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None,
                 on_progress: Callable[[Token, UtteranceProgress, NativeAudioObservation], None] | None = None):
        self.pid = pid
        self.on_error = on_error
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

        # Current Presentation measurement state. Raw PCM ownership stays in
        # AudioHistory; these values describe only target/query progress.
        self.token: Token | None = None
        self.aligner: TargetConditionedAligner | None = None
        self.causal_lower_bound_ms: int | None = None
        self.input_candidates_ms: tuple[int, ...] = ()
        self.target_attached_ms: int | None = None
        self._search_floor_ms = 0
        self._search_start_ms = 0
        self._search_exhausted_at: int | None = None
        self._lexical: LexicalProgressEvidence | None = None
        self._lexical_capture_start_ms: int | None = None
        self._lexical_revision = 0
        self._alignment_ready_through_ms: int | None = None

        # One expensive alignment may be active; at most one newer boundary is
        # retained. Intermediate snapshots have no product meaning.
        self._alignment_inflight_generation: int | None = None
        self._pending_alignment_boundary: int | None = None
        self._last_alignment_query: tuple[int, int] | None = None

        # Continuous target-independent observation follows AudioHistory itself.
        # There is no second PCM queue or replay path.
        self._observer_cursor_ms: int | None = None
        self._observer_running = False
        self._observer_revision = 0

        # A fallback fence is just an assessment obligation. Progress is emitted
        # only when both lexical (when required) and observer measurements reach it.
        self._required_assessment_ms: int | None = None
        self._last_progress_key: tuple[int, int] | None = None

        self._generation = 0
        self._closed = False
        self._capture_error: str | None = None

    def start(self) -> None:
        self._capture.start()

    def _retained_start_locked(self, end_ms: int | None = None) -> int:
        earliest = self.history.earliest_ms
        if earliest is not None:
            return int(earliest)
        if end_ms is None:
            end_ms = self.clock()
        return max(0, int(end_ms) - self.RETENTION_MS)

    def _minimum_search_start_locked(self, end_ms: int) -> int:
        start = self._retained_start_locked(end_ms)
        if self.causal_lower_bound_ms is not None:
            start = max(start, int(self.causal_lower_bound_ms))
        return start

    def _initial_search_start_locked(self, end_ms: int) -> int:
        minimum = self._minimum_search_start_locked(end_ms)
        self._search_floor_ms = minimum
        start = max(minimum, int(end_ms) - self.INITIAL_SEARCH_WINDOW_MS)
        usable = [
            value for value in self.input_candidates_ms
            if minimum <= value <= int(end_ms) - self.MIN_AUDIO_MS
        ]
        if usable:
            # External input is only a search seed. Failure may expand behind it.
            start = max(start, max(usable))
        return min(start, int(end_ms))

    def _expand_search_locked(self, boundary: int) -> bool:
        minimum = max(self._minimum_search_start_locked(boundary), int(self._search_floor_ms))
        current = max(minimum, int(self._search_start_ms))
        if current <= minimum:
            self._search_exhausted_at = int(boundary)
            return False
        span = max(self.MIN_AUDIO_MS, int(boundary) - current)
        expanded = max(minimum, current - max(self.INITIAL_SEARCH_WINDOW_MS, span))
        if expanded >= current:
            self._search_exhausted_at = int(boundary)
            return False
        self._search_start_ms = expanded
        self._search_exhausted_at = None
        return True

    def _prepare_new_audio_search_locked(self, boundary: int) -> None:
        exhausted = self._search_exhausted_at
        if exhausted is None or int(boundary) <= exhausted:
            return
        minimum = self._minimum_search_start_locked(boundary)
        lexical = self._lexical
        if (lexical is not None and lexical.aligned_target_units > 0
                and lexical.target_start_ms is not None):
            # Preserve only evidence already tied to the emerging target.
            start = int(lexical.target_start_ms)
        else:
            # No target evidence existed in the exhausted history. Keep only a
            # small backend context across the old frontier; do not rescan an
            # arbitrary fixed history window for every new PCM packet.
            start = int(exhausted) - self.MIN_AUDIO_MS
        self._search_floor_ms = max(minimum, start)
        self._search_start_ms = self._search_floor_ms
        self._search_exhausted_at = None

    def _narrow_search_locked(
        self, lexical: LexicalProgressEvidence, *, frame_ms: float, capture_end_ms: int,
    ) -> None:
        if not lexical.target_associated or lexical.target_start_ms is None:
            return
        minimum = self._minimum_search_start_locked(capture_end_ms)
        target_start = int(lexical.target_start_ms)
        if self.causal_lower_bound_ms is not None:
            desired = minimum
        else:
            usable = [stamp for stamp in self.input_candidates_ms if minimum <= stamp <= target_start]
            desired = max(usable) if usable else max(minimum, target_start - max(1, int(round(frame_ms))))
        self._search_floor_ms = max(int(self._search_floor_ms), int(desired))
        self._search_start_ms = max(int(self._search_start_ms), int(desired))

    def begin(self, token: Token, alignment_text: str, *,
              candidates: tuple[PronunciationCandidate, ...] | None = None,
              causal_lower_bound_ms: int | None = None,
              input_candidates_ms: tuple[int, ...] = ()) -> None:
        """Attach known text to retained PCM without replaying capture state."""
        lower_bound = int(causal_lower_bound_ms) if causal_lower_bound_ms is not None else None
        if lower_bound is not None and lower_bound <= 0:
            raise ValueError("audio causal lower bound must be a positive monotonic timestamp")
        input_candidates = tuple(sorted({int(value) for value in input_candidates_ms if int(value) > 0}))
        try:
            candidate = TargetConditionedAligner(self.model, g2p=self.g2p)
            candidate.set_target(alignment_text, candidates=candidates)
            aligner: TargetConditionedAligner | None = candidate
        except (ValueError, KeyError):
            aligner = None

        alignment_work = None
        submit_observer = False
        with self._lock:
            self._generation += 1
            self._pending_alignment_boundary = None
            self.token = token
            self.aligner = aligner
            self.causal_lower_bound_ms = lower_bound
            self.input_candidates_ms = input_candidates
            self.target_attached_ms = int(self.clock())
            self._lexical = None
            self._lexical_capture_start_ms = None
            self._lexical_revision += 1
            self._alignment_ready_through_ms = None if aligner is not None else self.history.latest_ms
            self._last_alignment_query = None
            self._last_progress_key = None
            self._required_assessment_ms = None

            frontier = self.history.latest_ms
            if frontier is not None:
                # Attaching a target obligates one integrated assessment through
                # all PCM already retained at that moment. Hook timing must not
                # make a partially processed observer frontier the product result.
                self._required_assessment_ms = int(frontier)
            end = int(frontier if frontier is not None else self.target_attached_ms)
            self._search_start_ms = self._initial_search_start_locked(end)
            self._search_exhausted_at = None

            if frontier is not None:
                if aligner is not None and frontier - self._search_start_ms >= self.MIN_AUDIO_MS:
                    alignment_work = self._request_alignment_locked(int(frontier))
                submit_observer = self._wake_observer_locked()

        self._submit_alignment(alignment_work)
        if submit_observer:
            self._submit_observer()
        with self._lock:
            failure = self._capture_error if self.token == token else None
        if failure:
            self.on_error(token, failure)

    def _retire_target_locked(self) -> None:
        self._pending_alignment_boundary = None
        self.token = None
        self.aligner = None
        self.causal_lower_bound_ms = None
        self.input_candidates_ms = ()
        self.target_attached_ms = None
        self._lexical = None
        self._lexical_capture_start_ms = None
        self._alignment_ready_through_ms = None
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
            if self.token is not None or self.aligner is not None or self._pending_alignment_boundary is not None:
                self._retire_target_locked()

    def health(self, status: CaptureHealth) -> None:
        """Handle transport failure; healthy heartbeats carry no analysis work."""
        if status.healthy:
            return
        with self._lock:
            self._capture_error = status.reason
            token = self.token
            self._observer_revision += 1
            self._observer_cursor_ms = None
        if token is not None:
            self.on_error(token, status.reason)

    def observation_fence(self, token: Token) -> int | None:
        with self._lock:
            if self._closed or token != self.token:
                return None
            frontier = self.history.latest_ms
            return int(frontier) if frontier is not None else None

    def request_assessment_through(self, token: Token, through_ms: int) -> None:
        """Require one integrated assessment through already-captured PCM."""
        alignment_work = None
        submit_observer = False
        with self._lock:
            if self._closed or token != self.token:
                return
            frontier = self.history.latest_ms
            if frontier is None:
                return
            boundary = min(int(through_ms), int(frontier))
            self._required_assessment_ms = max(
                boundary,
                self._required_assessment_ms if self._required_assessment_ms is not None else boundary,
            )
            if self.aligner is not None:
                alignment_work = self._request_alignment_locked(boundary)
                if (alignment_work is None
                        and self._alignment_inflight_generation is None
                        and boundary - int(self._search_start_ms) < self.MIN_AUDIO_MS):
                    self._alignment_ready_through_ms = max(
                        boundary,
                        self._alignment_ready_through_ms
                        if self._alignment_ready_through_ms is not None else boundary,
                    )
            submit_observer = self._wake_observer_locked()
        self._submit_alignment(alignment_work)
        if submit_observer:
            self._submit_observer()

    def feed(self, chunk: PCMChunk) -> None:
        """Capture callback: wake timeline consumers and return without inference."""
        alignment_work = None
        submit_observer = False
        with self._lock:
            if self._closed:
                return
            submit_observer = self._wake_observer_locked()
            if self.token is not None and self.aligner is not None:
                self._prepare_new_audio_search_locked(int(chunk.at_ms))
                last_boundary = self._last_alignment_query[1] if self._last_alignment_query is not None else -1
                if (int(chunk.at_ms) - self._search_start_ms >= self.MIN_AUDIO_MS
                        and (last_boundary < 0
                             or int(chunk.at_ms) - last_boundary >= self.INFERENCE_INTERVAL_MS)):
                    alignment_work = self._request_alignment_locked(int(chunk.at_ms))
        if submit_observer:
            self._submit_observer()
        self._submit_alignment(alignment_work)

    def _wake_observer_locked(self) -> bool:
        if self._closed or self._observer_running:
            return False
        self._observer_running = True
        return True

    def _submit_observer(self) -> None:
        try:
            self._observer_pool.submit(self._observer_loop)
        except RuntimeError:
            with self._lock:
                self._observer_running = False

    def _observer_loop(self) -> None:
        error: Exception | None = None
        try:
            while True:
                with self._lock:
                    if self._closed:
                        self._observer_running = False
                        return
                    revision = self._observer_revision
                    earliest = self.history.earliest_ms
                    frontier = self.history.latest_ms
                    if earliest is None or frontier is None:
                        assessment = self._assessment_locked()
                        if assessment is None:
                            self._observer_running = False
                            return
                        spans = ()
                    else:
                        cursor = self._observer_cursor_ms
                        rebuild_features = bool(cursor is None or int(cursor) < int(earliest))
                        start = int(earliest) if rebuild_features else int(cursor)
                        window = self.history.read(start, int(frontier))
                        spans = window.spans
                        reset_stream = rebuild_features or bool(
                            cursor is not None
                            and spans
                            and not window.continues_from_start
                        )
                        assessment = None if spans else self._assessment_locked()
                        if not spans and assessment is None:
                            self._observer_running = False
                            return

                if spans:
                    if reset_stream:
                        reset = getattr(self.vocal_observer, "reset", None)
                        if callable(reset):
                            reset()
                    if rebuild_features:
                        self.features.reset()
                    first_span = True
                    for span in spans:
                        if not first_span:
                            reset = getattr(self.vocal_observer, "reset", None)
                            if callable(reset):
                                reset()
                        first_span = False
                        for chunk in span:
                            frames = self.vocal_observer.observe(chunk)
                            self.features.add_vocal(frames)

                    with self._lock:
                        if revision != self._observer_revision:
                            continue
                        self._observer_cursor_ms = int(frontier)
                        assessment = self._assessment_locked()

                if assessment is not None:
                    self._run_assessment(assessment)
                    continue

                # New PCM or a newly-ready alignment may have arrived while the
                # model was running. Loop against the timeline again before idle.
        except Exception as exc:
            error = exc
        finally:
            if error is not None:
                with self._lock:
                    token = self.token
                    self._observer_running = False
                if token is not None:
                    self.on_error(token, "human-vocal observer failure: " + str(error))

    def _ready_boundary_locked(self) -> int | None:
        if self.token is None or self._observer_cursor_ms is None:
            return None
        observer = int(self._observer_cursor_ms)
        if self.aligner is None:
            ready = observer
        elif self._alignment_ready_through_ms is None:
            return None
        else:
            ready = min(observer, int(self._alignment_ready_through_ms))

        required = self._required_assessment_ms
        if required is not None:
            return int(required) if ready >= int(required) else None
        previous = self._last_progress_key[0] if self._last_progress_key is not None else None
        if previous is not None and ready - int(previous) < self.INFERENCE_INTERVAL_MS:
            return None
        return ready

    def _history_truncated_locked(self) -> bool:
        if self.causal_lower_bound_ms is None:
            return False
        earliest = self.history.earliest_ms
        return bool(earliest is not None and int(earliest) > int(self.causal_lower_bound_ms) + 1)

    def _feature_start_locked(self, lexical: LexicalProgressEvidence | None, end_ms: int) -> int | None:
        retained = self._retained_start_locked(end_ms)
        if lexical is not None and lexical.target_start_ms is not None:
            target_start = int(lexical.target_start_ms)
            if self.causal_lower_bound_ms is not None:
                return max(retained, int(self.causal_lower_bound_ms))
            earlier_inputs = [stamp for stamp in self.input_candidates_ms if stamp <= target_start]
            if earlier_inputs:
                return max(retained, max(earlier_inputs))
            # Once the target itself is located, its acoustic start is sufficient
            # context. No arbitrary pre-target lookback is a product requirement.
            return max(retained, target_start)
        if self.causal_lower_bound_ms is not None:
            return max(retained, int(self.causal_lower_bound_ms))
        if self.input_candidates_ms:
            return max(retained, min(self.input_candidates_ms))
        return None

    def _assessment_locked(self) -> _Assessment | None:
        boundary = self._ready_boundary_locked()
        if boundary is None or self.token is None:
            return None
        key = (int(boundary), self._lexical_revision)
        if key == self._last_progress_key:
            return None
        lexical = self._lexical
        capture_start = (
            int(self._lexical_capture_start_ms)
            if lexical is not None and self._lexical_capture_start_ms is not None
            else self._feature_start_locked(lexical, boundary)
        )
        feature_start = self._feature_start_locked(lexical, boundary)
        return _Assessment(
            token=self.token,
            generation=self._generation,
            boundary_ms=int(boundary),
            capture_start_ms=capture_start,
            feature_start_ms=feature_start,
            lexical=lexical,
            causal_lower_bound_ms=self.causal_lower_bound_ms,
            input_candidates_ms=self.input_candidates_ms,
            target_attached_ms=self.target_attached_ms,
            history_truncated=self._history_truncated_locked(),
            lexical_revision=self._lexical_revision,
        )

    def _run_assessment(self, item: _Assessment) -> None:
        vocal = None
        if item.feature_start_ms is not None:
            vocal = self.features.summarize_vocal(
                start_ms=int(item.feature_start_ms), end_ms=int(item.boundary_ms))

        recurrence = None

        def recurrence_lookup(start_ms: int, end_ms: int):
            nonlocal recurrence
            recurrence = self.features.recurrence(
                start_ms=min(int(start_ms), int(end_ms)), end_ms=int(end_ms))
            return recurrence

        progress, recurrence = evaluate_utterance_progress(
            capture_end_ms=item.boundary_ms,
            lexical=item.lexical,
            vocal=vocal,
            causal_lower_bound_ms=item.causal_lower_bound_ms,
            input_candidates_ms=item.input_candidates_ms,
            recurrence_lookup=recurrence_lookup,
        )
        observation = NativeAudioObservation(
            capture_start_ms=item.capture_start_ms,
            capture_end_ms=item.boundary_ms,
            lexical=item.lexical,
            vocal=vocal,
            recurrence=recurrence,
            causal_lower_bound_ms=item.causal_lower_bound_ms,
            input_candidates_ms=item.input_candidates_ms,
            target_attached_ms=item.target_attached_ms,
            history_truncated=item.history_truncated,
        )

        callback = None
        with self._lock:
            if (self._closed or item.generation != self._generation
                    or item.token != self.token
                    or item.lexical_revision != self._lexical_revision):
                return
            key = (item.boundary_ms, item.lexical_revision)
            if key == self._last_progress_key:
                return
            self._last_progress_key = key
            if (self._required_assessment_ms is not None
                    and item.boundary_ms >= self._required_assessment_ms):
                self._required_assessment_ms = None
            callback = self.on_progress
        if callback is not None:
            callback(item.token, progress, observation)

    def _start_alignment_locked(self, boundary: int):
        if self._closed or self.token is None or self.aligner is None:
            return None
        start = max(self._minimum_search_start_locked(boundary), int(self._search_start_ms))
        segments = self.history.read(start, boundary).spans
        if not segments:
            return None
        query = (start, int(boundary))
        self._last_alignment_query = query
        generation = self._generation
        self._alignment_inflight_generation = generation
        requested_ns = time.perf_counter_ns() if self.on_inference else 0
        return (self.token, generation, segments, self.aligner, requested_ns, query)

    def _request_alignment_locked(self, boundary: int):
        if self._closed or self.token is None or self.aligner is None:
            return None
        boundary = int(boundary)
        if boundary - int(self._search_start_ms) < self.MIN_AUDIO_MS:
            return None
        query = (max(self._minimum_search_start_locked(boundary), int(self._search_start_ms)), boundary)
        if query == self._last_alignment_query:
            return None
        if self._alignment_inflight_generation is not None:
            active_boundary = (
                int(self._last_alignment_query[1])
                if self._last_alignment_query is not None else -1
            )
            if boundary <= active_boundary:
                return None
            if self._pending_alignment_boundary is None or boundary > self._pending_alignment_boundary:
                self._pending_alignment_boundary = boundary
            return None
        return self._start_alignment_locked(boundary)

    def _take_pending_alignment_locked(self):
        boundary = self._pending_alignment_boundary
        self._pending_alignment_boundary = None
        if boundary is None:
            return None
        return self._start_alignment_locked(int(boundary))

    def _submit_alignment(self, work) -> None:
        if work is None:
            return
        token, generation, segments, aligner, requested_ns, query = work
        try:
            self._alignment_pool.submit(
                self._infer, token, generation, segments, aligner, requested_ns, query)
        except RuntimeError:
            next_work = None
            with self._lock:
                if self._alignment_inflight_generation == generation:
                    self._alignment_inflight_generation = None
                    next_work = self._take_pending_alignment_locked()
            self._submit_alignment(next_work)

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
    ) -> _AlignmentResult:
        results: list[tuple[TargetTimeAlignment, tuple[PCMChunk, ...]]] = []
        for samples in segments:
            if not samples:
                continue
            try:
                results.append((aligner.align(samples), samples))
            except ValueError:
                continue
        if not results:
            return _AlignmentResult(None, ())
        associated = [item for item in results if item[0].target_associated]
        if len(associated) > 1:
            return _AlignmentResult(None, (), ambiguous=True)
        if len(associated) == 1:
            alignment, samples = associated[0]
            return _AlignmentResult(alignment, samples)
        alignment, samples = max(
            results,
            key=lambda item: (item[0].aligned_target_units, item[0].fit.mean_unit_peak_margin),
        )
        return _AlignmentResult(alignment, samples)

    def _infer(
        self, token: Token, generation: int,
        segments: tuple[tuple[PCMChunk, ...], ...],
        aligner: TargetConditionedAligner, submitted_ns: int,
        query: tuple[int, int],
    ) -> None:
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        metrics = None
        next_work = None
        wake_observer = False
        try:
            selection = self._select_alignment(aligner, segments)
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
                    retry_same_boundary = False
                    if not associated and not selection.ambiguous:
                        retry_same_boundary = self._expand_search_locked(boundary)
                        if retry_same_boundary:
                            # Search widening changes the next query, but a newer
                            # captured frontier already pending subsumes this older
                            # snapshot and must not be discarded.
                            self._pending_alignment_boundary = max(
                                boundary,
                                self._pending_alignment_boundary
                                if self._pending_alignment_boundary is not None else boundary,
                            )
                    if lexical is not None:
                        if lexical != self._lexical:
                            self._lexical = lexical
                            self._lexical_capture_start_ms = int(query[0])
                            self._lexical_revision += 1
                        if selection.alignment is not None:
                            self._narrow_search_locked(
                                lexical,
                                frame_ms=selection.alignment.frame_ms,
                                capture_end_ms=boundary,
                            )
                        if selection.alignment is not None and selection.alignment.committed_target_end:
                            self.aligner = None
                            self._pending_alignment_boundary = None
                            self._alignment_ready_through_ms = boundary
                    if self.aligner is not None and not retry_same_boundary:
                        # This complete query is the lexical evidence available at
                        # this frontier, including unresolved/ambiguous results.
                        self._alignment_ready_through_ms = max(
                            boundary,
                            self._alignment_ready_through_ms
                            if self._alignment_ready_through_ms is not None else boundary,
                        )
                    wake_observer = self._wake_observer_locked()

                    if self.on_inference is not None:
                        sample_count_ms = sum(chunk.duration_ms for span in segments for chunk in span)
                        chosen = selection.alignment
                        metrics = {
                            "queue_ms": round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                            "align_ms": round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                            "audio_ms": round(sample_count_ms, 3),
                            "capture_start_ms": query[0],
                            "capture_end_ms": query[1],
                            "causal_lower_bound_ms": self.causal_lower_bound_ms,
                            "input_candidates_ms": list(self.input_candidates_ms),
                            "target_attached_ms": self.target_attached_ms,
                            "history_truncated": self._history_truncated_locked(),
                            "continuity_spans": len(segments),
                            "ambiguous_alignment_spans": bool(selection.ambiguous),
                        }
                        if chosen is not None:
                            fit = chosen.fit
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
                                "lexical_target_end_ms": lexical.target_end_ms if lexical is not None else None,
                                "rejection": chosen.rejection,
                            })
            if metrics is not None and self.on_inference is not None:
                try:
                    self.on_inference(token, metrics)
                except Exception:
                    pass
            if wake_observer:
                self._submit_observer()
        except Exception as exc:
            self.on_error(token, "acoustic inference failure: " + str(exc))
        finally:
            with self._lock:
                if self._alignment_inflight_generation == generation:
                    self._alignment_inflight_generation = None
                    next_work = self._take_pending_alignment_locked()
            self._submit_alignment(next_work)

    def close(self) -> None:
        self.cancel()
        with self._lock:
            self._closed = True
            self._observer_revision += 1
        self._capture.close()
        self._alignment_pool.shutdown(wait=True, cancel_futures=True)
        self._observer_pool.shutdown(wait=True, cancel_futures=True)
        self.features.reset()
