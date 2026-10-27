"""Native-audio evidence and integrated progression assessment.

Measurements retain their own provenance. This module combines already measured
lexical, human-vocal, temporal and recurrence evidence exactly once. It does not
reconstruct acoustic activity from model scores and does not own advancement
policy.
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
class VocalActivityInterval:
    """One observer-local interval of measured human-vocal activity.

    ``inactive_*`` records the contiguous sub-threshold support that established
    disappearance. It is measurement evidence, not a Presentation state.
    """

    start_ms: int
    last_active_ms: int
    inactive_required_ms: int
    inactive_start_ms: int | None = None
    inactive_end_ms: int | None = None

    @property
    def ongoing(self) -> bool:
        return (
            self.inactive_start_ms is None
            or self.inactive_end_ms is None
            or self.inactive_end_ms - self.inactive_start_ms < self.inactive_required_ms
        )

    def inactive_support_after(self, boundary_ms: int) -> bool:
        if self.ongoing or self.inactive_start_ms is None or self.inactive_end_ms is None:
            return False
        return (
            self.inactive_end_ms - max(self.inactive_start_ms, int(boundary_ms))
            >= self.inactive_required_ms
        )


@dataclass(frozen=True)
class VocalEvidenceSeries:
    """One observer's measurements on its own score and time-support scale."""

    observer: str
    active_score_threshold: float = 0.45
    samples: tuple[VocalScoreSample, ...] = ()
    activity: tuple[VocalActivityInterval, ...] = ()


@dataclass(frozen=True)
class HumanVocalEvidence:
    """Human-vocal measurements grouped only by observer provenance."""

    series: tuple[VocalEvidenceSeries, ...] = ()


@dataclass(frozen=True)
class VocalRecurrenceEvidence:
    """Periodic repetition measured from human-vocal observation history."""

    strength: float = 0.0
    period_ms: int | None = None
    prominence: float = 0.0


class UtteranceProgressState(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class LexicalProgressEvidence:
    """Target-conditioned alignment measurement for one PCM query."""

    target_units: int
    observed_units: int
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
            observed_units=alignment.observed_units,
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
    """One integrated audio judgment consumed by Controller.

    ``settled_through_ms`` is a synchronization watermark: when present, the
    audio subsystem has settled every measurement required for an explicit
    assessment fence through that captured PCM boundary.
    """

    state: UtteranceProgressState = UtteranceProgressState.UNKNOWN
    evidence_at_ms: int | None = None
    settled_through_ms: int | None = None

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


def _stable_recurrence(recurrence: VocalRecurrenceEvidence | None) -> bool:
    if recurrence is None or recurrence.period_ms is None:
        return False
    return bool(
        recurrence.strength >= LOOP_REPEATABILITY
        and recurrence.prominence >= LOOP_PERIOD_PROMINENCE
    )


def _representative_recurrence(
    candidates: tuple[VocalRecurrenceEvidence, ...],
) -> VocalRecurrenceEvidence | None:
    if not candidates:
        return None
    stable = tuple(candidate for candidate in candidates if _stable_recurrence(candidate))
    pool = stable or candidates
    return max(pool, key=lambda item: (item.prominence, item.strength))


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


def _presentation_anchors(
    *, lexical: LexicalProgressEvidence | None, causal_lower_bound_ms: int | None,
    input_candidates_ms: tuple[int, ...],
) -> tuple[int, ...]:
    """Return factual times that can relate new vocal activity to this Presentation.

    AutoAdv's own successful input is a hard causal lower bound. External inputs
    remain candidates. Once known text is acoustically located, only a candidate
    not later than that located onset can safely narrow the relation.
    """
    if causal_lower_bound_ms is not None:
        return (int(causal_lower_bound_ms),)

    candidates = tuple(sorted({int(value) for value in input_candidates_ms if int(value) > 0}))
    if _target_located(lexical) and lexical is not None and lexical.target_start_ms is not None:
        target_start = int(lexical.target_start_ms)
        earlier = tuple(value for value in candidates if value <= target_start)
        return (max(earlier) if earlier else target_start,)
    return candidates


def _related_activity(
    vocal: HumanVocalEvidence | None, *, anchors: tuple[int, ...],
) -> tuple[VocalActivityInterval, ...]:
    if vocal is None or not anchors:
        return ()
    return tuple(
        interval
        for series in vocal.series
        for interval in series.activity
        if any(
            interval.start_ms >= anchor and interval.last_active_ms > anchor
            for anchor in anchors
        )
    )


def evaluate_utterance_progress(
    *,
    capture_end_ms: int | None,
    lexical: LexicalProgressEvidence | None,
    vocal: HumanVocalEvidence | None,
    causal_lower_bound_ms: int | None = None,
    input_candidates_ms: tuple[int, ...] = (),
    recurrence_lookup: Callable[[int, int], tuple[VocalRecurrenceEvidence, ...]] | None = None,
) -> tuple[UtteranceProgress, VocalRecurrenceEvidence | None]:
    """Map measured evidence to current finite-vocal risk / end / unknown.

    Acoustic activity segmentation belongs to the measurement layer. This
    function only relates those measurements to the current Presentation and
    never changes Controller policy.
    """
    if capture_end_ms is None:
        return UtteranceProgress(evidence_at_ms=None), None
    capture_end = int(capture_end_ms)
    located = _target_located(lexical)
    complete = _target_complete(lexical)
    anchors = _presentation_anchors(
        lexical=lexical,
        causal_lower_bound_ms=causal_lower_bound_ms,
        input_candidates_ms=input_candidates_ms,
    )
    related = _related_activity(vocal, anchors=anchors)
    ongoing = tuple(interval for interval in related if interval.ongoing)

    measured_recurrence: VocalRecurrenceEvidence | None = None
    if ongoing:
        reading_exceeded = bool(
            lexical is not None
            and lexical.target_units > 0
            and lexical.observed_units > lexical.target_units
        )
        recurrence_needed = bool(not located or complete or reading_exceeded)
        if recurrence_needed and recurrence_lookup is not None:
            recurrence_start = min(anchors) if anchors else min(
                interval.start_ms for interval in ongoing)
            candidates = recurrence_lookup(recurrence_start, capture_end)
            measured_recurrence = _representative_recurrence(candidates)
        if not _stable_recurrence(measured_recurrence):
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end,
            ), measured_recurrence

    if not complete or lexical is None:
        return UtteranceProgress(
            state=UtteranceProgressState.UNKNOWN,
            evidence_at_ms=capture_end,
        ), measured_recurrence

    target_start = int(lexical.target_start_ms)
    target_end = int(lexical.target_end_ms)
    ended = any(
        interval.start_ms <= target_end
        and interval.last_active_ms >= target_start
        and interval.inactive_support_after(target_end)
        for interval in related
    )
    return UtteranceProgress(
        state=(UtteranceProgressState.ENDED if ended else UtteranceProgressState.UNKNOWN),
        evidence_at_ms=capture_end,
    ), measured_recurrence
