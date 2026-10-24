"""Unified stateful inference for native dialogue-audio progression."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .acoustic_observers import HumanVocalEvidence, VocalRecurrenceEvidence
from .forced_alignment import TargetTimeAlignment


class PresentationBoundaryCertainty(str, Enum):
    AUTOADV = "autoadv"
    SESSION_START = "session_start"
    EXTERNAL_UNKNOWN = "external_unknown"


class UtteranceProgressState(str, Enum):
    UNRESOLVED = "unresolved"
    FINITE_VOCAL_ACTIVE = "finite_vocal_active"
    FINITE_VOCAL_ENDING = "finite_vocal_ending"
    PERSISTENT_OR_LOOPING = "persistent_or_looping"
    ENDED = "ended"


@dataclass(frozen=True)
class LexicalProgressEvidence:
    target_units: int
    aligned_target_units: int
    target_associated: bool
    complete_alignment: bool
    committed_target_end: bool
    target_end_ms: int | None
    reading_rank: int
    path_margin: float
    boundary_margin: float

    @classmethod
    def from_alignment(cls, alignment: TargetTimeAlignment,
                       target_end_ms: int | None) -> "LexicalProgressEvidence":
        return cls(
            target_units=alignment.target_units,
            aligned_target_units=alignment.aligned_target_units,
            target_associated=alignment.target_associated,
            complete_alignment=alignment.complete_alignment,
            committed_target_end=alignment.committed_target_end,
            target_end_ms=target_end_ms,
            reading_rank=alignment.reading_rank,
            path_margin=alignment.fit.path_margin,
            boundary_margin=alignment.fit.boundary_margin,
        )


@dataclass(frozen=True)
class NativeAudioObservation:
    """Independent evidence measured for one target generation."""

    kind: str
    capture_start_ms: int | None = None
    capture_end_ms: int | None = None
    lexical: LexicalProgressEvidence | None = None
    vocal: HumanVocalEvidence | None = None
    recurrence: VocalRecurrenceEvidence | None = None
    boundary_at_ms: int | None = None
    boundary_certainty: PresentationBoundaryCertainty = PresentationBoundaryCertainty.SESSION_START
    history_truncated: bool = False
    pcm_last_frame_ms: int | None = None
    heartbeat_at_ms: int | None = None


@dataclass(frozen=True)
class UtteranceProgress:
    state: UtteranceProgressState = UtteranceProgressState.UNRESOLVED
    ended_at_ms: int | None = None
    lexical_target_associated: bool = False
    lexical_target_ended: bool = False
    finite_vocal_observed: bool = False
    persistent_vocal_observed: bool = False
    boundary_certainty: PresentationBoundaryCertainty = PresentationBoundaryCertainty.SESSION_START
    evidence_at_ms: int | None = None

    @property
    def ended(self) -> bool:
        return self.state == UtteranceProgressState.ENDED

    @property
    def finite_vocal_incomplete(self) -> bool:
        return self.state in {
            UtteranceProgressState.FINITE_VOCAL_ACTIVE,
            UtteranceProgressState.FINITE_VOCAL_ENDING,
        }


class UtteranceProgressEstimator:
    """Combine acoustic evidence into one monotonic utterance state.

    No observer absence is itself an endpoint.  An end requires a finite vocal
    event history, or target-associated lexical completion combined with
    independent vocal-tail evidence.  Persistent/looping vocal sources are
    represented explicitly rather than becoming an infinite hold.
    """

    VOCAL_ACTIVE = 0.45
    FINITE_END_CONTEXT_MS = 160
    LOOP_STRENGTH = 0.70
    LOOP_STABILITY = 0.55
    CROSS_EPOCH_PERSISTENCE = 0.72
    CROSS_EPOCH_VOCAL_FRACTION = 0.55
    RECENT_PERSISTENCE = 0.60

    def __init__(self):
        self.progress = UtteranceProgress()
        self._seen_vocal = False
        self._last_active_ms: int | None = None
        self._first_active_ms: int | None = None
        self._lexical_end_ms: int | None = None
        self._lexical_target_ended = False
        self._target_associated = False
        self._persistent = False

    def observe(self, observation: NativeAudioObservation) -> UtteranceProgress:
        if self.progress.ended:
            return self.progress

        lexical = observation.lexical
        if lexical is not None:
            self._target_associated = self._target_associated or lexical.target_associated
            if lexical.target_associated and lexical.complete_alignment and lexical.committed_target_end:
                self._lexical_target_ended = True
                if lexical.target_end_ms is not None:
                    self._lexical_end_ms = lexical.target_end_ms

        vocal = observation.vocal
        if vocal is not None and vocal.first_active_ms is not None:
            self._seen_vocal = True
            self._first_active_ms = (vocal.first_active_ms if self._first_active_ms is None
                                     else min(self._first_active_ms, vocal.first_active_ms))
            if vocal.last_active_ms is not None:
                self._last_active_ms = (vocal.last_active_ms if self._last_active_ms is None
                                        else max(self._last_active_ms, vocal.last_active_ms))

        recurrence = observation.recurrence or VocalRecurrenceEvidence()
        persistent = bool(
            recurrence.cross_epoch_persistence >= self.CROSS_EPOCH_PERSISTENCE or
            (recurrence.strength >= self.LOOP_STRENGTH and
             recurrence.period_stability >= self.LOOP_STABILITY) or
            (vocal is not None and
             vocal.pre_epoch_active_fraction >= self.CROSS_EPOCH_VOCAL_FRACTION and
             vocal.active_fraction >= self.CROSS_EPOCH_VOCAL_FRACTION)
        )
        self._persistent = self._persistent or persistent

        capture_end = observation.capture_end_ms
        # Quiet is an acoustic relation between the last observed human-vocal
        # activity and the current capture frontier.  Do not dilute that signal
        # with whole-epoch averages: a long utterance followed by a valid quiet
        # tail necessarily has a high cumulative vocal fraction.
        quiet_after_vocal = (
            capture_end is not None and self._last_active_ms is not None and
            capture_end - self._last_active_ms >= self.FINITE_END_CONTEXT_MS
        )
        vocal_after_lexical = bool(
            self._lexical_end_ms is not None and self._last_active_ms is not None and
            self._last_active_ms > self._lexical_end_ms + 40
        )

        # A target-correlated lexical end can close only when independent vocal
        # evidence says no finite tail remains.  Historical persistence is not
        # enough: while vocal activity continues after the lexical boundary, its
        # *recent* representation must still match the recurring source.  This
        # prevents a background vocal loop from cutting a newly started cry or
        # breath tail merely because the loop existed earlier.
        persistent_only_tail = bool(
            self._persistent and recurrence.recent_strength >= self.RECENT_PERSISTENCE
        )
        if self._lexical_target_ended:
            # Lexical completion is evidence about the known text, not proof that
            # a finite human-vocal event was ever observed.  In an unvoiced line
            # (or a false lexical match against background audio) keep the
            # utterance unresolved so the explicit uncertainty fallback remains
            # available instead of inventing a finite-vocal ending state.
            if not self._seen_vocal:
                state = UtteranceProgressState.UNRESOLVED
                ended_at = None
            elif vocal_after_lexical and not quiet_after_vocal and not persistent_only_tail:
                state = UtteranceProgressState.FINITE_VOCAL_ACTIVE
                ended_at = None
            elif persistent_only_tail:
                state = UtteranceProgressState.ENDED
                ended_at = self._lexical_end_ms
            elif quiet_after_vocal:
                state = UtteranceProgressState.ENDED
                ended_at = max(
                    value for value in (self._last_active_ms, self._lexical_end_ms)
                    if value is not None
                )
            else:
                state = UtteranceProgressState.FINITE_VOCAL_ENDING
                ended_at = None
        elif self._seen_vocal:
            # Non-lexical finite events can complete only when their onset is
            # causally scoped by AutoAdv's own boundary.  An external transition
            # has unknown start time and therefore remains unresolved/persistent
            # rather than inventing a boundary from HOOK arrival.
            if persistent or self._persistent:
                state = UtteranceProgressState.PERSISTENT_OR_LOOPING
                ended_at = None
            elif (observation.boundary_certainty == PresentationBoundaryCertainty.AUTOADV
                  and quiet_after_vocal and not observation.history_truncated):
                state = UtteranceProgressState.ENDED
                ended_at = self._last_active_ms
            elif quiet_after_vocal:
                state = UtteranceProgressState.FINITE_VOCAL_ENDING
                ended_at = None
            else:
                state = UtteranceProgressState.FINITE_VOCAL_ACTIVE
                ended_at = None
        elif persistent:
            state = UtteranceProgressState.PERSISTENT_OR_LOOPING
            ended_at = None
        else:
            state = UtteranceProgressState.UNRESOLVED
            ended_at = None

        self.progress = UtteranceProgress(
            state=state,
            ended_at_ms=ended_at,
            lexical_target_associated=self._target_associated,
            lexical_target_ended=self._lexical_target_ended,
            finite_vocal_observed=self._seen_vocal,
            persistent_vocal_observed=self._persistent,
            boundary_certainty=observation.boundary_certainty,
            evidence_at_ms=capture_end,
        )
        return self.progress
