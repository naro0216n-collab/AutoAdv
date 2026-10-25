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
class VocalScoreSample:
    """One observer score with the exact input support that produced it."""

    at_ms: int
    probability: float
    support_start_ms: int


@dataclass(frozen=True)
class VocalEvidenceSeries:
    """One observer's human-vocal measurements on its own score scale.

    ``active_intervals`` is the observer's strong-positive view only.  ``samples``
    preserves the continuous scores and receptive-field provenance so the
    snapshot reducer can reason about weaker relative changes without pretending
    that different observers share one probability scale or one time resolution.
    """

    source: str
    frame_count: int
    active_count: int
    peak_probability: float
    active_threshold: float = 0.45
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
RELATIVE_VOCAL_MIN_RISE = 0.05
RELATIVE_VOCAL_MAD_MULTIPLIER = 3.0


def _vocal_chains(
    intervals: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    """Reconstruct finite continuity inside one observer series only."""
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


@dataclass(frozen=True)
class _VocalRiskChain:
    source: str
    support_start_ms: int
    first_observed_ms: int
    last_observed_ms: int


def _selected_risk_samples(
    series: VocalEvidenceSeries, *, reference_before_ms: int | None,
) -> tuple[VocalScoreSample, ...]:
    """Select score rises inside one observer without inventing a shared scale."""
    if not series.samples:
        return ()
    threshold = float(series.active_threshold)
    if reference_before_ms is not None:
        reference = [
            sample.probability for sample in series.samples
            if sample.at_ms < reference_before_ms
        ][-128:]
        if reference:
            ordered = sorted(float(value) for value in reference)
            median = ordered[len(ordered) // 2]
            deviations = sorted(abs(value - median) for value in ordered)
            mad = deviations[len(deviations) // 2] if deviations else 0.0
            relative_threshold = median + max(
                RELATIVE_VOCAL_MIN_RISE, RELATIVE_VOCAL_MAD_MULTIPLIER * mad)
            threshold = min(threshold, relative_threshold)
    return tuple(
        sample for sample in series.samples
        if (reference_before_ms is None or sample.at_ms >= reference_before_ms)
        and sample.probability >= threshold
    )


def _series_risk_chains(
    vocal: HumanVocalEvidence | None, *, reference_before_ms: int | None,
) -> tuple[_VocalRiskChain, ...]:
    if vocal is None:
        return ()
    chains: list[_VocalRiskChain] = []
    for series in vocal.series:
        selected = _selected_risk_samples(
            series, reference_before_ms=reference_before_ms)
        if not selected:
            # Synthetic/domain tests and legacy observations may expose only the
            # strong-positive intervals. Preserve their meaning without
            # fabricating receptive-field provenance.
            chains.extend(
                _VocalRiskChain(series.source, start_ms, start_ms, end_ms)
                for start_ms, end_ms in _vocal_chains(series.active_intervals)
            )
            continue
        all_times = [sample.at_ms for sample in series.samples]
        gaps = [b - a for a, b in zip(all_times, all_times[1:]) if b > a]
        nominal_gap = float(sorted(gaps)[len(gaps) // 2]) if gaps else 16.0
        join_gap = max(32.0, nominal_gap * 2.0, float(END_CONTEXT_MS))
        current: list[VocalScoreSample] = []
        for sample in selected:
            if current and sample.at_ms - current[-1].at_ms >= join_gap:
                chains.append(_VocalRiskChain(
                    source=series.source,
                    support_start_ms=min(item.support_start_ms for item in current),
                    first_observed_ms=current[0].at_ms,
                    last_observed_ms=current[-1].at_ms,
                ))
                current = []
            current.append(sample)
        if current:
            chains.append(_VocalRiskChain(
                source=series.source,
                support_start_ms=min(item.support_start_ms for item in current),
                first_observed_ms=current[0].at_ms,
                last_observed_ms=current[-1].at_ms,
            ))
    return tuple(sorted(
        chains, key=lambda item: (item.first_observed_ms, item.last_observed_ms, item.source)))


def _connected_chain_end(
    chains: tuple[_VocalRiskChain, ...],
    anchor_start: int,
    anchor_end: int,
) -> int | None:
    """Return latest observer risk whose measured support can touch a lexical span."""
    ends = [
        chain.last_observed_ms for chain in chains
        if chain.last_observed_ms >= anchor_start
        and chain.support_start_ms <= anchor_end
    ]
    if ends:
        return max(max(ends), anchor_end)
    later = [
        chain.last_observed_ms for chain in chains
        if 0 <= chain.first_observed_ms - anchor_end < END_CONTEXT_MS
    ]
    return max(later) if later else None


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



def _open_post_boundary_risk(
    vocal: HumanVocalEvidence | None, *, boundary_ms: int, capture_end_ms: int,
) -> tuple[int, int] | None:
    """Find any currently open finite vocal-like change after a causal boundary.

    No candidate is consumed merely because an earlier chain closed.  A later
    measured change is evaluated from the same immutable snapshot, which avoids
    rebuilding the old stateful one-shot event guard.
    """
    candidates = [
        (chain.first_observed_ms, chain.last_observed_ms)
        for chain in _series_risk_chains(vocal, reference_before_ms=boundary_ms)
        # A wide receptive field may cross the boundary, but a score rise is
        # selected only relative to measurements from before that boundary.
        # The rise can therefore protect against a new finite change without
        # pretending the classifier pinpoints its physical onset.
        if chain.first_observed_ms >= boundary_ms
        and capture_end_ms - chain.last_observed_ms < END_CONTEXT_MS
    ]
    return max(candidates, key=lambda item: item[1]) if candidates else None


def evaluate_audio_snapshot(observation: NativeAudioObservation) -> UtteranceProgress:
    """Evaluate one immutable measurement snapshot with no retained inference state."""
    capture_end = observation.capture_end_ms
    if capture_end is None:
        return UtteranceProgress(evidence_at_ms=None)

    lexical = observation.lexical
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
        anchor_end = int(lexical.target_end_ms) if complete else aligned_end
        reference_before = (
            observation.causal_lower_bound_ms
            if observation.causal_lower_bound_ms is not None
            else target_start
        )
        chains = _series_risk_chains(
            observation.vocal, reference_before_ms=reference_before)
        connected_end = _connected_chain_end(chains, target_start, anchor_end)
        physical_risk_end = max(anchor_end, connected_end or anchor_end)
        recurrence_start = int(lexical.target_end_ms) if complete else aligned_end
        recurrent = _stable_recurrence(
            observation.recurrence, start_ms=recurrence_start,
            capture_end_ms=capture_end)

        # Any measured target prefix can locate a cut-risk candidate even before
        # it satisfies the stricter ownership threshold.  This is intentionally
        # not an ownership claim: after the connected risk closes, incomplete
        # lexical evidence returns to UNKNOWN and fallback policy remains in the
        # Controller.
        if not complete:
            if recurrent:
                return UtteranceProgress(
                    state=UtteranceProgressState.UNKNOWN, evidence_at_ms=capture_end)
            if capture_end - physical_risk_end < END_CONTEXT_MS:
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE, evidence_at_ms=capture_end)
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN, evidence_at_ms=capture_end)

        # A committed lexical end establishes target relation, not physical EOF.
        # ENDED also needs an independently observed vocal-risk chain connected
        # to that target.  Merely running a classifier and seeing no positive
        # support is not endpoint evidence; a detector that missed the whole
        # vocal event therefore leaves the Presentation UNKNOWN.
        if connected_end is None:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN, evidence_at_ms=capture_end)

        # Stable repetition can release finite-risk protection but never upgrades
        # itself into target completion.
        if recurrent:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN, evidence_at_ms=capture_end)
        if capture_end - physical_risk_end < END_CONTEXT_MS:
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE, evidence_at_ms=capture_end)

        # Physical endpoint confirmation is the conjunction of an independently
        # established lexical end and the return of the target-connected,
        # observer-relative vocal risk to its measured baseline.  A strong
        # positive disappearing by itself is therefore insufficient: sub-threshold
        # but elevated continuation remains ACTIVE above.
        return UtteranceProgress(
            state=UtteranceProgressState.ENDED,
            ended_at_ms=physical_risk_end,
            evidence_at_ms=capture_end,
        )

    # No lexical ownership is required to protect a new finite human-vocal risk
    # after an observed input lower bound.  Evaluate every currently open chain;
    # do not consume the first chain and ignore a later one.
    boundary = observation.causal_lower_bound_ms
    if boundary is not None:
        candidate = _open_post_boundary_risk(
            observation.vocal, boundary_ms=int(boundary), capture_end_ms=capture_end)
        if candidate is not None:
            start_ms, _end_ms = candidate
            if not _stable_recurrence(
                    observation.recurrence, start_ms=start_ms,
                    capture_end_ms=capture_end):
                return UtteranceProgress(
                    state=UtteranceProgressState.ACTIVE, evidence_at_ms=capture_end)

    return UtteranceProgress(
        state=UtteranceProgressState.UNKNOWN, evidence_at_ms=capture_end)
