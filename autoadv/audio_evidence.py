"""Minimal native-voice evidence assessment.

Capture, ASR/G2P, token timing and VAD remain observations. Product judgement
keeps their meanings separate: ASR establishes target association and a bounded
amount of terminal support, while independent temporal/acoustic boundaries can
establish that the associated render interval has ended. No weighted evidence
score and no exact full-target gate are used.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum

from .audio_match import TargetAlignment


class VoiceState(str, Enum):
    ENDED = 'ended'
    PLAYING = 'playing'
    INCONCLUSIVE = 'inconclusive'


@dataclass(frozen=True)
class VoiceEvidence:
    """Raw observations for one update of the active Utterance.

    ``frontier_*`` is one coherent earlier ASR snapshot with the greatest
    target support observed so far. NativeAudio preserves that snapshot rather
    than combining independent historical maxima or reducing the comparison to
    a semantic ``progressed`` flag.
    """
    observation_kind: str
    alignment: TargetAlignment
    frontier_alignment: TargetAlignment
    capture_end_ms: int | None
    frontier_capture_end_ms: int | None
    target_last_token_ms: int | None
    target_boundary_ms: int | None
    render_end_ms: int | None
    render_quiet_at_ms: int | None
    vad_onset_ms: int | None
    vad_tail_ms: int
    vad_end_ms: int | None


@dataclass(frozen=True)
class VoiceAssessment:
    state: VoiceState
    estimated_end_ms: int | None = None
    endpoint_source: str = ''


# One pronunciation-error budget governs attribution and terminal support.
# Very short targets remain exact because one edit can erase most of their
# identity; this is an attribution constraint, not an exact end-of-speech gate.
MIN_CORRELATED_PHONEMES = 3
ALIGNMENT_ERROR_DIVISOR = 4

# These are direct acoustic/transport boundary durations, not confidence or
# repeated-observation counters.
VAD_ENDPOINT_SILENCE_MS = 200
RENDER_ENDPOINT_QUIET_MS = 300


def _allowed_edits(units: int) -> int:
    return max(0, int(units) // ALIGNMENT_ERROR_DIVISOR)


def _target_correlated(alignment: TargetAlignment) -> bool:
    target = alignment.phonetic_target_units
    prefix = alignment.phonetic_prefix_units
    if target <= 0 or prefix <= 0:
        return False
    minimum_matches = min(MIN_CORRELATED_PHONEMES, target)
    return bool(
        alignment.phonetic_matches >= minimum_matches
        and alignment.phonetic_edits <= _allowed_edits(prefix)
    )


def _unobserved_tail_units(alignment: TargetAlignment) -> int:
    return max(0, alignment.phonetic_target_units - alignment.phonetic_prefix_units)


def _target_terminally_supported(alignment: TargetAlignment) -> bool:
    """Whether target support is close enough to an endpoint to prove ENDED.

    An unrecognized suffix is treated as another bounded recognition error.
    The same edit budget covers substitutions/deletions inside the observed
    prefix and a missing tail. This permits ordinary ASR tail loss without
    turning an arbitrary early prefix plus silence into completion evidence.
    """
    if not _target_correlated(alignment):
        return False
    target = alignment.phonetic_target_units
    unexplained = alignment.phonetic_edits + _unobserved_tail_units(alignment)
    return unexplained <= _allowed_edits(target)


def _target_support_advanced(evidence: VoiceEvidence) -> bool:
    """Did target support exceed the prior coherent frontier in new PCM?"""
    if evidence.observation_kind != 'asr' or not _target_correlated(evidence.alignment):
        return False
    if (evidence.target_last_token_ms is None
            or evidence.frontier_capture_end_ms is None):
        return False
    return bool(
        evidence.target_last_token_ms > evidence.frontier_capture_end_ms
        and evidence.alignment.phonetic_matches
        > evidence.frontier_alignment.phonetic_matches
    )


def assess_voice(evidence: VoiceEvidence) -> VoiceAssessment:
    """Map raw observations to ENDED / PLAYING / INCONCLUSIVE.

    ENDED requires two different facts: target support close enough to the
    target tail under the ordinary recognition-error budget, and one direct
    endpoint observation. The endpoint may be process render quiescence, a VAD
    speech boundary, or an ASR successor token. Exact target recognition is not
    required, but an arbitrary early prefix cannot be promoted to completion by
    silence alone.

    PLAYING requires target support to exceed the coherent historical frontier
    in PCM that did not exist when that frontier snapshot was decoded. ASR
    regression followed by recovery to the same support therefore does not
    masquerade as new speech progress.
    """
    endpoint_supported = (
        _target_terminally_supported(evidence.alignment)
        or _target_terminally_supported(evidence.frontier_alignment)
    )

    if endpoint_supported:
        render_quiet_ms = None
        if evidence.render_end_ms is not None and evidence.render_quiet_at_ms is not None:
            render_quiet_ms = evidence.render_quiet_at_ms - evidence.render_end_ms
        if render_quiet_ms is not None and render_quiet_ms >= RENDER_ENDPOINT_QUIET_MS:
            return VoiceAssessment(
                VoiceState.ENDED,
                estimated_end_ms=evidence.render_end_ms,
                endpoint_source='render_quiescence')

        if (evidence.vad_end_ms is not None
                and evidence.vad_tail_ms >= VAD_ENDPOINT_SILENCE_MS):
            return VoiceAssessment(
                VoiceState.ENDED,
                estimated_end_ms=evidence.vad_end_ms,
                endpoint_source='asr_vad')

    # A successor token is tied to the current ASR prefix, unlike independent
    # render/VAD observations, so only the current alignment can support it.
    if (evidence.target_boundary_ms is not None
            and _target_terminally_supported(evidence.alignment)):
        return VoiceAssessment(
            VoiceState.ENDED,
            estimated_end_ms=evidence.target_boundary_ms,
            endpoint_source='asr_timestamp')

    if _target_support_advanced(evidence):
        return VoiceAssessment(VoiceState.PLAYING)

    return VoiceAssessment(VoiceState.INCONCLUSIVE)
