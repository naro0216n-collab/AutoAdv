"""Native-audio evidence and integrated progression assessment.

Measurements retain their own provenance.  This module combines lexical,
human-vocal, temporal and optional recurrence evidence exactly once; it does not
reconstruct source identity or reinterpret transport facts as endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .forced_alignment import TargetTimeAlignment


@dataclass(frozen=True)
class VocalScoreSample:
    """One observer score with the exact input support that produced it."""

    at_ms: int
    score: float
    support_start_ms: int
    component_scores: tuple[float, ...] = ()


@dataclass(frozen=True)
class VocalEvidenceSeries:
    """One observer's human-vocal measurements on its own score scale."""

    observer: str
    active_score_threshold: float = 0.45
    samples: tuple[VocalScoreSample, ...] = ()


@dataclass(frozen=True)
class HumanVocalEvidence:
    """Human-vocal measurements grouped only by observer provenance."""

    series: tuple[VocalEvidenceSeries, ...] = ()


@dataclass(frozen=True)
class VocalRecurrenceEvidence:
    """Repeated structure measured from a human-vocal observer time series."""

    strength: float = 0.0
    period_ms: int | None = None
    period_stability: float = 0.0
    recent_strength: float = 0.0
    level_consistency: float = 0.0


class UtteranceProgressState(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class LexicalProgressEvidence:
    """Target-conditioned alignment measurement for one PCM query."""

    target_units: int
    aligned_target_units: int
    target_associated: bool
    complete_alignment: bool
    committed_target_end: bool
    target_start_ms: int | None
    aligned_end_ms: int | None
    target_end_ms: int | None
    reading_rank: int
    path_margin: float
    boundary_margin: float

    @classmethod
    def from_alignment(
        cls,
        alignment: TargetTimeAlignment,
        *,
        target_start_ms: int | None,
        aligned_end_ms: int | None,
        target_end_ms: int | None,
    ) -> "LexicalProgressEvidence":
        return cls(
            target_units=alignment.target_units,
            aligned_target_units=alignment.aligned_target_units,
            target_associated=alignment.target_associated,
            complete_alignment=alignment.complete_alignment,
            committed_target_end=alignment.committed_target_end,
            target_start_ms=target_start_ms,
            aligned_end_ms=aligned_end_ms,
            target_end_ms=target_end_ms,
            reading_rank=alignment.reading_rank,
            path_margin=alignment.fit.path_margin,
            boundary_margin=alignment.fit.boundary_margin,
        )


@dataclass(frozen=True)
class NativeAudioObservation:
    """Factual measurements published alongside one integrated progress result."""

    lexical: LexicalProgressEvidence | None = None
    vocal: HumanVocalEvidence | None = None
    recurrence: VocalRecurrenceEvidence | None = None
    causal_lower_bound_ms: int | None = None
    input_candidates_ms: tuple[int, ...] = ()
    target_attached_ms: int | None = None


@dataclass(frozen=True)
class UtteranceProgress:
    """One integrated answer consumed by Controller."""

    state: UtteranceProgressState = UtteranceProgressState.UNKNOWN
    evidence_at_ms: int | None = None

    @property
    def ended(self) -> bool:
        return self.state == UtteranceProgressState.ENDED

    @property
    def active(self) -> bool:
        return self.state == UtteranceProgressState.ACTIVE

    @property
    def unknown(self) -> bool:
        return self.state == UtteranceProgressState.UNKNOWN


LOOP_STRENGTH = 0.78
LOOP_STABILITY = 0.60
RECENT_LOOP_STRENGTH = 0.72
LOOP_LEVEL_CONSISTENCY = 0.55

# A threshold crossing is a measurement, not an endpoint. Releasing an already
# observed finite-vocal risk requires a contiguous span of explicitly measured
# sub-threshold support, never wall-clock passage alone.
MIN_QUIET_SUPPORT_MS = 240


@dataclass
class _ActivityRun:
    start_ms: int
    support_start_ms: int
    last_positive_ms: int
    quiet_required_ms: int
    quiet_start_ms: int | None = None
    quiet_end_ms: int | None = None

    @property
    def open(self) -> bool:
        return (self.quiet_start_ms is None or self.quiet_end_ms is None
                or self.quiet_end_ms - self.quiet_start_ms < self.quiet_required_ms)

    def closed_after(self, boundary_ms: int) -> bool:
        if self.open or self.quiet_start_ms is None or self.quiet_end_ms is None:
            return False
        return (self.quiet_end_ms - max(self.quiet_start_ms, int(boundary_ms))
                >= self.quiet_required_ms)


def _activity_runs(series: VocalEvidenceSeries) -> tuple[_ActivityRun, ...]:
    """Build observer-local runs once from measured support intervals.

    Positive support opens or continues a run. Sub-threshold support closes it
    only after enough contiguous measured quiet from the same observer. A later
    positive after closure starts a new run; short valleys remain inside the run.
    """
    samples = tuple(sorted(
        series.samples,
        key=lambda sample: (int(sample.at_ms), int(sample.support_start_ms)),
    ))
    required = max(MIN_QUIET_SUPPORT_MS, max(
        (int(sample.at_ms) - int(sample.support_start_ms) for sample in samples), default=0))
    threshold = float(series.active_score_threshold)
    runs: list[_ActivityRun] = []
    current: _ActivityRun | None = None

    for sample in samples:
        at_ms = int(sample.at_ms)
        support_start = int(sample.support_start_ms)
        positive = float(sample.score) >= threshold

        if positive:
            if current is None or not current.open:
                current = _ActivityRun(at_ms, support_start, at_ms, required)
                runs.append(current)
            else:
                current.last_positive_ms = at_ms
                current.quiet_start_ms = current.quiet_end_ms = None
            continue

        if current is None:
            continue
        if not current.open:
            if (current.quiet_end_ms is not None and support_start <= current.quiet_end_ms
                    and at_ms > support_start):
                current.quiet_end_ms = max(current.quiet_end_ms, at_ms)
            continue
        if support_start < current.last_positive_ms or at_ms <= support_start:
            continue

        if (current.quiet_start_ms is None or current.quiet_end_ms is None
                or support_start > current.quiet_end_ms):
            current.quiet_start_ms, current.quiet_end_ms = support_start, at_ms
        else:
            current.quiet_end_ms = max(current.quiet_end_ms, at_ms)

    return tuple(runs)


def _activity_context_after(
    series: VocalEvidenceSeries, *, timing_fact_ms: int,
) -> int | None:
    """Return existing activity context when current post-fact risk is observed.

    Physical activity runs stay observer-local.  A run that begins after the
    timing fact is directly relevant.  When the same physical run crossed the
    fact, measured post-fact low support followed by a later rise is the temporal
    evidence that a new finite-vocal risk may have appeared.  The returned start
    keeps the run's pre-fact context available to recurrence measurement;
    otherwise periodic activity is made artificially non-periodic
    by cropping it at the latest rise.
    """
    runs = _activity_runs(series)
    if not runs or not runs[-1].open:
        return None
    current = runs[-1]
    fact = int(timing_fact_ms)
    if current.last_positive_ms <= fact:
        return None
    if current.start_ms > fact:
        return current.start_ms

    threshold = float(series.active_score_threshold)
    quiet_start: int | None = None
    quiet_end: int | None = None
    for sample in sorted(
        series.samples,
        key=lambda item: (int(item.at_ms), int(item.support_start_ms)),
    ):
        at_ms = int(sample.at_ms)
        if at_ms <= fact or at_ms > current.last_positive_ms:
            continue
        if float(sample.score) >= threshold:
            if (quiet_start is not None and quiet_end is not None
                    and quiet_end - quiet_start >= MIN_QUIET_SUPPORT_MS):
                return current.start_ms
            quiet_start = quiet_end = None
            continue

        support_start = max(fact, int(sample.support_start_ms))
        if at_ms <= support_start:
            continue
        if quiet_start is None or quiet_end is None or support_start > quiet_end:
            quiet_start, quiet_end = support_start, at_ms
        else:
            quiet_end = max(quiet_end, at_ms)

    return None


def _target_related_runs(
    vocal: HumanVocalEvidence | None, *, target_start_ms: int, target_observed_end_ms: int,
) -> tuple[_ActivityRun, ...]:
    """Follow one target-related finite activity episode across observers.

    Each observer keeps its own score scale and contributes only its latest run
    intersecting the located target, matching the existing lexical relation. A
    later run from any observer joins only while measured quiet has not yet been
    sufficient to separate that episode.
    """
    all_runs: list[_ActivityRun] = []
    related: list[_ActivityRun] = []
    for series in vocal.series if vocal is not None else ():
        runs = _activity_runs(series)
        all_runs.extend(runs)
        run = next((
            item for item in reversed(runs)
            if item.last_positive_ms >= int(target_start_ms)
            and item.support_start_ms <= int(target_observed_end_ms)
        ), None)
        if run is not None:
            related.append(run)
    if not related:
        return ()

    def continuation_until(run: _ActivityRun) -> int:
        return (
            int(run.last_positive_ms) if run.quiet_start_ms is None
            else int(run.quiet_start_ms) + int(run.quiet_required_ms)
        )

    related_ids = {id(run) for run in related}
    frontier = max(continuation_until(run) for run in related)
    for run in sorted(all_runs, key=lambda item: item.start_ms):
        if id(run) in related_ids or run.last_positive_ms <= int(target_observed_end_ms):
            continue
        if run.start_ms > frontier:
            break
        related.append(run)
        related_ids.add(id(run))
        frontier = max(frontier, continuation_until(run))
    return tuple(related)


def _stable_recurrence(recurrence: VocalRecurrenceEvidence | None) -> bool:
    if recurrence is None or recurrence.period_ms is None:
        return False
    return bool(
        recurrence.strength >= LOOP_STRENGTH
        and recurrence.period_stability >= LOOP_STABILITY
        and recurrence.recent_strength >= RECENT_LOOP_STRENGTH
        and recurrence.level_consistency >= LOOP_LEVEL_CONSISTENCY
    )


def evaluate_utterance_progress(
    *,
    capture_end_ms: int | None,
    lexical: LexicalProgressEvidence | None,
    vocal: HumanVocalEvidence | None,
    causal_lower_bound_ms: int | None = None,
    input_candidates_ms: tuple[int, ...] = (),
    recurrence_lookup: Callable[[int, int], VocalRecurrenceEvidence | None] | None = None,
) -> tuple[UtteranceProgress, VocalRecurrenceEvidence | None]:
    """Produce one stateless progression result from factual measurements."""
    if capture_end_ms is None:
        return UtteranceProgress(evidence_at_ms=None), None
    capture_end = int(capture_end_ms)

    def recurrence_for(start_ms: int) -> VocalRecurrenceEvidence | None:
        if recurrence_lookup is None:
            return None
        return recurrence_lookup(int(start_ms), capture_end)

    lexical_span = bool(
        lexical is not None
        and lexical.aligned_target_units > 0
        and lexical.target_start_ms is not None
        and lexical.aligned_end_ms is not None
    )

    if lexical_span and lexical is not None:
        target_start = int(lexical.target_start_ms)
        complete = bool(
            lexical.target_associated
            and lexical.complete_alignment
            and lexical.committed_target_end
            and lexical.target_end_ms is not None
        )
        observed_target_end = int(
            lexical.target_end_ms if complete else lexical.aligned_end_ms)
        measured = _target_related_runs(
            vocal, target_start_ms=target_start, target_observed_end_ms=observed_target_end)

        if not measured:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end), None

        if complete:
            target_end = int(lexical.target_end_ms)
            if any(not run.closed_after(target_end) for run in measured):
                recurrence = recurrence_for(target_end)
                if _stable_recurrence(recurrence):
                    return UtteranceProgress(
                        state=UtteranceProgressState.UNKNOWN,
                        evidence_at_ms=capture_end), recurrence
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE,
                    evidence_at_ms=capture_end), recurrence
            return UtteranceProgress(
                state=UtteranceProgressState.ENDED,
                evidence_at_ms=capture_end), None

        # Partial target evidence remains protective only while its measured
        # activity run is still open. Incremental alignment continues in
        # NativeAudio as new PCM arrives; this domain does not manufacture a
        # reading-count-derived hold time.
        return UtteranceProgress(
            state=(UtteranceProgressState.ACTIVE
                   if any(run.open for run in measured)
                   else UtteranceProgressState.UNKNOWN),
            evidence_at_ms=capture_end), None

    timing_facts = (int(causal_lower_bound_ms),) if causal_lower_bound_ms is not None else tuple(
        sorted({int(value) for value in input_candidates_ms if int(value) > 0}))
    if not timing_facts:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    recurrence_contexts = [
        context
        for series in vocal.series if vocal is not None
        for fact in timing_facts
        if (context := _activity_context_after(
            series, timing_fact_ms=fact)) is not None
    ]

    if not recurrence_contexts:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    # Recurrence needs the existing structure that made the current rise
    # interpretable. Cropping at the rise would discard evidence that the same
    # structure already repeated across the Presentation boundary.
    recurrence_start = min(recurrence_contexts)
    recurrence = recurrence_for(recurrence_start)
    if _stable_recurrence(recurrence):
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), recurrence
    return UtteranceProgress(
        state=UtteranceProgressState.ACTIVE,
        evidence_at_ms=capture_end), recurrence
