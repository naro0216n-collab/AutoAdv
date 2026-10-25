"""Stateful inference of whether the current presentation voice is active, ended or unknown.

The observers in this module describe acoustic facts.  The estimator combines
known-text relation, causal timing and human-vocal activity into one presentation
judgment.  It never classifies foreground/background sources and never owns game
input or fallback policy.
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
    """Infer whether the current presentation still has a finite vocal event.

    The estimator does not identify speakers or classify foreground/background
    sources.  A vocal event can start from current known-text alignment or from
    a new human-vocal onset after a known AutoAdv causal boundary.  Once open,
    the event remains ACTIVE through short internal quiet and closes only after
    enough acoustic quiet has actually been observed.  Transport heartbeats do
    not mutate this acoustic state.

    Lexical evidence still records target association for diagnostics and can
    turn a closed event into ENDED. A causal-only event is selected only after
    the target has attached: retained pre-HOOK audio is re-evaluated against that
    stronger boundary instead of freezing the first post-input vocal onset. The
    selected unowned event merely suppresses fallback while it is open; when it
    closes the result returns to UNKNOWN.
    """

    END_CONTEXT_MS = 180
    EVENT_CLOSE_QUIET_MS = END_CONTEXT_MS * 2
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
        self._recurrent_tail_observed = False

        self._event_open = False
        self._event_start_ms: int | None = None
        self._event_last_vocal_ms: int | None = None
        self._event_has_lexical_anchor = False
        self._causal_candidate_consumed = False

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

    def _extend_event_from_interval(self, interval: tuple[int, int]) -> bool:
        """Extend the open event unless observed quiet would already close it."""
        start_ms, end_ms = interval
        if not self._event_open or self._event_last_vocal_ms is None:
            return False
        if end_ms <= self._event_last_vocal_ms:
            return False
        if start_ms - self._event_last_vocal_ms >= self.EVENT_CLOSE_QUIET_MS:
            return False
        self._event_last_vocal_ms = end_ms
        return True

    def _lexical_event(self, vocal: HumanVocalEvidence | None) -> tuple[int, int] | None:
        """Return one finite vocal event connected to the aligned target span."""
        if (
            vocal is None
            or not vocal.active_intervals
            or not self._target_audio_associated
            or self._target_span_start_ms is None
            or self._target_span_end_ms is None
        ):
            return None
        intervals = tuple(sorted(vocal.active_intervals))
        anchor_index = next((
            i for i, interval in enumerate(intervals)
            if self._overlaps(interval, self._target_span_start_ms, self._target_span_end_ms)
        ), None)
        if anchor_index is None:
            return None
        start_ms, end_ms = intervals[anchor_index]
        for interval in intervals[anchor_index + 1:]:
            if interval[0] - end_ms >= self.EVENT_CLOSE_QUIET_MS:
                break
            end_ms = max(end_ms, interval[1])
        return start_ms, end_ms

    def _causal_event(
        self, observation: NativeAudioObservation, vocal: HumanVocalEvidence | None,
    ) -> tuple[int, int] | None:
        """Return one still-open causal candidate re-evaluated at target attach.

        AutoAdv input is the lower bound for the presentation, while HOOK/target
        attachment tells us which presentation retained audio must now be judged
        against. A vocal interval that closed before target attachment is therefore
        not frozen in as the current candidate merely because it was the first
        post-input onset. Delayed HOOK remains supported when the same finite event
        is still active at attachment.

        Causal timing is not source ownership, so only one unowned candidate may
        suppress fallback. After that candidate closes, later unrelated human-vocal
        activity is not repeatedly promoted; known-text evidence may still establish
        target ownership independently.
        """
        if self._causal_candidate_consumed or vocal is None or not vocal.active_intervals:
            return None
        attached_ms = observation.target_attached_ms
        capture_end_ms = observation.capture_end_ms
        if attached_ms is None or capture_end_ms is None or capture_end_ms < attached_ms:
            return None

        intervals = tuple(sorted(vocal.active_intervals))
        chains: list[tuple[int, int]] = []
        for start_ms, end_ms in intervals:
            if chains and start_ms - chains[-1][1] < self.EVENT_CLOSE_QUIET_MS:
                chains[-1] = (chains[-1][0], max(chains[-1][1], end_ms))
            else:
                chains.append((start_ms, end_ms))

        # If voice was already active across the strong AutoAdv input lower bound,
        # the first chain is inherited context. It cannot become current merely
        # because a later HOOK identifies the text. A later separated chain can
        # still be considered.
        if vocal.active_across_causal_boundary and chains:
            chains = chains[1:]

        open_candidates = [
            (start_ms, end_ms)
            for start_ms, end_ms in chains
            if end_ms + self.ALIGNMENT_ATTACH_GRACE_MS >= attached_ms
            and capture_end_ms - end_ms < self.EVENT_CLOSE_QUIET_MS
        ]
        if not open_candidates:
            return None
        return max(open_candidates, key=lambda interval: (interval[1], interval[0]))

    def _open_event(self, start_ms: int, end_ms: int, *, lexical: bool) -> None:
        self._event_open = True
        self._event_start_ms = start_ms
        self._event_last_vocal_ms = end_ms
        self._event_has_lexical_anchor = lexical

    def _clear_event(self) -> None:
        self._event_open = False
        self._event_start_ms = None
        self._event_last_vocal_ms = None
        self._event_has_lexical_anchor = False

    def _event_connected_to(self, start_ms: int, end_ms: int) -> bool:
        if (not self._event_open or self._event_start_ms is None
                or self._event_last_vocal_ms is None):
            return False
        if end_ms < self._event_start_ms:
            return self._event_start_ms - end_ms < self.EVENT_CLOSE_QUIET_MS
        if start_ms > self._event_last_vocal_ms:
            return start_ms - self._event_last_vocal_ms < self.EVENT_CLOSE_QUIET_MS
        return True

    def _update_open_event(self, vocal: HumanVocalEvidence | None) -> None:
        if not self._event_open or vocal is None:
            return
        for interval in sorted(vocal.active_intervals):
            self._extend_event_from_interval(interval)

    def _stable_recurrence(self, recurrence: VocalRecurrenceEvidence,
                           capture_end_ms: int | None) -> bool:
        if (
            recurrence.period_ms is None
            or recurrence.strength < self.LOOP_STRENGTH
            or recurrence.period_stability < self.LOOP_STABILITY
            or recurrence.recent_strength < self.RECENT_LOOP_STRENGTH
            or self._event_start_ms is None
            or capture_end_ms is None
        ):
            return False
        observed_ms = capture_end_ms - self._event_start_ms
        return observed_ms >= max(self.MIN_LOOP_OBSERVATION_MS, recurrence.period_ms * 2)

    def observe(self, observation: NativeAudioObservation) -> UtteranceProgress:
        if self.progress.ended:
            return self.progress

        # A transport heartbeat carries no new acoustic interval.  It may cause
        # NativeAudio to flush pending PCM, but it must never advance, clear or
        # confirm the vocal-event lifecycle by itself.
        if observation.capture_end_ms is None:
            return self.progress

        capture_end = observation.capture_end_ms
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
        lexical_event = self._lexical_event(vocal)
        if lexical_event is not None:
            self._target_vocal_observed = True
            if not self._event_open:
                # Do not reopen a vocal event whose quiet endpoint is already
                # visible in this same retained observation.
                if capture_end - lexical_event[1] < self.EVENT_CLOSE_QUIET_MS:
                    self._open_event(*lexical_event, lexical=True)
            elif self._event_connected_to(*lexical_event):
                self._event_has_lexical_anchor = True
                self._update_open_event(vocal)
            elif not self._event_has_lexical_anchor:
                # Known-text relation is stronger than the provisional causal
                # guard. If target evidence points to a separate event, replace
                # the unowned candidate instead of letting old/background vocal
                # activity block the current target. An already-closed lexical
                # event remains closed so the delayed-END branch below can use it.
                self._clear_event()
                if capture_end - lexical_event[1] < self.EVENT_CLOSE_QUIET_MS:
                    self._open_event(*lexical_event, lexical=True)

        if self._event_open:
            self._update_open_event(vocal)
        elif observation.causal_lower_bound_ms is not None:
            causal_event = self._causal_event(observation, vocal)
            if causal_event is not None:
                start_ms, end_ms = causal_event
                self._causal_candidate_consumed = True
                self._open_event(start_ms, end_ms, lexical=False)

        recurrence = observation.recurrence or VocalRecurrenceEvidence()
        recurrent_tail = bool(
            self._event_open
            and self._event_has_lexical_anchor
            and self._lexical_target_ended
            and self._stable_recurrence(recurrence, capture_end)
        )
        self._recurrent_tail_observed = self._recurrent_tail_observed or recurrent_tail

        ended_at = None
        if recurrent_tail:
            state = UtteranceProgressState.ENDED
            ended_at = self._lexical_end_ms
            self._event_open = False
        elif self._event_open and self._event_last_vocal_ms is not None:
            quiet_ms = capture_end - self._event_last_vocal_ms
            if quiet_ms >= self.EVENT_CLOSE_QUIET_MS:
                lexical_event_closed = self._event_has_lexical_anchor and self._lexical_target_ended
                event_end_ms = self._event_last_vocal_ms
                self._clear_event()
                if lexical_event_closed:
                    state = UtteranceProgressState.ENDED
                    ended_at = max(
                        value for value in (self._lexical_end_ms, event_end_ms)
                        if value is not None
                    )
                else:
                    state = UtteranceProgressState.UNKNOWN
            else:
                state = UtteranceProgressState.ACTIVE
        elif (
            lexical_event is not None
            and self._lexical_target_ended
            and capture_end - lexical_event[1] >= self.EVENT_CLOSE_QUIET_MS
        ):
            # Delayed HOOK/alignment may first observe an already-finished event.
            # The clean lexical relation plus retained vocal interval is enough
            # to infer that the finite target event has ended without replaying
            # an artificial ACTIVE period.
            state = UtteranceProgressState.ENDED
            ended_at = max(
                value for value in (self._lexical_end_ms, lexical_event[1])
                if value is not None
            )
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
