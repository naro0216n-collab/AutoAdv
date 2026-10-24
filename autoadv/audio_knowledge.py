"""Native-audio observations and monotonic utterance knowledge.

Acquisition reports what was measured.  This module is the single place where
those measurements are reduced into facts about the active target utterance.
It never decides whether the game should advance; Controller owns that policy.
"""
from __future__ import annotations

from dataclasses import dataclass

from .audio_match import TargetAlignment, alignment_support_rank


MIN_ASSOCIATED_PHONEMES = 3
ALIGNMENT_ERROR_DIVISOR = 4


def _allowed_edits(units: int) -> int:
    return max(0, int(units) // ALIGNMENT_ERROR_DIVISOR)


def alignment_associates_target(alignment: TargetAlignment) -> bool:
    """Whether one alignment is strong enough to associate audio with target.

    This establishes identity only.  It says nothing about whether the target
    is still playing or has ended.
    """
    target = alignment.phonetic_target_units
    prefix = alignment.phonetic_prefix_units
    if target <= 0 or prefix <= 0:
        return False
    minimum_matches = min(MIN_ASSOCIATED_PHONEMES, target)
    return bool(
        alignment.phonetic_matches >= minimum_matches
        and alignment.phonetic_edits <= _allowed_edits(prefix)
    )


def alignment_reaches_target_tail(alignment: TargetAlignment) -> bool:
    """Whether the measured alignment reaches the configured target tail.

    Reaching the tail is still only an alignment fact.  An acoustic end time is
    required separately before end knowledge can be formed.
    """
    return bool(
        alignment_associates_target(alignment)
        and alignment.phonetic_prefix_units == alignment.phonetic_target_units
        and alignment.phonetic_edits <= _allowed_edits(alignment.phonetic_target_units)
    )


@dataclass(frozen=True)
class NativeAudioObservation:
    """One immutable observation for the active Utterance generation.

    Fields retain source semantics.  In particular, ASR token starts, VAD
    boundaries and process PCM gaps are not target-voice endpoints.
    ``target_acoustic_end_ms`` is reserved for a backend observation whose
    contract directly locates the acoustic end of the target-aligned tail.
    """
    kind: str
    alignment: TargetAlignment = TargetAlignment()
    capture_end_ms: int | None = None
    target_last_token_start_ms: int | None = None
    target_successor_token_start_ms: int | None = None
    target_acoustic_end_ms: int | None = None
    target_acoustic_end_source: str = ''
    pcm_last_frame_ms: int | None = None
    heartbeat_at_ms: int | None = None
    vad_onset_ms: int | None = None
    vad_end_ms: int | None = None
    vad_tail_ms: int = 0


@dataclass(frozen=True)
class TargetAssociation:
    """Provenance for the strongest target association observed so far."""
    observation: NativeAudioObservation

    @property
    def alignment(self) -> TargetAlignment:
        return self.observation.alignment


@dataclass(frozen=True)
class TargetEndConfirmation:
    """A directly constrained target acoustic endpoint with provenance."""
    estimated_end_ms: int
    source: str
    observation: NativeAudioObservation


@dataclass(frozen=True)
class NativeVoiceKnowledge:
    """Monotonic knowledge about one target utterance."""
    association: TargetAssociation | None = None
    end: TargetEndConfirmation | None = None

    @property
    def target_associated(self) -> bool:
        return self.association is not None

    @property
    def target_end_confirmed(self) -> bool:
        return self.end is not None


def _prefer_association(current: TargetAssociation | None,
                        observation: NativeAudioObservation) -> TargetAssociation | None:
    if not alignment_associates_target(observation.alignment):
        return current
    candidate = TargetAssociation(observation)
    if current is None:
        return candidate
    if alignment_support_rank(candidate.alignment) > alignment_support_rank(current.alignment):
        return candidate
    return current


def reduce_native_voice_knowledge(previous: NativeVoiceKnowledge,
                                  observation: NativeAudioObservation
                                  ) -> NativeVoiceKnowledge:
    """Accumulate target identity and endpoint knowledge without reclassification.

    Knowledge only grows inside one Utterance generation.  ASR regression does
    not erase earlier association.  Generic transport/VAD/successor-token facts
    remain observations and cannot manufacture target-end knowledge.
    """
    association = _prefer_association(previous.association, observation)
    end = previous.end

    if (end is None
            and observation.target_acoustic_end_ms is not None
            and observation.target_acoustic_end_source
            and alignment_reaches_target_tail(observation.alignment)):
        end = TargetEndConfirmation(
            estimated_end_ms=int(observation.target_acoustic_end_ms),
            source=observation.target_acoustic_end_source,
            observation=observation,
        )

    return NativeVoiceKnowledge(association=association, end=end)
