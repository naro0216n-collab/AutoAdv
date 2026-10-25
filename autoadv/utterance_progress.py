"""Stateful inference of whether the current target voice is active, ended or unknown.

The observers in this module describe acoustic facts.  The estimator combines
those facts only after it can associate them with the current target.  It never
owns game input or fallback policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .forced_alignment import TargetTimeAlignment


@dataclass(frozen=True)
class HumanVocalEvidence:
    """Frame-derived human-vocal activity over one analysis interval."""

    start_ms: int
    end_ms: int
    frame_count: int
    mean_probability: float
    peak_probability: float
    active_fraction: float
    first_active_ms: int | None
    last_active_ms: int | None
    active_intervals: tuple[tuple[int, int], ...] = ()
    active_across_causal_boundary: bool = False
    post_causal_onset_ms: int | None = None


@dataclass(frozen=True)
class VocalRecurrenceEvidence:
    """Similarity evidence for continuing human-vocal weighted acoustics."""

    strength: float = 0.0
    period_ms: int | None = None
    period_stability: float = 0.0
    recent_strength: float = 0.0


class UtteranceProgressState(str, Enum):
    UNKNOWN = "unknown"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass(frozen=True)
class LexicalProgressEvidence:
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
    """Independent acoustic facts measured for one target generation."""

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
    state: UtteranceProgressState = UtteranceProgressState.UNKNOWN
    ended_at_ms: int | None = None
    target_audio_associated: bool = False
    lexical_target_ended: bool = False
    target_vocal_observed: bool = False
    recurrent_tail_observed: bool = False
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


class UtteranceProgressEstimator:
    """Combine target relation, vocal continuity and recurrence by purpose.

    Known-text alignment establishes target ownership. Human-vocal activity then
    describes the physical continuation of that target, including a terminal
    continuation that can resume after an observed quiet interval. A causal vocal
    onset remains timing evidence only: process-mix speech is not target-owned
    merely because it began after a presentation boundary. Recurrence is consulted
    only for an already-owned continuation, never to establish ownership by itself.
    """

    END_CONTEXT_MS = 180
    ALIGNMENT_ATTACH_GRACE_MS = 120
    LOOP_STRENGTH = 0.72
    LOOP_STABILITY = 0.60
    RECENT_LOOP_STRENGTH = 0.62
    MIN_LOOP_OBSERVATION_MS = 1200

    def __init__(self):
        self.progress = UtteranceProgress()
        self._target_audio_associated = False
        self._lexical_target_ended = False
        self._lexical_end_ms: int | None = None
        self._target_span_start_ms: int | None = None
        self._target_span_end_ms: int | None = None
        self._target_vocal_observed = False
        self._target_vocal_start_ms: int | None = None
        self._last_target_vocal_ms: int | None = None
        self._quiet_candidate_vocal_ms: int | None = None
        self._quiet_candidate_evidence_ms: int | None = None
        self._recurrent_tail_observed = False

    @staticmethod
    def _overlaps(interval: tuple[int, int], start_ms: int, end_ms: int) -> bool:
        left, right = interval
        return right >= start_ms and left <= end_ms

    def _lexical_is_current(self, observation: NativeAudioObservation,
                            lexical: LexicalProgressEvidence) -> bool:
        if not lexical.target_associated:
            return False
        # A known AutoAdv/session lower bound constrains the analyzed audio to
        # the current causal presentation.  With no known lower bound, retained
        # history may contain an older matching line; require alignment evidence
        # to reach the factual target-attach time before claiming ownership.
        if observation.causal_lower_bound_ms is not None:
            return True
        if observation.target_attached_ms is None or lexical.aligned_end_ms is None:
            return False
        return lexical.aligned_end_ms + self.ALIGNMENT_ATTACH_GRACE_MS >= observation.target_attached_ms

    def _connected_target_vocal(self, vocal: HumanVocalEvidence,
                                lexical: LexicalProgressEvidence | None) -> tuple[int, int] | None:
        intervals = vocal.active_intervals
        if not intervals or lexical is None or not self._target_audio_associated:
            return None

        # Known-text association is the ownership anchor.  Vocal intervals that
        # overlap the aligned target span belong to the same target evidence even
        # when VAD split the lexical performance around internal pauses.  Do not
        # bridge later activity with a fixed gap and do not use a merely
        # post-causal onset to manufacture non-lexical ownership.
        start = lexical.target_start_ms or self._target_span_start_ms
        end = lexical.aligned_end_ms or lexical.target_end_ms or self._target_span_end_ms
        if start is None or end is None:
            return None
        owned = [interval for interval in intervals if self._overlaps(interval, start, end)]
        if not owned:
            return None
        return min(interval[0] for interval in owned), max(interval[1] for interval in owned)

    def _extend_open_lexical_tail(self, vocal: HumanVocalEvidence | None,
                                  capture_end_ms: int | None) -> bool:
        """Extend an already-owned lexical event while a terminal vocal is live.

        This is deliberately not a gap threshold.  Once lexical ownership exists,
        a newly active human-vocal interval after the aligned span keeps the same
        finite event open until endpoint confirmation.  Intervals that have already
        gone quiet before the current observation do not retroactively acquire
        target ownership.  A line without lexical ownership never enters this path.
        """
        if (
            vocal is None
            or capture_end_ms is None
            or not self._lexical_target_ended
            or not self._target_vocal_observed
            or self._target_span_end_ms is None
        ):
            return False
        last_target_ms = self._last_target_vocal_ms
        if last_target_ms is None:
            return False
        endpoint_candidate_since = last_target_ms + self.END_CONTEXT_MS
        endpoint_candidate_open = (
            self._quiet_candidate_vocal_ms == last_target_ms
            and self._quiet_candidate_evidence_ms is not None
        )
        candidates = [
            interval for interval in vocal.active_intervals
            if interval[0] > self._target_span_end_ms
            and interval[1] > last_target_ms
            and capture_end_ms - interval[1] < self.END_CONTEXT_MS
            and (
                interval[0] <= endpoint_candidate_since
                or (endpoint_candidate_open and interval[0] > endpoint_candidate_since)
            )
        ]
        if not candidates:
            return False
        _, end_ms = max(candidates, key=lambda interval: interval[1])
        self._last_target_vocal_ms = max(self._last_target_vocal_ms or end_ms, end_ms)
        self._quiet_candidate_vocal_ms = None
        self._quiet_candidate_evidence_ms = None
        return True

    def _quiet_endpoint_confirmed(self, *, target_quiet: bool,
                                  capture_end_ms: int | None) -> bool:
        """Require a later observation to confirm a quiet endpoint candidate.

        A single inactive tail can be an internal pause before a cry, breath, laugh
        or other non-lexical continuation.  The first quiet observation therefore
        records an endpoint candidate; only a later observation with the same last
        target-vocal frontier confirms it.  Any resumed owned continuation clears
        the candidate.
        """
        if not target_quiet or capture_end_ms is None or self._last_target_vocal_ms is None:
            self._quiet_candidate_vocal_ms = None
            self._quiet_candidate_evidence_ms = None
            return False
        if (
            self._quiet_candidate_vocal_ms == self._last_target_vocal_ms
            and self._quiet_candidate_evidence_ms is not None
            and capture_end_ms > self._quiet_candidate_evidence_ms
        ):
            return True
        self._quiet_candidate_vocal_ms = self._last_target_vocal_ms
        self._quiet_candidate_evidence_ms = capture_end_ms
        return False

    def _stable_recurrence(self, recurrence: VocalRecurrenceEvidence,
                           capture_end_ms: int | None) -> bool:
        if (
            recurrence.period_ms is None
            or recurrence.strength < self.LOOP_STRENGTH
            or recurrence.period_stability < self.LOOP_STABILITY
            or recurrence.recent_strength < self.RECENT_LOOP_STRENGTH
            or self._target_vocal_start_ms is None
            or capture_end_ms is None
        ):
            return False
        observed_ms = capture_end_ms - self._target_vocal_start_ms
        return observed_ms >= max(self.MIN_LOOP_OBSERVATION_MS, recurrence.period_ms * 2)

    def observe(self, observation: NativeAudioObservation) -> UtteranceProgress:
        if self.progress.ended:
            return self.progress

        lexical = observation.lexical
        current_lexical = False
        if lexical is not None:
            current_lexical = self._lexical_is_current(observation, lexical)
            if current_lexical:
                self._target_audio_associated = True
                if lexical.target_start_ms is not None:
                    self._target_span_start_ms = (
                        lexical.target_start_ms if self._target_span_start_ms is None
                        else min(self._target_span_start_ms, lexical.target_start_ms)
                    )
                if lexical.aligned_end_ms is not None:
                    self._target_span_end_ms = (
                        lexical.aligned_end_ms if self._target_span_end_ms is None
                        else max(self._target_span_end_ms, lexical.aligned_end_ms)
                    )
                if lexical.complete_alignment and lexical.committed_target_end:
                    self._lexical_target_ended = True
                    if lexical.target_end_ms is not None:
                        self._lexical_end_ms = lexical.target_end_ms

        vocal = observation.vocal
        owned_vocal = None
        if vocal is not None:
            owned_vocal = self._connected_target_vocal(
                vocal, lexical if current_lexical else None)
        if owned_vocal is not None:
            self._target_audio_associated = True
            self._target_vocal_observed = True
            start_ms, end_ms = owned_vocal
            self._target_vocal_start_ms = (
                start_ms if self._target_vocal_start_ms is None
                else min(self._target_vocal_start_ms, start_ms)
            )
            self._last_target_vocal_ms = (
                end_ms if self._last_target_vocal_ms is None
                else max(self._last_target_vocal_ms, end_ms)
            )

        capture_end = observation.capture_end_ms
        self._extend_open_lexical_tail(vocal, capture_end)
        target_quiet = bool(
            capture_end is not None
            and self._last_target_vocal_ms is not None
            and capture_end - self._last_target_vocal_ms >= self.END_CONTEXT_MS
        )
        target_currently_active = bool(
            capture_end is not None
            and self._last_target_vocal_ms is not None
            and capture_end - self._last_target_vocal_ms < self.END_CONTEXT_MS
        )
        quiet_endpoint_confirmed = self._quiet_endpoint_confirmed(
            target_quiet=target_quiet, capture_end_ms=capture_end)
        recurrence = observation.recurrence or VocalRecurrenceEvidence()
        recurrent_tail = bool(
            self._target_audio_associated
            and self._target_vocal_observed
            and target_currently_active
            and self._stable_recurrence(recurrence, capture_end)
        )
        self._recurrent_tail_observed = self._recurrent_tail_observed or recurrent_tail

        ended_at = None
        if not self._target_audio_associated:
            state = UtteranceProgressState.UNKNOWN
        elif self._lexical_target_ended:
            if not self._target_vocal_observed:
                # Alignment alone does not prove physical vocal completion.
                state = UtteranceProgressState.UNKNOWN
            elif recurrent_tail:
                # The target text has ended and the remaining target-connected
                # vocal structure is stably repeating; further waiting is not
                # evidence of finite progression.
                state = UtteranceProgressState.ENDED
                ended_at = self._lexical_end_ms
            elif target_quiet and quiet_endpoint_confirmed:
                state = UtteranceProgressState.ENDED
                ended_at = max(
                    value for value in (self._lexical_end_ms, self._last_target_vocal_ms)
                    if value is not None
                )
            elif target_quiet:
                # Keep the finite target event open through the first terminal
                # quiet observation.  This blocks uncertainty fallback from
                # turning a normal internal pause into an early advance.
                state = UtteranceProgressState.ACTIVE
            elif target_currently_active:
                state = UtteranceProgressState.ACTIVE
            else:
                state = UtteranceProgressState.UNKNOWN
        elif self._target_vocal_observed:
            if recurrent_tail:
                # For non-lexical material a stable repeated target-connected
                # structure is itself evidence that finite progression has
                # ceased, while recurrence alone can never establish ownership.
                state = UtteranceProgressState.ENDED
                ended_at = capture_end
            elif (target_quiet and quiet_endpoint_confirmed
                  and observation.causal_lower_bound_ms is not None
                  and not observation.history_truncated):
                state = UtteranceProgressState.ENDED
                ended_at = self._last_target_vocal_ms
            elif target_quiet:
                state = UtteranceProgressState.ACTIVE
            elif target_currently_active:
                state = UtteranceProgressState.ACTIVE
            else:
                state = UtteranceProgressState.UNKNOWN
        else:
            state = UtteranceProgressState.UNKNOWN

        self.progress = UtteranceProgress(
            state=state,
            ended_at_ms=ended_at,
            target_audio_associated=self._target_audio_associated,
            lexical_target_ended=self._lexical_target_ended,
            target_vocal_observed=self._target_vocal_observed,
            recurrent_tail_observed=self._recurrent_tail_observed,
            evidence_at_ms=capture_end,
        )
        return self.progress
