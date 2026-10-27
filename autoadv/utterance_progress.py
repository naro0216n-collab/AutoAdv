"""Integrated native-audio inference for the current Presentation.

Measurement layers preserve lexical, human-vocal, temporal and recurrence facts.
This module relates those facts to the current Presentation exactly once and
returns only continuing finite-vocal risk, confirmed end, or uncertainty.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .audio_evidence import (
    HumanVocalEvidence, LexicalProgressEvidence, VocalActivityInterval,
    VocalRecurrenceEvidence,
)


class UtteranceProgressState(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class UtteranceProgress:
    """One integrated audio judgment consumed by Controller."""

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


LOOP_REPEATABILITY = 0.60
LOOP_PERIOD_PROMINENCE = 0.10


def _stable_recurrence(recurrence: VocalRecurrenceEvidence) -> bool:
    return bool(
        recurrence.period_ms is not None
        and recurrence.strength >= LOOP_REPEATABILITY
        and recurrence.prominence >= LOOP_PERIOD_PROMINENCE
    )


def _target_located(lexical: LexicalProgressEvidence | None) -> bool:
    return bool(
        lexical is not None
        and lexical.target_associated
        and lexical.aligned_target_units > 0
        and lexical.target_start_ms is not None
        and lexical.aligned_end_ms is not None
    )


def _target_complete(lexical: LexicalProgressEvidence | None) -> bool:
    return bool(
        _target_located(lexical)
        and lexical is not None
        and lexical.complete_alignment
        and lexical.committed_target_end
        and lexical.target_end_ms is not None
    )


def _target_overlap(
    interval: VocalActivityInterval, lexical: LexicalProgressEvidence | None,
) -> bool:
    """Whether measured activity overlaps the acoustically located known text."""
    if not _target_located(lexical) or lexical is None:
        return False
    return bool(
        interval.start_ms <= int(lexical.aligned_end_ms)
        and interval.last_active_ms >= int(lexical.target_start_ms)
    )


def _starts_after_presentation_fact(
    interval: VocalActivityInterval, *, lexical: LexicalProgressEvidence | None,
    causal_lower_bound_ms: int | None, input_candidates_ms: tuple[int, ...],
) -> bool:
    """Relate an onset without collapsing distinct temporal facts into one anchor.

    AutoAdv's own successful input is a hard lower bound. External inputs remain
    independent candidates. Once known text is located, an external candidate is
    useful for narrowing only when it is not later than the measured target onset;
    the measured target onset itself can relate later non-lexical activity.
    """
    if causal_lower_bound_ms is not None:
        boundary = int(causal_lower_bound_ms)
        return interval.start_ms >= boundary and interval.last_active_ms > boundary

    candidates = tuple(sorted({
        int(value) for value in input_candidates_ms if int(value) > 0
    }))
    if _target_located(lexical) and lexical is not None:
        target_start = int(lexical.target_start_ms)
        if interval.start_ms >= target_start and interval.last_active_ms > target_start:
            return True
        return any(
            value <= target_start
            and interval.start_ms >= value
            and interval.last_active_ms > value
            for value in candidates
        )

    return any(
        interval.start_ms >= value and interval.last_active_ms > value
        for value in candidates
    )


def _related_activity(
    vocal: HumanVocalEvidence | None, *, lexical: LexicalProgressEvidence | None,
    causal_lower_bound_ms: int | None, input_candidates_ms: tuple[int, ...],
) -> tuple[VocalActivityInterval, ...]:
    if vocal is None:
        return ()
    intervals = tuple(
        interval for series in vocal.series for interval in series.activity
    )
    if causal_lower_bound_ms is not None:
        return tuple(
            interval for interval in intervals
            if _starts_after_presentation_fact(
                interval, lexical=lexical, causal_lower_bound_ms=causal_lower_bound_ms,
                input_candidates_ms=input_candidates_ms)
        )
    return tuple(
        interval for interval in intervals
        if _target_overlap(interval, lexical)
        or _starts_after_presentation_fact(
            interval, lexical=lexical, causal_lower_bound_ms=None,
            input_candidates_ms=input_candidates_ms)
    )


def _recurrence_query_start(
    ongoing: tuple[VocalActivityInterval, ...], *, lexical: LexicalProgressEvidence | None,
    causal_lower_bound_ms: int | None, input_candidates_ms: tuple[int, ...],
) -> int:
    """Choose only the measurement range needed for optional recurrence analysis."""
    if causal_lower_bound_ms is not None:
        return int(causal_lower_bound_ms)
    if _target_located(lexical) and lexical is not None:
        target_start = int(lexical.target_start_ms)
        earlier = tuple(
            int(value) for value in input_candidates_ms
            if int(value) > 0 and int(value) <= target_start
        )
        return max(earlier) if earlier else target_start
    candidates = tuple(int(value) for value in input_candidates_ms if int(value) > 0)
    related_candidates = tuple(
        value for value in candidates
        if any(interval.start_ms >= value and interval.last_active_ms > value for interval in ongoing)
    )
    return min(related_candidates) if related_candidates else min(
        interval.start_ms for interval in ongoing)


def evaluate_utterance_progress(
    *,
    capture_end_ms: int | None,
    lexical: LexicalProgressEvidence | None,
    vocal: HumanVocalEvidence | None,
    causal_lower_bound_ms: int | None = None,
    input_candidates_ms: tuple[int, ...] = (),
    recurrence_lookup: Callable[[int, int], tuple[VocalRecurrenceEvidence, ...]] | None = None,
) -> tuple[UtteranceProgress, tuple[VocalRecurrenceEvidence, ...]]:
    """Map measured evidence to current finite-vocal risk / end / unknown."""
    if capture_end_ms is None:
        return UtteranceProgress(evidence_at_ms=None), ()
    capture_end = int(capture_end_ms)
    complete = _target_complete(lexical)
    related = _related_activity(
        vocal, lexical=lexical, causal_lower_bound_ms=causal_lower_bound_ms,
        input_candidates_ms=input_candidates_ms)
    ongoing = tuple(interval for interval in related if interval.ongoing)

    recurrence: tuple[VocalRecurrenceEvidence, ...] = ()
    if ongoing:
        reading_exceeded = bool(
            lexical is not None
            and lexical.target_units > 0
            and lexical.observed_units > lexical.target_units
        )
        recurrence_needed = bool(not _target_located(lexical) or complete or reading_exceeded)
        if recurrence_needed and recurrence_lookup is not None:
            recurrence = recurrence_lookup(
                _recurrence_query_start(
                    ongoing, lexical=lexical, causal_lower_bound_ms=causal_lower_bound_ms,
                    input_candidates_ms=input_candidates_ms),
                capture_end,
            )
        if not any(_stable_recurrence(candidate) for candidate in recurrence):
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE, evidence_at_ms=capture_end
            ), recurrence

    if not complete or lexical is None:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN, evidence_at_ms=capture_end
        ), recurrence

    target_start = int(lexical.target_start_ms)
    target_end = int(lexical.target_end_ms)
    ended = any(
        _target_overlap(interval, lexical)
        and interval.start_ms <= target_end
        and interval.last_active_ms >= target_start
        and interval.inactive_support_after(target_end)
        for series in (vocal.series if vocal is not None else ())
        for interval in series.activity
    )
    return UtteranceProgress(
        state=(UtteranceProgressState.ENDED if ended else UtteranceProgressState.UNKNOWN),
        evidence_at_ms=capture_end,
    ), recurrence
