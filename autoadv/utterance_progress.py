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


def _vocal_chains(
    intervals: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Reconstruct finite vocal continuity from one immutable snapshot.

    Observer-positive intervals are measurements, not event identities.  A short
    gap can be an internal pause or a detector miss, so the reducer joins only
    gaps smaller than the endpoint-confirmation context.  Larger gaps remain
    separate finite candidates.  Re-running this function on the same snapshot
    therefore reproduces the old event-lifecycle meaning without retaining any
    inferred event state.
    """
    ordered = sorted(intervals)
    if not ordered:
        return ()
    chains: list[tuple[int, int]] = [ordered[0]]
    for start_ms, end_ms in ordered[1:]:
        previous_start, previous_end = chains[-1]
        if start_ms - previous_end < END_CONTEXT_MS:
            chains[-1] = (previous_start, max(previous_end, end_ms))
        else:
            chains.append((start_ms, end_ms))
    return tuple(chains)


def _connected_chain(
    intervals: tuple[tuple[int, int], ...],
    anchor_start: int,
    anchor_end: int,
) -> tuple[int, int] | None:
    """Return the finite vocal chain connected to a measured target span."""
    chains = _vocal_chains(intervals)
    anchor_index = next((
        index for index, (start_ms, end_ms) in enumerate(chains)
        if end_ms >= anchor_start and start_ms <= anchor_end
    ), None)
    if anchor_index is not None:
        start_ms, end_ms = chains[anchor_index]
        return min(start_ms, anchor_start), max(end_ms, anchor_end)

    # The lexical model itself establishes a vocal span. A human-vocal observer
    # may miss that span, but a later positive chain can still establish a
    # connected physical tail when no endpoint-confirming gap separates them.
    later = next((
        (start_ms, end_ms) for start_ms, end_ms in chains
        if 0 <= start_ms - anchor_end < END_CONTEXT_MS
    ), None)
    if later is None:
        return None
    return anchor_start, later[1]


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


def _first_post_boundary_chain(
    intervals: tuple[tuple[int, int], ...], boundary_ms: int,
) -> tuple[int, int] | None:
    """Return the one causal-only finite candidate for this Presentation.

    A chain already active across the observed input lower bound is inherited
    acoustic context and cannot establish a new finite risk.  With no lexical
    ownership evidence, only the first separated post-boundary chain is allowed
    to protect fallback.  Once that chain is visibly closed, later unrelated
    human-vocal activity cannot be repeatedly promoted merely because it is the
    newest sound.  This is the stateless equivalent of the old one-shot causal
    candidate guard.
    """
    for start_ms, end_ms in _vocal_chains(intervals):
        if start_ms < boundary_ms <= end_ms:
            continue
        if start_ms >= boundary_ms:
            return start_ms, end_ms
    return None


def _vocal_measurement_present(vocal: HumanVocalEvidence | None) -> bool:
    return bool(vocal is not None and any(item.frame_count > 0 for item in vocal.series))


def evaluate_audio_snapshot(observation: NativeAudioObservation) -> UtteranceProgress:
    """Evaluate one immutable measurement snapshot with no retained inference state."""
    capture_end = observation.capture_end_ms
    if capture_end is None:
        return UtteranceProgress(evidence_at_ms=None)

    vocal_intervals = observation.vocal.active_intervals if observation.vocal else ()
    lexical = observation.lexical

    # Known-text relation is the strongest ownership evidence.  Association says
    # that some target prefix was acoustically supported; it does *not* say that
    # the finite vocal event is still open.  The target-connected vocal chain (or
    # the measured lexical span itself when the vocal observer misses it) owns the
    # cut-risk decision at the current snapshot frontier.
    if lexical is not None and lexical.target_associated:
        target_start = lexical.target_start_ms
        aligned_end = lexical.aligned_end_ms
        target_end = lexical.target_end_ms
        anchor_end = target_end if (
            lexical.complete_alignment and lexical.committed_target_end
            and target_end is not None
        ) else aligned_end
        anchor_start = target_start if target_start is not None else anchor_end

        if anchor_start is None or anchor_end is None:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end,
            )

        chain = _connected_chain(vocal_intervals, anchor_start, anchor_end)
        physical_end = chain[1] if chain is not None else anchor_end
        recurrence_start = target_end if target_end is not None else anchor_end
        recurrent = _stable_recurrence(
            observation.recurrence,
            start_ms=recurrence_start,
            capture_end_ms=capture_end,
        )
        complete = bool(
            lexical.complete_alignment
            and lexical.committed_target_end
            and target_end is not None
        )

        if not complete:
            # A partial target relation protects only while its finite vocal chain
            # is observably still open.  Once endpoint-confirming context exists,
            # ownership of the *whole* utterance is unresolved and Controller may
            # apply its explicit uncertainty policy.  Stable repetition likewise
            # removes indefinite protection but never manufactures ENDED.
            if recurrent:
                return UtteranceProgress(
                    state=UtteranceProgressState.UNKNOWN,
                    evidence_at_ms=capture_end,
                )
            if capture_end - physical_end < END_CONTEXT_MS:
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE,
                    evidence_at_ms=capture_end,
                )
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end,
            )

        # A committed lexical end gives ownership but not a physical voice EOF.
        # Stable recurrent tail after that independently established target end can
        # stop extending the finite event; otherwise connected human-vocal support
        # remains protected through short internal gaps.
        if recurrent:
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
        if not _vocal_measurement_present(observation.vocal):
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end,
            )
        return UtteranceProgress(
            state=UtteranceProgressState.ENDED,
            ended_at_ms=max(target_end, physical_end),
            evidence_at_ms=capture_end,
        )

    # Without lexical ownership, an observed input lower bound can still expose
    # one new finite human-vocal risk.  Its disappearance/repetition only removes
    # fallback suppression; it cannot prove that the current text itself ended.
    boundary = observation.causal_lower_bound_ms
    if boundary is not None:
        candidate = _first_post_boundary_chain(vocal_intervals, boundary)
        if candidate is not None:
            start_ms, end_ms = candidate
            if not _stable_recurrence(
                observation.recurrence,
                start_ms=start_ms,
                capture_end_ms=capture_end,
            ) and capture_end - end_ms < END_CONTEXT_MS:
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE,
                    evidence_at_ms=capture_end,
                )

    return UtteranceProgress(
        state=UtteranceProgressState.UNKNOWN,
        evidence_at_ms=capture_end,
    )
