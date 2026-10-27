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

# A threshold crossing is a measurement, not an endpoint.  Releasing an already
# observed finite-vocal risk requires a contiguous span of explicitly measured
# sub-threshold support.  The floor prevents a single narrow VAD frame from
# splitting breaths, cries and ordinary within-utterance pauses; observers with
# wider receptive fields naturally require at least one full support window.
MIN_QUIET_SUPPORT_MS = 240

# Partial target alignment is direct evidence that the known finite utterance
# has begun but has not yet reached its target end.  When vocal support briefly
# drops, keep analyzing real PCM long enough for the existing incremental
# aligner to show whether target coverage resumes.  This is an analysis budget,
# not an estimated utterance duration or a completion timer.
MIN_PARTIAL_ANALYSIS_MS = 500
MAX_PARTIAL_ANALYSIS_MS = 2_000


@dataclass(frozen=True)
class _ActivityRun:
    start_ms: int
    support_start_ms: int
    last_positive: VocalScoreSample
    closed_at_ms: int | None = None

    @property
    def open(self) -> bool:
        return self.closed_at_ms is None


def _ordered_samples(series: VocalEvidenceSeries) -> tuple[VocalScoreSample, ...]:
    """Return one observer's factual measurements in acoustic time order."""
    return tuple(sorted(
        series.samples,
        key=lambda sample: (int(sample.at_ms), int(sample.support_start_ms)),
    ))


def _support_width_ms(sample: VocalScoreSample) -> int:
    return max(0, int(sample.at_ms) - int(sample.support_start_ms))


def _required_quiet_support_ms(samples: tuple[VocalScoreSample, ...]) -> int:
    return max(MIN_QUIET_SUPPORT_MS, max(
        (_support_width_ms(sample) for sample in samples), default=0))


def _quiet_support_end(
    series: VocalEvidenceSeries, *, after_ms: int, through_ms: int | None = None,
) -> int | None:
    """Return where enough contiguous measured low support closes a run.

    Only sub-threshold input support entirely after ``after_ms`` counts.  Gaps
    between measured supports are not silently filled, and wall-clock distance
    from the last positive is irrelevant.
    """
    threshold = float(series.active_score_threshold)
    samples = _ordered_samples(series)
    required = _required_quiet_support_ms(samples)
    quiet_start: int | None = None
    quiet_end: int | None = None
    for sample in samples:
        at_ms = int(sample.at_ms)
        if at_ms <= int(after_ms):
            continue
        if through_ms is not None and at_ms > int(through_ms):
            break
        if float(sample.score) >= threshold:
            quiet_start = quiet_end = None
            continue
        support_start = int(sample.support_start_ms)
        if support_start < int(after_ms) or at_ms <= support_start:
            continue
        if quiet_start is None or quiet_end is None or support_start > quiet_end:
            quiet_start, quiet_end = support_start, at_ms
        else:
            quiet_end = max(quiet_end, at_ms)
        if quiet_end - quiet_start >= required:
            return quiet_end
    return None


def _activity_runs(series: VocalEvidenceSeries) -> tuple[_ActivityRun, ...]:
    """Build observer-local activity runs from measured support intervals.

    A run starts on a positive observer measurement.  It survives threshold
    valleys until the same observer has measured enough contiguous low support
    to close it.  A later positive after closure starts a genuinely new run.
    """
    threshold = float(series.active_score_threshold)
    samples = _ordered_samples(series)
    required = _required_quiet_support_ms(samples)
    runs: list[_ActivityRun] = []
    start_ms: int | None = None
    support_start_ms: int | None = None
    last_positive: VocalScoreSample | None = None
    quiet_start: int | None = None
    quiet_end: int | None = None

    for sample in samples:
        at_ms = int(sample.at_ms)
        if float(sample.score) >= threshold:
            if start_ms is None:
                start_ms = at_ms
                support_start_ms = int(sample.support_start_ms)
            last_positive = sample
            quiet_start = quiet_end = None
            continue
        if start_ms is None or last_positive is None or support_start_ms is None:
            continue
        boundary = int(last_positive.at_ms)
        sample_start = int(sample.support_start_ms)
        if sample_start < boundary or at_ms <= sample_start:
            continue
        if quiet_start is None or quiet_end is None or sample_start > quiet_end:
            quiet_start, quiet_end = sample_start, at_ms
        else:
            quiet_end = max(quiet_end, at_ms)
        if quiet_end - quiet_start < required:
            continue
        runs.append(_ActivityRun(
            start_ms=start_ms, support_start_ms=support_start_ms,
            last_positive=last_positive, closed_at_ms=quiet_end))
        start_ms = support_start_ms = None
        last_positive = None
        quiet_start = quiet_end = None

    if start_ms is not None and support_start_ms is not None and last_positive is not None:
        runs.append(_ActivityRun(
            start_ms=start_ms, support_start_ms=support_start_ms,
            last_positive=last_positive, closed_at_ms=None))
    return tuple(runs)


