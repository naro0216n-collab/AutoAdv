"""Minimal native-voice evidence assessment.

ASR/G2P/VAD stay as observations.  Product judgement uses only four facts:
whether the target pronunciation is correlated, whether its tail is reached,
whether a direct endpoint was observed, and whether target-correlated audio
advanced in newly captured PCM.  No weighted evidence score or freshness
window is used.
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
    """Raw observations for one ASR snapshot of the active Utterance."""
    alignment: TargetAlignment
    target_last_token_ms: int | None
    target_boundary_ms: int | None
    target_progressed: bool
    vad_onset_ms: int | None
    vad_tail_ms: int
    vad_end_ms: int | None


@dataclass(frozen=True)
class VoiceAssessment:
    state: VoiceState
    estimated_end_ms: int | None = None
    endpoint_source: str = ''


# Keep only the minimum attribution/tolerance and acoustic endpoint controls.
# The lexical tolerance is deliberately expressed against edit counts rather
# than several derived confidence scores.  Short targets therefore require an
# exact match; longer utterances can absorb ordinary ASR/G2P errors.
MIN_CORRELATED_PHONEMES = 3
ALIGNMENT_ERROR_DIVISOR = 4
VAD_ENDPOINT_SILENCE_MS = 200


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


def _target_complete(alignment: TargetAlignment, correlated: bool) -> bool:
    return bool(
        correlated
        and alignment.phonetic_prefix_units == alignment.phonetic_target_units
    )


def assess_voice(evidence: VoiceEvidence) -> VoiceAssessment:
    """Map one observation snapshot to ENDED / PLAYING / INCONCLUSIVE.

    ENDED requires target completion plus a direct endpoint observation: either
    a following ASR token that bounds the selected target prefix, or a real VAD
    silence after that completed target.  PLAYING requires target correlation
    plus target progress in newly captured PCM.  Everything else is explicitly
    inconclusive and is left to Controller's text fallback policy.
    """
    correlated = _target_correlated(evidence.alignment)
    complete = _target_complete(evidence.alignment, correlated)

    if complete and evidence.target_boundary_ms is not None:
        return VoiceAssessment(
            VoiceState.ENDED, estimated_end_ms=evidence.target_boundary_ms,
            endpoint_source='asr_timestamp')

    if (complete and evidence.vad_end_ms is not None
            and evidence.vad_tail_ms >= VAD_ENDPOINT_SILENCE_MS):
        return VoiceAssessment(
            VoiceState.ENDED, estimated_end_ms=evidence.vad_end_ms,
            endpoint_source='asr_vad')

    if correlated and evidence.target_progressed:
        return VoiceAssessment(VoiceState.PLAYING)

    return VoiceAssessment(VoiceState.INCONCLUSIVE)
