"""Native-audio observations and monotonic utterance knowledge.

Acquisition preserves what the aligner or transport actually measured.  This
module is the single reducer that turns target-conditioned alignment facts into
knowledge about the active Utterance.  Controller alone owns advancement.
"""
from __future__ import annotations

from dataclasses import dataclass

from .forced_alignment import TargetTimeAlignment


@dataclass(frozen=True)
class NativeAudioObservation:
    """One immutable observation for one Utterance generation."""

    kind: str
    alignment: TargetTimeAlignment | None = None
    capture_start_ms: int | None = None
    capture_end_ms: int | None = None
    target_acoustic_completion_ms: int | None = None
    pcm_last_frame_ms: int | None = None
    heartbeat_at_ms: int | None = None


@dataclass(frozen=True)
class TargetAssociation:
    """Provenance for the strongest positive target association observed."""

    observation: NativeAudioObservation

    @property
    def alignment(self) -> TargetTimeAlignment:
        if self.observation.alignment is None:
            raise RuntimeError("association without alignment provenance")
        return self.observation.alignment


@dataclass(frozen=True)
class TargetAcousticCompletion:
    """Committed completion of the known target pronunciation.

    This does not assert physical voice-file/channel EOF or absence of
    non-lexical vocal material outside the aligned target pronunciation.
    """

    estimated_completion_ms: int
    source: str
    observation: NativeAudioObservation


@dataclass(frozen=True)
class NativeVoiceKnowledge:
    """Monotonic knowledge about one target utterance."""

    association: TargetAssociation | None = None
    completion: TargetAcousticCompletion | None = None

    @property
    def target_associated(self) -> bool:
        return self.association is not None

    @property
    def target_acoustic_completed(self) -> bool:
        return self.completion is not None


def _association_rank(alignment: TargetTimeAlignment) -> tuple[int, int, float, float]:
    return (
        int(alignment.complete_alignment),
        alignment.aligned_target_units,
        alignment.fit.mean_unit_peak_margin,
        alignment.fit.path_margin,
    )


def _prefer_association(current: TargetAssociation | None,
                        observation: NativeAudioObservation) -> TargetAssociation | None:
    alignment = observation.alignment
    if alignment is None or not alignment.target_associated:
        return current
    candidate = TargetAssociation(observation)
    if current is None or _association_rank(candidate.alignment) > _association_rank(current.alignment):
        return candidate
    return current


def reduce_native_voice_knowledge(previous: NativeVoiceKnowledge,
                                  observation: NativeAudioObservation
                                  ) -> NativeVoiceKnowledge:
    """Grow target association/completion knowledge without inventing facts.

    Completion is accepted only when the same target-conditioned alignment both
    associated the target and closed the full target pronunciation with right
    context. It is deliberately narrower than physical source EOF. Transport
    heartbeats and PCM availability cannot manufacture this completion.
    """
    association = _prefer_association(previous.association, observation)
    completion = previous.completion
    alignment = observation.alignment
    if (completion is None and alignment is not None
            and alignment.target_associated
            and alignment.complete_alignment
            and alignment.committed_target_end
            and observation.target_acoustic_completion_ms is not None):
        completion = TargetAcousticCompletion(
            estimated_completion_ms=int(observation.target_acoustic_completion_ms),
            source="target_conditioned_alignment",
            observation=observation,
        )
    return NativeVoiceKnowledge(association=association, completion=completion)
