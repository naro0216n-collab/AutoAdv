"""Snapshot-based native-audio assessment for one presentation.

Observers and alignment backends publish measurements tied to exact audio/time
ranges.  This module combines one immutable snapshot into the only three product
results needed by Controller.  It deliberately keeps no event identity or
inference history: evaluating the same snapshot twice produces the same result.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .forced_alignment import TargetTimeAlignment


@dataclass(frozen=True)
class VocalEvidenceSeries:
    """One observer's human-vocal measurements on its own score scale."""

    source: str
    frame_count: int
    active_count: int
    peak_probability: float
    active_intervals: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class HumanVocalEvidence:
    """Time-local human-vocal support without cross-model score averaging."""

    start_ms: int
    end_ms: int
    series: tuple[VocalEvidenceSeries, ...] = ()

    @property
    def active_intervals(self) -> tuple[tuple[int, int], ...]:
        intervals = sorted(
            interval for item in self.series for interval in item.active_intervals)
        if not intervals:
            return ()
        merged: list[tuple[int, int]] = [intervals[0]]
        for start_ms, end_ms in intervals[1:]:
            if start_ms <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end_ms))
            else:
                merged.append((start_ms, end_ms))
        return tuple(merged)

    @property
    def first_active_ms(self) -> int | None:
        intervals = self.active_intervals
        return intervals[0][0] if intervals else None

    @property
    def last_active_ms(self) -> int | None:
        intervals = self.active_intervals
        return intervals[-1][1] if intervals else None


@dataclass(frozen=True)
class VocalRecurrenceEvidence:
    """Independent measurements of repeated acoustic structure.

    Values intentionally remain separate.  ``strength`` is spectral recurrence
    among time points supported by a human-vocal observer.  ``level_consistency``
    is amplitude consistency for those pairs.  ``low_vocal_reference_similarity``
    measures how well the same structure is already explained by nearby audio
    without positive human-vocal support; it is not a background/source label.
    """

    strength: float = 0.0
    period_ms: int | None = None
    period_stability: float = 0.0
    recent_strength: float = 0.0
    level_consistency: float = 0.0
    low_vocal_reference_similarity: float = 0.0


