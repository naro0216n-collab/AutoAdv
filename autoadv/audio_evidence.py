"""Minimal native-voice evidence assessment.

Capture, ASR/G2P, token timing and VAD remain observations.  Product judgement
keeps their meanings separate: ASR establishes target association and where
that support was observed, while independent temporal/acoustic boundaries can
establish that the associated render interval has ended.  No weighted evidence
score and no ASR full-target gate are used.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import math

from .audio_match import TargetAlignment


class VoiceState(str, Enum):
    ENDED = 'ended'
    PLAYING = 'playing'
    INCONCLUSIVE = 'inconclusive'


@dataclass(frozen=True)
class VoiceEvidence:
    """Raw observations for one update of the active Utterance.

    ``observation_kind`` distinguishes a newly decoded ASR snapshot from a
    transport-only update such as process-render quiescence.  The previous
    alignment/capture boundary are raw history needed to decide whether target
    support actually moved into newly captured PCM; NativeAudio does not reduce
    that comparison to a semantic ``progressed`` flag.
    """
    observation_kind: str
    alignment: TargetAlignment
    previous_alignment: TargetAlignment
    capture_end_ms: int | None
    previous_capture_end_ms: int | None
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


# Attribution tolerance is intentionally small and explicit.  Very short
# targets remain exact for attribution because one edit can erase most of their
# identity; that exactness is not an end-of-speech requirement.
MIN_CORRELATED_PHONEMES = 3
ALIGNMENT_ERROR_DIVISOR = 4

# Acoustic/render boundaries are independent endpoint observations.  They are
# durations, not repeated-observation/stability counters.
VAD_ENDPOINT_SILENCE_MS = 200
RENDER_ENDPOINT_QUIET_MS = 300

# Missing ASR tail units are not treated as absent speech.  A boundary is only
# rejected when the unobserved target suffix could not physically fit between
# the last target-supported token and that boundary.  This bound is deliberately
# permissive; it is a safety contradiction check, not a speech-rate estimator.
MAX_UNOBSERVED_PHONEMES_PER_SECOND = 50.0


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


def _tail_can_fit_before(evidence: VoiceEvidence, endpoint_ms: int | None) -> bool:
    """Return whether ASR-unobserved target tail could precede ``endpoint_ms``.

    This never claims that the suffix was spoken.  It only prevents an endpoint
    from proving completion when the endpoint occurs too soon for the missing
    target tail to have existed at all.
    """
    remaining = _unobserved_tail_units(evidence.alignment)
    if remaining <= 0:
        return True
    if endpoint_ms is None or evidence.target_last_token_ms is None:
        return False
    available_ms = endpoint_ms - evidence.target_last_token_ms
    if available_ms < 0:
        return False
    required_ms = math.ceil(
        remaining * 1000.0 / MAX_UNOBSERVED_PHONEMES_PER_SECOND)
    return available_ms >= required_ms


def _lexical_tail_close_enough(alignment: TargetAlignment) -> bool:
    """A successor ASR token is an endpoint only when alignment is near tail.

    Unlike render/VAD boundaries this evidence is produced by the same ASR
    hypothesis as target association, so it must not excuse a large missing
    suffix.  Reuse the ordinary edit tolerance instead of adding another score.
    """
    target = alignment.phonetic_target_units
    return bool(target > 0 and _unobserved_tail_units(alignment) <= _allowed_edits(target))


def _target_support_advanced(evidence: VoiceEvidence, correlated: bool) -> bool:
    """Did target support enter PCM captured after the previous ASR snapshot?"""
    if evidence.observation_kind != 'asr' or not correlated:
        return False
    if (evidence.target_last_token_ms is None
            or evidence.previous_capture_end_ms is None):
        return False
    return bool(
        evidence.target_last_token_ms > evidence.previous_capture_end_ms
        and evidence.alignment.phonetic_matches
        > evidence.previous_alignment.phonetic_matches
    )


def assess_voice(evidence: VoiceEvidence) -> VoiceAssessment:
    """Map raw observations to ENDED / PLAYING / INCONCLUSIVE.

    ENDED is proved through one of three meaningfully different endpoint paths:
    process render quiescence, a VAD speech boundary, or an ASR successor token.
    None requires ASR to recognize the complete target text.  PLAYING requires
    target support to move into PCM that did not exist at the previous ASR
    snapshot; cumulative re-decode improvements over old PCM do not count.
    """
    correlated = _target_correlated(evidence.alignment)
    if correlated:
        render_quiet_ms = None
        if evidence.render_end_ms is not None and evidence.render_quiet_at_ms is not None:
            render_quiet_ms = evidence.render_quiet_at_ms - evidence.render_end_ms
        if (render_quiet_ms is not None
                and render_quiet_ms >= RENDER_ENDPOINT_QUIET_MS
                and _tail_can_fit_before(evidence, evidence.render_end_ms)):
            return VoiceAssessment(
                VoiceState.ENDED,
                estimated_end_ms=evidence.render_end_ms,
                endpoint_source='render_quiescence')

        if (evidence.vad_end_ms is not None
                and evidence.vad_tail_ms >= VAD_ENDPOINT_SILENCE_MS
                and _tail_can_fit_before(evidence, evidence.vad_end_ms)):
            return VoiceAssessment(
                VoiceState.ENDED,
                estimated_end_ms=evidence.vad_end_ms,
                endpoint_source='asr_vad')

        if (evidence.target_boundary_ms is not None
                and _lexical_tail_close_enough(evidence.alignment)
                and _tail_can_fit_before(evidence, evidence.target_boundary_ms)):
            return VoiceAssessment(
                VoiceState.ENDED,
                estimated_end_ms=evidence.target_boundary_ms,
                endpoint_source='asr_timestamp')

    if _target_support_advanced(evidence, correlated):
        return VoiceAssessment(VoiceState.PLAYING)

    return VoiceAssessment(VoiceState.INCONCLUSIVE)
