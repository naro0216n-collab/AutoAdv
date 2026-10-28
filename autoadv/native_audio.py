"""Process-scoped acoustic measurement over one retained PCM timeline.

ProcessCapture owns acquisition and AudioHistory is the only raw-audio source of
truth. Human-vocal observation advances continuously through that timeline with
one cursor, while known-text alignment issues bounded range queries over the same
timeline. Neither measurement recreates capture state or owns game input.
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
from .model import Token
from .forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner, TargetTimeAlignment
from .g2p import PronunciationCandidate
from .process_audio import CaptureHealth, ProcessCapture
from .audio_evidence import LexicalProgressEvidence
from .utterance_progress import UtteranceProgress, evaluate_utterance_progress


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
    lexical: LexicalProgressEvidence | None
    causal_lower_bound_ms: int | None
    input_candidates_ms: tuple[int, ...]
    target_attached_ms: int | None
    lexical_revision: int


@dataclass(frozen=True)
class _AssessmentFence:
    """One captured frontier that must be settled before an optional callback."""

    boundary_ms: int
    on_settled: Callable[[Token], None] | None = None


def _assessment_diagnostics(
    item: _Assessment, progress: UtteranceProgress, vocal,
    vocal_diagnostics: list[dict], recurrence,
) -> dict:
    lexical = item.lexical
    representative = (
        max(recurrence, key=lambda candidate: (candidate.prominence, candidate.strength))
        if recurrence else None
    )
    return {
        "target_units": lexical.target_units if lexical is not None else 0,
        "observed_units": lexical.observed_units if lexical is not None else 0,
        "aligned_target_units": lexical.aligned_target_units if lexical is not None else 0,
        "target_associated": lexical.target_associated if lexical is not None else False,
        "complete_alignment": lexical.complete_alignment if lexical is not None else False,
        "committed_target_end": lexical.committed_target_end if lexical is not None else False,
        "lexical_target_start_ms": lexical.target_start_ms if lexical is not None else None,
        "lexical_aligned_end_ms": lexical.aligned_end_ms if lexical is not None else None,
        "lexical_target_end_ms": lexical.target_end_ms if lexical is not None else None,
        "evidence_at_ms": progress.evidence_at_ms,
        "causal_lower_bound_ms": item.causal_lower_bound_ms,
        "input_candidates_ms": list(item.input_candidates_ms),
        "target_attached_ms": item.target_attached_ms,
        "vocal_series": vocal_diagnostics,
        "vocal_activity": ([{
            "observer": series.observer,
            "start_ms": interval.start_ms,
            "last_active_ms": interval.last_active_ms,
            "ongoing": interval.ongoing,
            "inactive_start_ms": interval.inactive_start_ms,
            "inactive_end_ms": interval.inactive_end_ms,
            "inactive_required_ms": interval.inactive_required_ms,
        } for series in vocal.series for interval in series.activity]
            if vocal is not None else []),
        "recurrence_strength": representative.strength if representative is not None else 0.0,
        "recurrence_period_ms": representative.period_ms if representative is not None else None,
        "recurrence_prominence": representative.prominence if representative is not None else 0.0,
        "recurrence_candidates": [{
            "observer": candidate.observer,
            "strength": candidate.strength,
            "period_ms": candidate.period_ms,
            "prominence": candidate.prominence,
        } for candidate in recurrence],
    }


class NativeAudio:
    """Measure one process PCM timeline and publish integrated utterance progress."""

    RETENTION_MS = 60_000
    INFERENCE_INTERVAL_MS = 500
    MIN_AUDIO_MS = 300
    # Unassociated target discovery is always bounded. Retention is search
    # capacity, not one inference unit; older PCM is visited by overlapping
    # windows while current-frontier assessment remains independently usable.
    INITIAL_SEARCH_WINDOW_MS = 8_000
    HISTORICAL_SEARCH_OVERLAP_MS = INITIAL_SEARCH_WINDOW_MS // 2

    def __init__(self, pid: int, helper: Path, model: Path, vocab: Path,
                 on_error: Callable[[Token | None, str], None],
                 *, on_progress: Callable[[Token, UtteranceProgress, dict], None],
                 model_backend=None, g2p=None, clock=None,
                 human_vocal_model: Path | None = None,
                 vocal_observer: VocalActivityObserver | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None):
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
        self._confirmed_search_floor_ms: int | None = None
        self._history_search_end_ms: int | None = None
        self._lexical: LexicalProgressEvidence | None = None
        self._lexical_revision = 0
        self._alignment_ready_through_ms: int | None = None

        # One scheduler owns alignment work. The PCM frontier remains in
        # AudioHistory; no second pending/requested frontier is maintained here.
        self._alignment_inflight_generation: int | None = None
        self._last_alignment_query: tuple[int, int] | None = None

        # Continuous human-vocal observation follows AudioHistory itself. There
        # is no second PCM queue or replay path.
        self._observer_cursor_ms: int | None = None
        self._observer_running = False
        self._observer_revision = 0

        # Explicit assessment obligations synchronize observer/alignment work only
        # through captured PCM; they never require exhausting retained history.
        self._assessment_fence: _AssessmentFence | None = None
        self._last_progress_key: tuple[int, int] | None = None

        self._generation = 0
        self._closed = False
        self._capture_error: str | None = None

    def start(self) -> None:
        self._capture.start()

    def _take_settlement_locked(self) -> Callable[[Token], None] | None:
        fence = self._assessment_fence
        self._assessment_fence = None
        return fence.on_settled if fence is not None else None

    def _notify_failure(
        self, token: Token, reason: str, settled: Callable[[Token], None] | None,
    ) -> None:
        self.on_error(token, reason)
        if settled is not None:
            settled(token)

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

    def _search_lower_bound_locked(self, end_ms: int) -> int:
        """Return the current factual lower bound for alignment search.

        Retention and AutoAdv's own successful input are hard bounds. A later
        bound may be adopted only after target association is established; weak
        phone fragments and exhausted queries never become search ownership.
        """
        minimum = self._minimum_search_start_locked(end_ms)
        if self._confirmed_search_floor_ms is not None:
            minimum = max(minimum, int(self._confirmed_search_floor_ms))
        return minimum

    def _initial_search_start_locked(self, end_ms: int) -> int:
        minimum = self._minimum_search_start_locked(end_ms)
        start = max(minimum, int(end_ms) - self.INITIAL_SEARCH_WINDOW_MS)
        usable = [
            value for value in self.input_candidates_ms
            if minimum <= value <= int(end_ms) - self.MIN_AUDIO_MS
        ]
        if usable:
            # External input is only a search seed. Failure may expand behind it.
            start = max(start, max(usable))
        return min(start, int(end_ms))

    def _history_overlap_ms(self, span_ms: int) -> int:
        span = max(self.MIN_AUDIO_MS, int(span_ms))
        return min(
            self.HISTORICAL_SEARCH_OVERLAP_MS,
            max(self.MIN_AUDIO_MS, span // 2),
            span,
        )

    def _start_history_after_query_locked(self, query: tuple[int, int]) -> None:
        """Expose older retained PCM without enlarging the completed query."""
        if self._history_search_end_ms is not None:
            return
        start, end = (int(query[0]), int(query[1]))
        minimum = self._search_lower_bound_locked(end)
        if start <= minimum:
            self._history_search_end_ms = minimum
            return
        overlap = self._history_overlap_ms(end - start)
        self._history_search_end_ms = min(end, start + overlap)

    def _advance_history_search_locked(self, query: tuple[int, int], frontier: int) -> None:
        """Move one bounded historical window backward after a miss."""
        start, end = (int(query[0]), int(query[1]))
        minimum = self._search_lower_bound_locked(frontier)
        if start <= minimum:
            self._history_search_end_ms = minimum
            return
        overlap = self._history_overlap_ms(end - start)
        self._history_search_end_ms = max(minimum, start + overlap)

    def _history_query_locked(self, frontier: int) -> tuple[int, int] | None:
        end = self._history_search_end_ms
        if end is None:
            return None
        minimum = self._search_lower_bound_locked(frontier)
        end = max(minimum, min(int(frontier), int(end)))
        if end <= minimum:
            self._history_search_end_ms = minimum
            return None
        start = max(minimum, end - self.INITIAL_SEARCH_WINDOW_MS)
        if end - start < self.MIN_AUDIO_MS:
            self._history_search_end_ms = minimum
            return None
        return (start, end)

    def _adopt_associated_search_floor_locked(
        self, lexical: LexicalProgressEvidence, *, query_start_ms: int, capture_end_ms: int,
    ) -> None:
        if not lexical.target_associated or lexical.target_start_ms is None:
            return
        minimum = self._minimum_search_start_locked(capture_end_ms)
        # The bounded snapshot that actually established association is factual
        # sufficient context. Reuse its start rather than an earlier input time or
        # the entire retained history on every later target-follow query.
        desired = max(minimum, int(query_start_ms))
        confirmed = (
            int(desired) if self._confirmed_search_floor_ms is None
            else max(int(self._confirmed_search_floor_ms), int(desired))
        )
        self._confirmed_search_floor_ms = confirmed

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
            self.token = token
            self.aligner = aligner
            self.causal_lower_bound_ms = lower_bound
            self.input_candidates_ms = input_candidates
            self.target_attached_ms = int(self.clock())
            self._lexical = None
            self._lexical_revision += 1
            self._alignment_ready_through_ms = None if aligner is not None else self.history.latest_ms
            self._last_alignment_query = None
            self._last_progress_key = None
            self._assessment_fence = None
            self._confirmed_search_floor_ms = None
            self._history_search_end_ms = None

            frontier = self.history.latest_ms
            if frontier is not None:
                # Attaching a target obligates one integrated assessment through
                # all PCM already retained at that moment. Hook timing must not
                # make a partially processed observer frontier the product result.
                self._assessment_fence = _AssessmentFence(int(frontier))
            if frontier is not None:
                if aligner is not None:
                    alignment_work = self._schedule_alignment_locked()
                submit_observer = self._wake_observer_locked()

        self._submit_alignment(alignment_work)
        if submit_observer:
            self._submit_observer()
        with self._lock:
            failure = self._capture_error if self.token == token else None
        if failure:
            self.on_error(token, failure)

    def _retire_target_locked(self) -> None:
        self.token = None
        self.aligner = None
        self.causal_lower_bound_ms = None
        self.input_candidates_ms = ()
        self.target_attached_ms = None
        self._lexical = None
        self._confirmed_search_floor_ms = None
        self._history_search_end_ms = None
        self._alignment_ready_through_ms = None
        self._assessment_fence = None
        self._last_progress_key = None

    def finish_line(self, token: Token) -> bool:
        with self._lock:
            if token != self.token:
                return False
            self._retire_target_locked()
            return True

    def cancel(self) -> None:
        with self._lock:
            if self.token is not None or self.aligner is not None:
                self._retire_target_locked()

    def health(self, status: CaptureHealth) -> None:
        """Handle transport failure; healthy heartbeats carry no analysis work."""
        if status.healthy:
            return
        with self._lock:
            self._capture_error = status.reason
            token = self.token
            settled = self._take_settlement_locked()
            self._observer_revision += 1
            self._observer_cursor_ms = None
        if token is not None:
            self._notify_failure(token, status.reason, settled)

    def request_assessment_settlement(
        self, token: Token, on_settled: Callable[[Token], None],
    ) -> bool:
        """Settle all measurements through the PCM frontier captured now.

        The caller does not own or receive the capture boundary. Once observer
        and lexical work both cover that internal frontier, the same integrated
        inference is published through the ordinary progress callback, followed
        by the one-shot settlement callback. If capture/inference is already
        unavailable, no obligation is armed and the caller may continue without
        waiting for acoustic work.
        """
        alignment_work = None
        submit_observer = False
        with self._lock:
            if self._closed or token != self.token or self._capture_error:
                return False
            frontier = self.history.latest_ms
            if frontier is None:
                return False
            boundary = int(frontier)
            current = self._assessment_fence
            self._assessment_fence = _AssessmentFence(
                boundary_ms=max(boundary, current.boundary_ms if current is not None else boundary),
                on_settled=on_settled,
            )
            if self.aligner is not None:
                alignment_work = self._schedule_alignment_locked()
            submit_observer = self._wake_observer_locked()
        self._submit_alignment(alignment_work)
        if submit_observer:
            self._submit_observer()
        return True

    def feed(self, chunk: PCMChunk) -> None:
        """Capture callback: wake timeline consumers and return without inference."""
        alignment_work = None
        submit_observer = False
        with self._lock:
            if self._closed:
                return
            submit_observer = self._wake_observer_locked()
            if self.token is not None and self.aligner is not None:
                alignment_work = self._schedule_alignment_locked()
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
                    settled = self._take_settlement_locked()
                    self._observer_running = False
                if token is not None:
                    self._notify_failure(
                        token, "human-vocal observer failure: " + str(error), settled)

    def _fence_ready_locked(self, boundary_ms: int) -> bool:
        observer = self._observer_cursor_ms
        if observer is None or int(observer) < int(boundary_ms):
            return False
        if self.aligner is None:
            return True
        alignment = self._alignment_ready_through_ms
        return alignment is not None and int(alignment) >= int(boundary_ms)

    def _ready_boundary_locked(self) -> int | None:
        if self.token is None or self._observer_cursor_ms is None:
            return None
        observer = int(self._observer_cursor_ms)

        fence = self._assessment_fence
        if fence is not None:
            boundary = int(fence.boundary_ms)
            # An explicit assessment fence is the only point where both
            # measurements must cover the same captured boundary. Ordinary
            # human-vocal updates must not be frozen by slower alignment work.
            if not self._fence_ready_locked(boundary):
                return None
            return boundary

        # Outside an explicit fence, the continuously measured acoustic frontier
        # is the assessment frontier. A later alignment result increments the
        # lexical revision and causes the same frontier to be assessed again.
        if self._last_progress_key is not None:
            previous, revision = self._last_progress_key
            if (revision == self._lexical_revision
                    and observer - int(previous) < self.INFERENCE_INTERVAL_MS):
                return None
        return observer

    def _assessment_locked(self) -> _Assessment | None:
        boundary = self._ready_boundary_locked()
        if boundary is None or self.token is None:
            return None
        key = (int(boundary), self._lexical_revision)
        # A requested fence may target a boundary that was already assessed.
        # Re-emitting that result is the settlement acknowledgement.
        if key == self._last_progress_key and self._assessment_fence is None:
            return None
        return _Assessment(
            token=self.token,
            generation=self._generation,
            boundary_ms=int(boundary),
            lexical=self._lexical,
            causal_lower_bound_ms=self.causal_lower_bound_ms,
            input_candidates_ms=self.input_candidates_ms,
            target_attached_ms=self.target_attached_ms,
            lexical_revision=self._lexical_revision,
        )

    def _run_assessment(self, item: _Assessment) -> None:
        vocal = None
        vocal_diagnostics: list[dict] = []
        needs_vocal = bool(
            item.causal_lower_bound_ms is not None
            or item.input_candidates_ms
            or (item.lexical is not None
                and item.lexical.target_associated
                and item.lexical.target_start_ms is not None)
        )
        feature_start = self.features.earliest_ms if needs_vocal else None
        if feature_start is not None:
            # Activity intervals preserve their measured onset in the cache, so
            # querying the retained range cannot recreate an old activity as a
            # synthetic post-Presentation onset.
            vocal = self.features.summarize_vocal(
                start_ms=int(feature_start), end_ms=int(item.boundary_ms))
            vocal_diagnostics = self.features.vocal_diagnostics(
                start_ms=int(feature_start), end_ms=int(item.boundary_ms))

        def recurrence_lookup(start_ms: int, end_ms: int):
            return self.features.recurrences(
                start_ms=int(start_ms),
                end_ms=int(end_ms),
            )

        progress, recurrence = evaluate_utterance_progress(
            capture_end_ms=item.boundary_ms,
            lexical=item.lexical,
            vocal=vocal,
            causal_lower_bound_ms=item.causal_lower_bound_ms,
            input_candidates_ms=item.input_candidates_ms,
            recurrence_lookup=recurrence_lookup,
        )
        diagnostics = _assessment_diagnostics(
            item, progress, vocal, vocal_diagnostics, recurrence)

        settled = None
        with self._lock:
            if (self._closed or item.generation != self._generation
                    or item.token != self.token
                    or item.lexical_revision != self._lexical_revision):
                return
            key = (item.boundary_ms, item.lexical_revision)
            fence = self._assessment_fence
            if fence is not None:
                boundary = int(fence.boundary_ms)
                # Work that began before a newer fence was requested cannot
                # acknowledge it until both measurements cover that frontier.
                if item.boundary_ms < boundary or not self._fence_ready_locked(boundary):
                    return
            if key == self._last_progress_key and fence is None:
                return
            self._last_progress_key = key
            if fence is not None:
                self._assessment_fence = None
                settled = fence.on_settled
        self.on_progress(item.token, progress, diagnostics)
        if settled is not None:
            settled(item.token)

        # Releasing a required fence may expose optional retained-history search.
        # Resume it only after Controller has consumed the assessment, because
        # Controller may complete the Presentation and retire the target.
        next_work = None
        with self._lock:
            if (not self._closed and item.generation == self._generation
                    and item.token == self.token):
                next_work = self._schedule_alignment_locked()
        self._submit_alignment(next_work)

    def _target_associated_locked(self) -> bool:
        lexical = self._lexical
        return bool(
            lexical is not None
            and lexical.target_associated
            and lexical.target_start_ms is not None
        )

    def _next_alignment_query_locked(
        self,
    ) -> tuple[tuple[int, int], int | None] | None:
        frontier = self.history.latest_ms
        if frontier is None:
            return None
        frontier = int(frontier)
        ready = self._alignment_ready_through_ms
        fence = self._assessment_fence
        associated = self._target_associated_locked()

        # A required fence has priority over optional historical discovery. One
        # bounded frontier measurement acknowledges it; older retained windows
        # remain searchable but do not become a fourth product state.
        if fence is not None:
            boundary = min(int(fence.boundary_ms), frontier)
            if ready is not None and int(ready) >= boundary:
                return None
            start = (
                self._search_lower_bound_locked(boundary)
                if associated else self._initial_search_start_locked(boundary)
            )
            return ((start, boundary), boundary)

        if associated:
            if ready is not None and frontier - int(ready) < self.INFERENCE_INTERVAL_MS:
                return None
            # Once the target is located, do not search older unrelated history.
            # The HuBERT backend is snapshot based, so following a still-open target
            # reuses its confirmed floor and grows only with that target itself.
            return ((self._search_lower_bound_locked(frontier), frontier), frontier)

        current_due = bool(
            ready is None or frontier - int(ready) >= self.INFERENCE_INTERVAL_MS
        )
        if current_due:
            return ((self._initial_search_start_locked(frontier), frontier), frontier)

        history_query = self._history_query_locked(frontier)
        if history_query is not None:
            return (history_query, None)
        return None

    def _schedule_alignment_locked(self):
        """Build one target-discovery query or one target-follow query."""
        if (self._closed or self.token is None or self.aligner is None
                or self._alignment_inflight_generation is not None):
            return None

        # Historical windows may fall entirely inside a capture gap. Advance past
        # such empty ranges here instead of waiting for unrelated future PCM to
        # wake the scheduler again.
        while True:
            planned = self._next_alignment_query_locked()
            if planned is None:
                return None
            query, assessment_boundary = planned
            start, boundary = (int(query[0]), int(query[1]))
            if boundary - start < self.MIN_AUDIO_MS:
                if assessment_boundary is not None:
                    self._alignment_ready_through_ms = max(
                        int(assessment_boundary),
                        self._alignment_ready_through_ms
                        if self._alignment_ready_through_ms is not None
                        else int(assessment_boundary),
                    )
                return None
            if query == self._last_alignment_query:
                return None

            segments = self.history.read(start, boundary).spans
            if segments:
                break
            if assessment_boundary is not None:
                self._alignment_ready_through_ms = max(
                    int(assessment_boundary),
                    self._alignment_ready_through_ms
                    if self._alignment_ready_through_ms is not None
                    else int(assessment_boundary),
                )
                return None
            self._advance_history_search_locked(
                query, int(self.history.latest_ms or boundary))

        self._last_alignment_query = query
        generation = self._generation
        self._alignment_inflight_generation = generation
        requested_ns = time.perf_counter_ns() if self.on_inference else 0
        return (self.token, generation, segments, self.aligner, requested_ns,
                query, assessment_boundary)

    def _submit_alignment(self, work) -> None:
        if work is None:
            return
        token, generation, segments, aligner, requested_ns, query, assessment_boundary = work
        try:
            self._alignment_pool.submit(
                self._infer, token, generation, segments, aligner, requested_ns,
                query, assessment_boundary)
        except RuntimeError:
            next_work = None
            with self._lock:
                if self._alignment_inflight_generation == generation:
                    self._alignment_inflight_generation = None
                    next_work = self._schedule_alignment_locked()
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
        query: tuple[int, int], assessment_boundary: int | None,
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
                    historical = assessment_boundary is None
                    previously_associated = self._target_associated_locked()

                    if associated and lexical is not None:
                        if lexical != self._lexical:
                            self._lexical = lexical
                            self._lexical_revision += 1
                        self._adopt_associated_search_floor_locked(
                            lexical,
                            query_start_ms=int(query[0]),
                            capture_end_ms=boundary,
                        )
                        self._history_search_end_ms = None

                        if selection.alignment.committed_target_end:
                            self.aligner = None
                        elif historical:
                            # Historical discovery can revise a prior UNKNOWN, but a
                            # partial target must then be followed toward the current
                            # PCM frontier before that newer frontier is considered
                            # alignment-ready.
                            self._alignment_ready_through_ms = (
                                boundary if self._alignment_ready_through_ms is None
                                else min(int(self._alignment_ready_through_ms), boundary)
                            )
                    elif historical:
                        if selection.ambiguous:
                            # Multiple supported continuity spans are not resolved by
                            # searching farther toward an arbitrary first/last match.
                            self._history_search_end_ms = self._search_lower_bound_locked(
                                int(self.history.latest_ms or boundary))
                        else:
                            self._advance_history_search_locked(
                                query, int(self.history.latest_ms or boundary))
                    else:
                        # This bounded query is factual evidence at its capture
                        # frontier even when no target is associated. Optional older
                        # history may later revise it; assessment does not wait for
                        # retained-history exhaustion.
                        if lexical is not None and not previously_associated:
                            if lexical != self._lexical:
                                self._lexical = lexical
                                self._lexical_revision += 1
                        if not associated and not selection.ambiguous:
                            self._start_history_after_query_locked(query)
                        elif selection.ambiguous:
                            self._history_search_end_ms = self._search_lower_bound_locked(boundary)

                    if assessment_boundary is not None:
                        self._alignment_ready_through_ms = max(
                            int(assessment_boundary),
                            self._alignment_ready_through_ms
                            if self._alignment_ready_through_ms is not None
                            else int(assessment_boundary),
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
                            "continuity_spans": len(segments),
                            "ambiguous_alignment_spans": bool(selection.ambiguous),
                        }
                        if chosen is not None:
                            fit = chosen.fit
                            metrics.update({
                                "frame_count": chosen.frame_count,
                                "target_units": chosen.target_units,
                                "observed_units": chosen.observed_units,
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
        except Exception as exc:
            with self._lock:
                settled = self._take_settlement_locked()
            self._notify_failure(
                token, "acoustic inference failure: " + str(exc), settled)
        finally:
            with self._lock:
                if self._alignment_inflight_generation == generation:
                    self._alignment_inflight_generation = None
                    next_work = self._schedule_alignment_locked()
                    wake_observer = self._wake_observer_locked() or wake_observer
            if wake_observer:
                self._submit_observer()
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
