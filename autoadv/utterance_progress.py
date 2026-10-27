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
    """Current periodic repetition measured from one human-vocal observer."""

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


LOOP_REPEATABILITY = 0.60
LOOP_PERIOD_PROMINENCE = 0.10

# A threshold crossing is a measurement, not an endpoint. Releasing an already
# observed finite-vocal risk requires a contiguous span of explicitly measured
# sub-threshold support, never wall-clock passage alone.
MIN_QUIET_SUPPORT_MS = 240


@dataclass
class _ActivityRun:
    start_ms: int
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


def _quiet_support_required(vocal: HumanVocalEvidence | None) -> int:
    """Return one conservative disappearance horizon for the current evidence.

    Observer scores keep their own scales, but disappearance is a statement about
    the same process mix.  A narrow observer window must therefore not release a
    finite-vocal risk while another current observer still measures much wider
    acoustic support.  The widest factual support window becomes the common
    minimum quiet span; no source ownership or per-case timeout is inferred.
    """
    widths = (
        int(sample.at_ms) - int(sample.support_start_ms)
        for series in (vocal.series if vocal is not None else ())
        for sample in series.samples
    )
    return max(MIN_QUIET_SUPPORT_MS, max(widths, default=0))


def _series_activity_runs(
    series: VocalEvidenceSeries, *, quiet_required_ms: int,
) -> tuple[_ActivityRun, ...]:
    """Build one observer's finite activity runs from measured support only."""
    samples = tuple(sorted(
        series.samples,
        key=lambda sample: (int(sample.at_ms), int(sample.support_start_ms)),
    ))
    required = int(quiet_required_ms)
    threshold = float(series.active_score_threshold)
    runs: list[_ActivityRun] = []
    current: _ActivityRun | None = None

    for sample in samples:
        at_ms = int(sample.at_ms)
        support_start = int(sample.support_start_ms)
        positive = float(sample.score) >= threshold

        if positive:
            if current is None or not current.open:
                current = _ActivityRun(at_ms, at_ms, required)
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


def _activity_runs_after(
    series: VocalEvidenceSeries, *, timing_facts: tuple[int, ...], quiet_required_ms: int,
) -> tuple[_ActivityRun, ...]:
    """Return physical activity runs whose measured onset follows a time fact.

    Run segmentation is owned solely by ``_series_activity_runs``. Presentation
    timing relates whole measured runs to known facts; it never cuts a second
    onset out of score fluctuations inside an existing run.
    """
    if not timing_facts:
        return ()
    return tuple(
        run for run in _series_activity_runs(
            series, quiet_required_ms=quiet_required_ms)
        if any(run.start_ms >= fact and run.last_positive_ms > fact
               for fact in timing_facts)
    )


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


def evaluate_utterance_progress(
    *,
    capture_end_ms: int | None,
    lexical: LexicalProgressEvidence | None,
    vocal: HumanVocalEvidence | None,
    causal_lower_bound_ms: int | None = None,
    input_candidates_ms: tuple[int, ...] = (),
    recurrence_lookup: Callable[
        [str, int, int], tuple[VocalRecurrenceEvidence, ...]
    ] | None = None,
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

    quiet_required_ms = _quiet_support_required(vocal)
    observed: list[tuple[str, _ActivityRun]] = []
    for series in vocal.series if vocal is not None else ():
        observed.extend(
            (series.observer, run)
            for run in _activity_runs_after(
                series, timing_facts=facts, quiet_required_ms=quiet_required_ms)
        )

    measured_recurrence: VocalRecurrenceEvidence | None = None
    open_runs = tuple((observer, run) for observer, run in observed if run.open)
    if open_runs:
        # Reading amount is only permission to consult recurrence before a text
        # endpoint is committed; it never becomes endpoint evidence itself.
        reading_exceeded = bool(
            lexical is not None
            and lexical.target_units > 0
            and lexical.observed_units > lexical.target_units
        )
        recurrence_eligible = bool(
            not target_located or target_complete or reading_exceeded
        )

        stable_by_observer: dict[str, bool] = {}
        candidates: tuple[VocalRecurrenceEvidence, ...] = ()
        if recurrence_eligible and recurrence_lookup is not None:
            anchor = max(facts) if facts else capture_end
            recurrence_start = max(0, int(anchor) - max(0, capture_end - int(anchor)))
            collected: list[VocalRecurrenceEvidence] = []
            for observer in sorted({observer for observer, _run in open_runs}):
                observer_candidates = recurrence_lookup(
                    observer, recurrence_start, capture_end)
                collected.extend(observer_candidates)
                stable_by_observer[observer] = any(
                    _stable_recurrence(candidate) for candidate in observer_candidates)
            candidates = tuple(collected)
        measured_recurrence = _representative_recurrence(candidates)

        # Without a located text target, observers are alternate measurements of
        # one unresolved process-level vocal risk. One stable current recurrence
        # releases that generic risk; observer disagreement is not source evidence.
        if not target_located:
            state = (UtteranceProgressState.UNKNOWN
                     if any(stable_by_observer.values())
                     else UtteranceProgressState.ACTIVE)
            return UtteranceProgress(
                state=state, evidence_at_ms=capture_end), measured_recurrence

        # A located text target is stronger relation evidence. Recurrence from one
        # observer cannot erase finite target continuation still measured by
        # another, so every open observer must be recurrent before target-specific
        # protection can be released.
        if (not stable_by_observer or any(
                not stable_by_observer.get(observer, False)
                for observer, _run in open_runs)):
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
        run.start_ms <= target_end
        and run.last_positive_ms >= target_start
        and run.closed_after(target_end)
        for _observer, run in observed
    )
    return UtteranceProgress(
        state=(UtteranceProgressState.ENDED
               if physical_end_observed else UtteranceProgressState.UNKNOWN),
        evidence_at_ms=capture_end), measured_recurrence