class UtteranceProgressState(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class LexicalProgressEvidence:
    """Target-conditioned alignment measurement for one PCM snapshot."""

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
    """Measurements for one explicit audio snapshot/query.

    ``causal_lower_bound_ms`` is an observed game-input lower bound when one is
    available. ``target_attached_ms`` is retained only as factual telemetry for
    when the known text became available; it is never an acoustic boundary.
    """

    kind: str
    capture_start_ms: int | None = None
    capture_end_ms: int | None = None
    lexical: LexicalProgressEvidence | None = None
    vocal: HumanVocalEvidence | None = None
    recurrence: VocalRecurrenceEvidence | None = None
    causal_lower_bound_ms: int | None = None
    target_attached_ms: int | None = None
    history_truncated: bool = False
    pcm_last_frame_ms: int | None = None
    heartbeat_at_ms: int | None = None


@dataclass(frozen=True)
class UtteranceProgress:
    """One stateless integrated answer consumed by Controller."""

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


END_CONTEXT_MS = 360
LOOP_STRENGTH = 0.78
LOOP_STABILITY = 0.60
RECENT_LOOP_STRENGTH = 0.72
LOOP_LEVEL_CONSISTENCY = 0.55
MIN_LOOP_OBSERVATION_MS = 1200
LOOP_REFERENCE_MARGIN = 0.06


def _connected_chain(
    intervals: tuple[tuple[int, int], ...],
    anchor_start: int,
    anchor_end: int,
) -> tuple[int, int] | None:
    """Return vocal support connected in time to a measured target span."""
    ordered = sorted(intervals)
    anchor_index = next((
        index for index, (start_ms, end_ms) in enumerate(ordered)
        if end_ms >= anchor_start and start_ms <= anchor_end
    ), None)
    if anchor_index is None:
        # The lexical model itself establishes a vocal span.  A vocal observer
        # may miss it; only a nearby *later* observation can extend the physical
        # tail beyond that measured lexical span.
        later = next((
            (start_ms, end_ms) for start_ms, end_ms in ordered
            if 0 <= start_ms - anchor_end < END_CONTEXT_MS
        ), None)
        if later is None:
            return None
        start_ms, end_ms = anchor_start, later[1]
        anchor_index = ordered.index(later)
    else:
        start_ms, end_ms = ordered[anchor_index]
        start_ms = min(start_ms, anchor_start)
        end_ms = max(end_ms, anchor_end)

    for next_start, next_end in ordered[anchor_index + 1:]:
        if next_start - end_ms >= END_CONTEXT_MS:
            break
        end_ms = max(end_ms, next_end)
    return start_ms, end_ms


def _stable_recurrence(
    recurrence: VocalRecurrenceEvidence | None,
    *,
    start_ms: int | None,
    capture_end_ms: int,
) -> bool:
    if recurrence is None or start_ms is None or recurrence.period_ms is None:
        return False
    observed_ms = capture_end_ms - start_ms
    reference_distinct = (
        recurrence.low_vocal_reference_similarity <= 0.0
        or recurrence.strength >= (
            recurrence.low_vocal_reference_similarity + LOOP_REFERENCE_MARGIN)
    )
    return bool(
        recurrence.strength >= LOOP_STRENGTH
        and recurrence.period_stability >= LOOP_STABILITY
        and recurrence.recent_strength >= RECENT_LOOP_STRENGTH
        and recurrence.level_consistency >= LOOP_LEVEL_CONSISTENCY
        and reference_distinct
        and observed_ms >= max(MIN_LOOP_OBSERVATION_MS, recurrence.period_ms * 2)
    )


def _new_post_boundary_intervals(
    intervals: tuple[tuple[int, int], ...], boundary_ms: int,
) -> tuple[tuple[int, int], ...]:
    """Return activity that observably begins after the causal lower bound.

    Activity already present across the lower bound is not re-labelled as an
    old/background source.  It simply is not evidence that a new finite vocal
    risk began in this Presentation.  A separated later interval remains new
    evidence because the intervening lack of positive support is observable.
    """
    return tuple(
        (start_ms, end_ms)
        for start_ms, end_ms in intervals
        if start_ms >= boundary_ms
    )


def evaluate_audio_snapshot(observation: NativeAudioObservation) -> UtteranceProgress:
    """Evaluate one immutable measurement snapshot with no retained inference state."""
    capture_end = observation.capture_end_ms
    if capture_end is None:
        return UtteranceProgress(evidence_at_ms=None)

    vocal_intervals = observation.vocal.active_intervals if observation.vocal else ()
    lexical = observation.lexical

    # Known-text relation is the strongest direct evidence available.  An
    # associated prefix that has not reached a committed target end is still a
    # finite target utterance in progress.
    if lexical is not None and lexical.target_associated:
        target_start = lexical.target_start_ms
        aligned_end = lexical.aligned_end_ms
        target_end = lexical.target_end_ms
        if not (lexical.complete_alignment and lexical.committed_target_end and target_end is not None):
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end,
            )

        anchor_start = target_start if target_start is not None else target_end
        anchor_end = max(
            value for value in (aligned_end, target_end)
            if value is not None
        )
        chain = _connected_chain(vocal_intervals, anchor_start, anchor_end)
        physical_end = chain[1] if chain is not None else anchor_end
        vocal_measurement_present = bool(
            observation.vocal is not None
            and any(item.frame_count > 0 for item in observation.vocal.series)
        )

        if _stable_recurrence(
            observation.recurrence,
            start_ms=target_end,
            capture_end_ms=capture_end,
        ):
            return UtteranceProgress(
                state=UtteranceProgressState.ENDED,
                ended_at_ms=target_end,
                evidence_at_ms=capture_end,
            )

        if capture_end - physical_end < END_CONTEXT_MS:
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end,
            )
        if not vocal_measurement_present:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end,
            )

        return UtteranceProgress(
            state=UtteranceProgressState.ENDED,
            ended_at_ms=physical_end,
            evidence_at_ms=capture_end,
        )

    # Without a lexical relation, a known input lower bound can still protect a
    # non-lexical finite vocal change from fallback.  This is deliberately a
    # risk judgment, not source ownership, so its disappearance/repetition
    # returns to UNKNOWN rather than manufacturing ENDED.
    boundary = observation.causal_lower_bound_ms
    if boundary is not None:
        new_intervals = _new_post_boundary_intervals(vocal_intervals, boundary)
        if new_intervals:
            first_new = new_intervals[0][0]
            if not _stable_recurrence(
                observation.recurrence,
                start_ms=first_new,
                capture_end_ms=capture_end,
            ):
                latest_end = new_intervals[-1][1]
                if capture_end - latest_end < END_CONTEXT_MS:
                    return UtteranceProgress(
                        state=UtteranceProgressState.ACTIVE,
                        evidence_at_ms=capture_end,
                    )

    return UtteranceProgress(
        state=UtteranceProgressState.UNKNOWN,
        evidence_at_ms=capture_end,
    )
