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
    target_acoustic_end_ms: int | None = None
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
class TargetEndConfirmation:
    """A committed target acoustic endpoint with its exact alignment provenance."""

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
    """Grow identity/end knowledge without inventing facts from missing evidence.

    A committed end is accepted only when the same target-conditioned alignment
    both associated the target and closed its complete tail with right context.
    Transport heartbeats and PCM availability can never manufacture an end.
    """
    association = _prefer_association(previous.association, observation)
    end = previous.end
    alignment = observation.alignment
    if (end is None and alignment is not None
            and alignment.target_associated
            and alignment.complete_alignment
            and alignment.committed_end
            and observation.target_acoustic_end_ms is not None):
        end = TargetEndConfirmation(
            estimated_end_ms=int(observation.target_acoustic_end_ms),
            source="target_conditioned_alignment",
            observation=observation,
        )
    return NativeVoiceKnowledge(association=association, end=end)
