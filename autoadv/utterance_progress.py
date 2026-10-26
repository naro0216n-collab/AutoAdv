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


def _ordered_samples(series: VocalEvidenceSeries) -> tuple[VocalScoreSample, ...]:
    """Return one observer's factual measurements in acoustic time order."""
    return tuple(sorted(
        series.samples,
        key=lambda sample: (int(sample.at_ms), int(sample.support_start_ms)),
    ))


def _latest_positive(
    series: VocalEvidenceSeries, *, not_before_ms: int,
) -> VocalScoreSample | None:
    threshold = float(series.active_score_threshold)
    positives = [
        sample for sample in _ordered_samples(series)
        if int(sample.at_ms) >= int(not_before_ms) and float(sample.score) >= threshold
    ]
    return positives[-1] if positives else None


def _observed_closed_after(
    series: VocalEvidenceSeries, *, last_positive: VocalScoreSample,
    not_before_ms: int | None = None,
) -> bool:
    """Whether this observer explicitly measured quiet after the positive support.

    Passage of time and absence of samples are not closure evidence.  A low score
    closes the risk only when the acoustic support that produced it begins after
    both the last positive observation and any additional factual lower bound.
    """
    threshold = float(series.active_score_threshold)
    boundary = int(last_positive.at_ms)
    if not_before_ms is not None:
        boundary = max(boundary, int(not_before_ms))
    return any(
        int(sample.at_ms) > int(last_positive.at_ms)
        and int(sample.support_start_ms) >= boundary
        and float(sample.score) < threshold
        for sample in _ordered_samples(series)
    )


def _open_activity_start(series: VocalEvidenceSeries) -> int | None:
    """Return the observer's currently open threshold-supported activity.

    Activity is split only by an observed sub-threshold support window that no
    longer overlaps the preceding positive observation.  No wall-clock closure
    or inferred silence is introduced here.
    """
    threshold = float(series.active_score_threshold)
    start_ms: int | None = None
    last_positive: VocalScoreSample | None = None
    for sample in _ordered_samples(series):
        if float(sample.score) >= threshold:
            if start_ms is None:
                start_ms = int(sample.at_ms)
            last_positive = sample
            continue
        if (last_positive is not None
                and int(sample.support_start_ms) >= int(last_positive.at_ms)):
            start_ms = None
            last_positive = None
    if start_ms is None or last_positive is None:
        return None
    return start_ms


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
        measured: list[tuple[VocalEvidenceSeries, VocalScoreSample]] = []
        for series in vocal.series if vocal is not None else ():
            last_positive = _latest_positive(series, not_before_ms=target_start)
            if last_positive is not None:
                measured.append((series, last_positive))

        if not measured:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end), None

        if complete:
            target_end = int(lexical.target_end_ms)
            open_risk = any(
                not _observed_closed_after(
                    series, last_positive=last_positive, not_before_ms=target_end)
                for series, last_positive in measured
            )
            if open_risk:
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

        if any(
            not _observed_closed_after(series, last_positive=last_positive)
            for series, last_positive in measured
        ):
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end), None
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    timing_facts = (int(causal_lower_bound_ms),) if causal_lower_bound_ms is not None else tuple(
        sorted({int(value) for value in input_candidates_ms if int(value) > 0}))
    if not timing_facts:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    open_starts: list[int] = []
    for series in vocal.series if vocal is not None else ():
        start_ms = _open_activity_start(series)
        if start_ms is not None and any(fact <= start_ms for fact in timing_facts):
            open_starts.append(start_ms)

    if not open_starts:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    recurrence_start = min(open_starts)
    recurrence = recurrence_for(recurrence_start)
    if _stable_recurrence(recurrence):
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), recurrence
    return UtteranceProgress(
        state=UtteranceProgressState.ACTIVE,
        evidence_at_ms=capture_end), recurrence