def _target_activity_run(
    series: VocalEvidenceSeries, *, target_start_ms: int, target_observed_end_ms: int,
) -> _ActivityRun | None:
    """Return the latest run whose measured support intersects target span.

    This deliberately does not select an arbitrary later process-local voice.
    A run beginning after the observed target span belongs to no target merely
    because it is the latest positive in the process mix.
    """
    candidates = [
        run for run in _activity_runs(series)
        if int(run.last_positive.at_ms) >= int(target_start_ms)
        and int(run.support_start_ms) <= int(target_observed_end_ms)
    ]
    return candidates[-1] if candidates else None


def _run_closed_after(
    series: VocalEvidenceSeries, *, run: _ActivityRun, not_before_ms: int | None = None,
) -> bool:
    boundary = int(run.last_positive.at_ms)
    if not_before_ms is not None:
        boundary = max(boundary, int(not_before_ms))
    return _quiet_support_end(series, after_ms=boundary) is not None


def _open_activity_start(series: VocalEvidenceSeries) -> int | None:
    runs = _activity_runs(series)
    if not runs or not runs[-1].open:
        return None
    return int(runs[-1].start_ms)


def _partial_analysis_window_ms(lexical: LexicalProgressEvidence) -> int:
    """Bound extra acoustic analysis from observed target progress only.

    The ratio of unaligned to aligned pronunciation units determines how much
    additional *PCM analysis* is worthwhile after the latest aligned unit.  The
    result never declares when speech should end and is bounded so a partial
    mismatch eventually returns to UNKNOWN/fallback policy.
    """
    aligned = max(0, int(lexical.aligned_target_units))
    target = max(0, int(lexical.target_units))
    if (aligned <= 0 or target <= aligned or lexical.target_start_ms is None
            or lexical.aligned_end_ms is None):
        return 0
    observed_span = max(1, int(lexical.aligned_end_ms) - int(lexical.target_start_ms))
    remaining = target - aligned
    projected_analysis = observed_span * remaining / aligned
    return max(
        MIN_PARTIAL_ANALYSIS_MS,
        min(MAX_PARTIAL_ANALYSIS_MS, int(round(projected_analysis))),
    )


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
        measured: list[tuple[VocalEvidenceSeries, _ActivityRun]] = []
        for series in vocal.series if vocal is not None else ():
            run = _target_activity_run(
                series, target_start_ms=target_start,
                target_observed_end_ms=observed_target_end)
            if run is not None:
                measured.append((series, run))

        if not measured:
            return UtteranceProgress(
                state=UtteranceProgressState.UNKNOWN,
                evidence_at_ms=capture_end), None

        if complete:
            target_end = int(lexical.target_end_ms)
            open_risk = any(
                not _run_closed_after(
                    series, run=run, not_before_ms=target_end)
                for series, run in measured
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

        # A partial target is direct evidence that the known finite utterance
        # began.  Short observer valleys remain inside that activity run.  Even
        # after the run has enough measured quiet to close, keep the result
        # ACTIVE only for a bounded *analysis* window so the existing incremental
        # aligner can consume additional PCM and demonstrate resumed target
        # coverage.  If coverage does not advance through that observed window,
        # release to UNKNOWN rather than inventing an endpoint.
        if any(run.open for _series, run in measured):
            return UtteranceProgress(
                state=UtteranceProgressState.ACTIVE,
                evidence_at_ms=capture_end), None
        extension = _partial_analysis_window_ms(lexical)
        if extension > 0 and capture_end - int(lexical.aligned_end_ms) < extension:
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
