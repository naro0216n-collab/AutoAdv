"""Native-audio evidence and integrated progression assessment.

Measurements retain their own provenance.  This module combines lexical,
human-vocal, temporal and optional recurrence evidence exactly once; it does not
reconstruct source identity or reinterpret transport facts as endpoints.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from statistics import median
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
MIN_LOOP_OBSERVATION_MS = 1200


def _series_timing(series: VocalEvidenceSeries) -> tuple[float, float]:
    """Return observer-local cadence and receptive support width."""
    if not series.samples:
        return 16.0, 16.0
    times = [sample.at_ms for sample in series.samples]
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    cadence = float(median(gaps)) if gaps else 16.0
    widths = [max(0, sample.at_ms - sample.support_start_ms) for sample in series.samples]
    support = float(median(widths)) if widths else cadence
    return max(1.0, cadence), max(1.0, support)


def _series_closure_context_ms(series: VocalEvidenceSeries) -> int:
    """Observer-local evidence horizon needed after its last positive score."""
    cadence, support = _series_timing(series)
    return max(1, int(round(max(cadence * 2.0, support * 0.5))))


def _latest_positive_ms(
    series: VocalEvidenceSeries, *, not_before_ms: int,
) -> int | None:
    threshold = float(series.active_score_threshold)
    times = [
        int(sample.at_ms)
        for sample in series.samples
        if int(sample.at_ms) >= int(not_before_ms) and float(sample.score) >= threshold
    ]
    return max(times) if times else None


def _has_recent_positive(
    series: VocalEvidenceSeries, *, last_positive_ms: int,
    evidence_end_ms: int, not_before_ms: int | None = None,
) -> bool:
    reference = int(last_positive_ms)
    if not_before_ms is not None:
        reference = max(reference, int(not_before_ms))
    return int(evidence_end_ms) - reference < _series_closure_context_ms(series)


def _current_positive_run_after(
    series: VocalEvidenceSeries, *, timing_ms: tuple[int, ...], evidence_end_ms: int,
) -> tuple[int, int] | None:
    """Return current threshold-positive run only when it began after a timing fact."""
    facts = tuple(sorted({int(value) for value in timing_ms if int(value) > 0}))
    if not facts:
        return None
    threshold = float(series.active_score_threshold)
    positives = sorted(
        int(sample.at_ms) for sample in series.samples if float(sample.score) >= threshold)
    if not positives:
        return None
    closure = _series_closure_context_ms(series)
    first = last = positives[0]
    for at_ms in positives[1:]:
        if at_ms - last >= closure:
            first = at_ms
        last = at_ms
    if not any(fact <= first for fact in facts):
        return None
    if int(evidence_end_ms) - last >= closure:
        return None
    return first, last


def _stable_recurrence(
    recurrence: VocalRecurrenceEvidence | None,
    *,
    start_ms: int | None,
    capture_end_ms: int,
) -> bool:
    if recurrence is None or start_ms is None or recurrence.period_ms is None:
        return False
    observed_ms = capture_end_ms - start_ms
    return bool(
        recurrence.strength >= LOOP_STRENGTH
        and recurrence.period_stability >= LOOP_STABILITY
        and recurrence.recent_strength >= RECENT_LOOP_STRENGTH
        and recurrence.level_consistency >= LOOP_LEVEL_CONSISTENCY
        and observed_ms >= max(
            MIN_LOOP_OBSERVATION_MS,
            recurrence.period_ms * 3,
        )
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

    def recurrence_if_needed(start_ms: int) -> VocalRecurrenceEvidence | None:
        if recurrence_lookup is None or capture_end - int(start_ms) < MIN_LOOP_OBSERVATION_MS:
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
        measured: list[tuple[VocalEvidenceSeries, int]] = []
        for series in vocal.series if vocal is not None else ():
            last_positive = _latest_positive_ms(series, not_before_ms=target_start)
            if last_positive is not None:
                measured.append((series, last_positive))

        if not complete:
            if any(
                _has_recent_positive(
                    series, last_positive_ms=last, evidence_end_ms=capture_end)
                for series, last in measured
            ):
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE,
                    evidence_at_ms=capture_end), None
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end), None

        target_end = int(lexical.target_end_ms)
        if not measured:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end), None

        still_open = any(
            _has_recent_positive(
                series, last_positive_ms=last, evidence_end_ms=capture_end,
                not_before_ms=target_end)
            for series, last in measured
        )
        if still_open:
            recurrence = recurrence_if_needed(target_end)
            if _stable_recurrence(
                recurrence, start_ms=target_end, capture_end_ms=capture_end,
            ):
                return UtteranceProgress(
                    state=UtteranceProgressState.UNKNOWN,
                    evidence_at_ms=capture_end), recurrence
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end), recurrence

        return UtteranceProgress(
            state=UtteranceProgressState.ENDED,
            evidence_at_ms=capture_end), None

    timing_facts = (int(causal_lower_bound_ms),) if causal_lower_bound_ms is not None else tuple(
        sorted({int(value) for value in input_candidates_ms if int(value) > 0}))
    if not timing_facts:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    open_measurements: list[tuple[VocalEvidenceSeries, int, int]] = []
    for series in vocal.series if vocal is not None else ():
        current = _current_positive_run_after(
            series, timing_ms=timing_facts, evidence_end_ms=capture_end)
        if current is not None:
            open_measurements.append((series, current[0], current[1]))

    if not open_measurements:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    recurrence_start = min(first for _series, first, _last in open_measurements)
    recurrence = recurrence_if_needed(recurrence_start)
    if _stable_recurrence(recurrence, start_ms=recurrence_start, capture_end_ms=capture_end):
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), recurrence
    return UtteranceProgress(
        state=UtteranceProgressState.ACTIVE,
        evidence_at_ms=capture_end), recurrence
