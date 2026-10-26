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

    source: str
    frame_count: int
    active_count: int
    peak_score: float
    active_score_threshold: float = 0.45
    active_intervals: tuple[tuple[int, int], ...] = ()
    samples: tuple[VocalScoreSample, ...] = ()


@dataclass(frozen=True)
class HumanVocalEvidence:
    """Time-local human-vocal support without cross-model score averaging."""

    start_ms: int
    end_ms: int
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

    kind: str
    capture_start_ms: int | None = None
    capture_end_ms: int | None = None
    lexical: LexicalProgressEvidence | None = None
    vocal: HumanVocalEvidence | None = None
    recurrence: VocalRecurrenceEvidence | None = None
    causal_lower_bound_ms: int | None = None
    input_candidates_ms: tuple[int, ...] = ()
    target_attached_ms: int | None = None
    history_truncated: bool = False
    pcm_last_frame_ms: int | None = None
    heartbeat_at_ms: int | None = None


@dataclass(frozen=True)
class UtteranceProgress:
    """One integrated answer consumed by Controller."""

    state: UtteranceProgressState = UtteranceProgressState.UNKNOWN
    ended_at_ms: int | None = None
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


@dataclass(frozen=True)
class _VocalRiskSpan:
    source: str
    support_start_ms: int
    first_observed_ms: int
    last_observed_ms: int
    closure_context_ms: int


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
    """Evidence horizon needed before one observer can call its activity closed.

    The horizon is derived from that observer's actual sampling cadence and input
    support.  It is deliberately not reused for lexical association, PCM
    continuity, recurrence, or any other semantic responsibility.
    """
    cadence, support = _series_timing(series)
    return max(1, int(round(max(cadence * 2.0, support * 0.5))))


def _vocal_risk_spans(vocal: HumanVocalEvidence | None) -> tuple[_VocalRiskSpan, ...]:
    if vocal is None:
        return ()
    spans: list[_VocalRiskSpan] = []
    for series in vocal.series:
        if not series.active_intervals:
            continue
        threshold = float(series.active_score_threshold)
        positives = [sample for sample in series.samples if sample.score >= threshold]
        closure = _series_closure_context_ms(series)
        for first, last in series.active_intervals:
            covered = [sample for sample in positives if int(first) <= sample.at_ms <= int(last)]
            support_start = min(
                (sample.support_start_ms for sample in covered), default=int(first))
            spans.append(_VocalRiskSpan(
                source=series.source,
                support_start_ms=int(support_start),
                first_observed_ms=int(first),
                last_observed_ms=int(last),
                closure_context_ms=closure,
            ))
    return tuple(sorted(
        spans, key=lambda item: (item.first_observed_ms, item.last_observed_ms, item.source)))


def _overall_closure_context_ms(vocal: HumanVocalEvidence | None) -> int:
    if vocal is None or not vocal.series:
        return 0
    return max((_series_closure_context_ms(series) for series in vocal.series), default=0)


def _span_open(span: _VocalRiskSpan, evidence_end_ms: int) -> bool:
    return int(evidence_end_ms) - span.last_observed_ms < span.closure_context_ms


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


def _open_post_boundary_risk(
    vocal: HumanVocalEvidence | None, *, boundary_ms: int, capture_end_ms: int,
) -> tuple[int, int] | None:
    candidates = [
        (span.first_observed_ms, span.last_observed_ms)
        for span in _vocal_risk_spans(vocal)
        if span.first_observed_ms >= int(boundary_ms)
        and _span_open(span, int(capture_end_ms))
    ]
    return max(candidates, key=lambda item: item[1]) if candidates else None


def _open_post_input_risk(
    vocal: HumanVocalEvidence | None, *, candidates: tuple[int, ...],
    capture_end_ms: int,
) -> tuple[int, int] | None:
    stamps = tuple(sorted({int(value) for value in candidates if int(value) > 0}))
    if not stamps:
        return None
    risks = [
        (span.first_observed_ms, span.last_observed_ms)
        for span in _vocal_risk_spans(vocal)
        if _span_open(span, int(capture_end_ms))
        and any(stamp <= span.first_observed_ms for stamp in stamps)
    ]
    return max(risks, key=lambda item: item[1]) if risks else None


def evaluate_utterance_progress(
    *,
    capture_end_ms: int | None,
    lexical: LexicalProgressEvidence | None,
    vocal: HumanVocalEvidence | None,
    causal_lower_bound_ms: int | None = None,
    input_candidates_ms: tuple[int, ...] = (),
    recurrence_lookup: Callable[[int, int], VocalRecurrenceEvidence | None] | None = None,
) -> tuple[UtteranceProgress, VocalRecurrenceEvidence | None]:
    """Produce one stateless progression result from the available evidence."""
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
        aligned_end = int(lexical.aligned_end_ms)
        complete = bool(
            lexical.target_associated
            and lexical.complete_alignment
            and lexical.committed_target_end
            and lexical.target_end_ms is not None
        )
        spans = tuple(
            span for span in _vocal_risk_spans(vocal)
            if span.last_observed_ms >= target_start
        )
        closure_context = _overall_closure_context_ms(vocal)

        if not complete:
            # A located lexical prefix plus currently open human-vocal evidence
            # is direct cut-risk.  Once both measurements are old, the result is
            # unresolved rather than a permanent ACTIVE latch.
            if any(_span_open(span, capture_end) for span in spans):
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE,
                    evidence_at_ms=capture_end), None
            if closure_context and capture_end - aligned_end < closure_context:
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE,
                    evidence_at_ms=capture_end), None
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end), None

        target_end = int(lexical.target_end_ms)
        # Lexical completion locates the text but is not physical EOF.  A
        # physical completion requires independent human-vocal support in the
        # located Presentation range.
        if not spans:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end), None

        physical_risk_end = max(target_end, max(span.last_observed_ms for span in spans))
        open_risk = any(_span_open(span, capture_end) for span in spans)
        endpoint_context_open = bool(
            closure_context and capture_end - physical_risk_end < closure_context)
        if open_risk or endpoint_context_open:
            recurrence = recurrence_if_needed(target_end)
            if _stable_recurrence(
                recurrence, start_ms=target_end, capture_end_ms=capture_end,
            ):
                # Repetition can release finite-risk protection, but it does not
                # manufacture a physical endpoint for this target.
                return UtteranceProgress(
                    state=UtteranceProgressState.UNKNOWN,
                    evidence_at_ms=capture_end), recurrence
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end), recurrence

        return UtteranceProgress(
            state=UtteranceProgressState.ENDED,
            ended_at_ms=physical_risk_end,
            evidence_at_ms=capture_end), None

    # Without a lexical location, only a finite human-vocal change following a
    # real timing fact can suppress fallback.  It never manufactures ENDED.
    if causal_lower_bound_ms is not None:
        candidate = _open_post_boundary_risk(
            vocal, boundary_ms=int(causal_lower_bound_ms), capture_end_ms=capture_end)
    else:
        candidate = _open_post_input_risk(
            vocal, candidates=input_candidates_ms, capture_end_ms=capture_end)
    if candidate is None:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), None

    start_ms, _end_ms = candidate
    recurrence = recurrence_if_needed(start_ms)
    if _stable_recurrence(recurrence, start_ms=start_ms, capture_end_ms=capture_end):
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end), recurrence
    return UtteranceProgress(
        state=UtteranceProgressState.ACTIVE,
        evidence_at_ms=capture_end), recurrence
