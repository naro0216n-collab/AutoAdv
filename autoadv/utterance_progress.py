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


def _series_activity_runs(series: VocalEvidenceSeries) -> tuple[_ActivityRun, ...]:
    """Build one observer's finite activity runs from measured support only.

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


def _post_fact_activity_start(
    series: VocalEvidenceSeries, run: _ActivityRun, *, timing_fact_ms: int,
) -> int | None:
    """Return the first measured activity change after one timing fact.

    The observer owns physical run segmentation. A run that starts after the
    fact is directly usable. If the same run already crossed the fact, only a
    measured low-score span followed by a later rise establishes a new
    post-fact change. This relates measurements in time without assigning the
    sound to a source, speaker, or target.
    """
    fact = int(timing_fact_ms)
    if run.last_positive_ms <= fact:
        return None
    if run.start_ms >= fact:
        return int(run.start_ms)

    threshold = float(series.active_score_threshold)
    quiet_start: int | None = None
    quiet_end: int | None = None
    for sample in sorted(
        series.samples,
        key=lambda item: (int(item.at_ms), int(item.support_start_ms)),
    ):
        at_ms = int(sample.at_ms)
        if at_ms <= fact or at_ms > run.last_positive_ms:
            continue
        if float(sample.score) >= threshold:
            if (quiet_start is not None and quiet_end is not None
                    and quiet_end - quiet_start >= MIN_QUIET_SUPPORT_MS):
                return at_ms
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


def _timing_facts(
    *, lexical: LexicalProgressEvidence | None, causal_lower_bound_ms: int | None,
    input_candidates_ms: tuple[int, ...],
) -> tuple[int, ...]:
    """Choose factual time anchors without promoting them to source labels."""
    if causal_lower_bound_ms is not None:
        return (int(causal_lower_bound_ms),)

    target_start = None
    if (lexical is not None and lexical.target_associated
            and lexical.aligned_target_units > 0
            and lexical.target_start_ms is not None):
        target_start = int(lexical.target_start_ms)

    candidates = tuple(sorted({
        int(value) for value in input_candidates_ms if int(value) > 0
    }))
    if target_start is not None:
        earlier = tuple(value for value in candidates if value <= target_start)
        return (max(earlier) if earlier else target_start,)
    return candidates


def _observed_activity_after(
    series: VocalEvidenceSeries, *, timing_facts: tuple[int, ...],
) -> tuple[tuple[_ActivityRun, int], ...]:
    """Return observer runs with a measured post-anchor activity change."""
    if not timing_facts:
        return ()
    observed: list[tuple[_ActivityRun, int]] = []
    for run in _series_activity_runs(series):
        starts = [
            start
            for fact in timing_facts
            if (start := _post_fact_activity_start(
                series, run, timing_fact_ms=fact)) is not None
        ]
        if starts:
            observed.append((run, min(starts)))
    return tuple(observed)


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
    recurrence_lookup: Callable[[str, int, int], VocalRecurrenceEvidence | None] | None = None,
) -> tuple[UtteranceProgress, VocalRecurrenceEvidence | None]:
    """Combine factual measurements once into cut-risk / end / unknown.

    Human-vocal measurements are never assigned to a target or source. Known
    text alignment and input observations only provide time anchors and, when
    committed, a textual end candidate.
    """
    if capture_end_ms is None:
        return UtteranceProgress(evidence_at_ms=None), None
    capture_end = int(capture_end_ms)

    target_located = bool(
        lexical is not None
        and lexical.target_associated
        and lexical.aligned_target_units > 0
        and lexical.target_start_ms is not None
        and lexical.aligned_end_ms is not None
    )
    target_complete = bool(
        target_located
        and lexical is not None
        and lexical.complete_alignment
        and lexical.committed_target_end
        and lexical.target_end_ms is not None
    )
    facts = _timing_facts(
        lexical=lexical,
        causal_lower_bound_ms=causal_lower_bound_ms,
        input_candidates_ms=input_candidates_ms,
    )

    observed: list[tuple[str, _ActivityRun, int]] = []
    for series in vocal.series if vocal is not None else ():
        observed.extend(
            (series.observer, run, activity_start)
            for run, activity_start in _observed_activity_after(
                series, timing_facts=facts)
        )

    measured_recurrence: VocalRecurrenceEvidence | None = None

    # Every observer keeps its own score scale and recurrence. A repeated
    # structure from one observer can release only that observer's open risk.
    for observer, run, _activity_start in observed:
        if not run.open:
            continue
        recurrence = (
            recurrence_lookup(observer, int(run.start_ms), capture_end)
            if recurrence_lookup is not None else None
        )
        measured_recurrence = recurrence
        if not _stable_recurrence(recurrence):
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end), measured_recurrence

    if not target_complete or lexical is None:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), measured_recurrence

    # A committed known-text end is not by itself physical vocal EOF. Ended
    # requires measured human-vocal activity that overlaps the located text and
    # is subsequently closed by the same observer. Activity observed only after
    # the textual end cannot manufacture that physical-end evidence.
    target_start = int(lexical.target_start_ms)
    target_end = int(lexical.target_end_ms)
    physical_end_observed = any(
        activity_start <= target_end
        and run.last_positive_ms >= target_start
        and run.closed_after(target_end)
        for _observer, run, activity_start in observed
    )
    return UtteranceProgress(
        state=(UtteranceProgressState.ENDED
               if physical_end_observed else UtteranceProgressState.UNKNOWN),
        evidence_at_ms=capture_end), measured_recurrence
