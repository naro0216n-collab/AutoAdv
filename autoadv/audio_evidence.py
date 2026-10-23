"""Pure evidence model for native game-voice assessment.

Capture, ASR, G2P and VAD produce observations. This module is the only place
that assigns those observations the semantic state ENDED / PLAYING /
INCONCLUSIVE. It performs no capture, scheduling or game input.
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
    """One immutable observation snapshot for the current Utterance."""
    alignment: TargetAlignment
    audio_ms: float
    capture_end_ms: int | None
    observation_ms: int | None
    target_last_token_ms: int | None
    target_boundary_ms: int | None
    post_target_pcm_ms: float
    vad_active: bool
    vad_contains_speech: bool
    vad_onset_ms: int | None
    vad_tail_ms: int
    vad_end_ms: int | None
    stable_alignment_observations: int
    phonetic_progress_delta: float | None

    @property
    def target_last_token_age_ms(self) -> float | None:
        if self.observation_ms is None or self.target_last_token_ms is None:
            return None
        return max(0.0, float(self.observation_ms - self.target_last_token_ms))


@dataclass(frozen=True)
class VoiceAssessmentPolicy:
    """Central tuning surface for converting evidence into semantic state."""
    association_min: float = .72
    progress_min: float = .55
    endpoint_min: float = .45
    completion_evidence_min: float = .60
    target_tail_completion_min: float = .78
    target_tail_contradiction_max: float = .35
    target_length_ratio_min: float = .72
    target_length_ratio_max: float = 1.28
    post_target_full_ms: float = 320.0
    vad_full_tail_ms: float = 400.0
    stability_endpoint_weight: float = .35
    playing_target_tail_ms: float = 700.0
    progress_regression_tolerance: float = .15


DEFAULT_VOICE_POLICY = VoiceAssessmentPolicy()


@dataclass(frozen=True)
class VoiceAssessment:
    state: VoiceState
    association_score: float
    completion_score: float
    contradiction_score: float
    endpoint_score: float
    timestamp_endpoint_score: float
    vad_endpoint_score: float
    stability_endpoint_score: float
    length_score: float
    evidence_score: float
    target_tail_reached: bool
    sufficient_progress: bool
    target_last_token_age_ms: float | None
    estimated_end_ms: int | None = None
    endpoint_source: str = ''


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _alignment_scores(alignment: TargetAlignment) -> tuple[float, float, float]:
    surface_error_ratio = alignment.surface_edits / max(
        1, alignment.surface_prefix_units, alignment.surface_observed_units)
    phonetic_error_ratio = alignment.phonetic_edits / max(
        1, alignment.phonetic_prefix_units, alignment.phonetic_observed_units)
    surface_onset = alignment.onset_score
    phonetic_onset = alignment.phonetic_onset_score
    completion = _clamp(
        .92 * alignment.phonetic_coverage
        + .08 * alignment.coverage
        - .22 * phonetic_error_ratio
        - min(.08, .025 * alignment.reading_rank))
    contradiction = _clamp(
        .65 * phonetic_error_ratio
        + .35 * max(0.0, alignment.pronunciation_length_ratio - 1.15))
    association = _clamp(max(
        surface_onset,
        phonetic_onset * .88 + surface_onset * .12 - .08 * contradiction))
    # Surface errors remain part of the raw alignment and are intentionally not
    # converted into an additional hidden gate here.
    _ = surface_error_ratio
    return association, completion, contradiction


def _target_tail_reached(alignment: TargetAlignment, completion: float,
                         contradiction: float, policy: VoiceAssessmentPolicy) -> bool:
    target = alignment.phonetic_target_units
    if alignment.surface_target_units < 3 or target <= 0:
        return False
    trailing_allowance = max(1, target // 10)
    return bool(
        alignment.phonetic_matches >= max(
            3, target - trailing_allowance - max(1, target // 8))
        and alignment.phonetic_prefix_units >= target - trailing_allowance
        and policy.target_length_ratio_min <= alignment.pronunciation_length_ratio <= policy.target_length_ratio_max
        and alignment.phonetic_edits <= max(2, target // 6)
        and completion >= policy.target_tail_completion_min
        and contradiction <= policy.target_tail_contradiction_max
    )


def _length_evidence(alignment: TargetAlignment) -> float:
    ratios = (alignment.length_ratio, alignment.pronunciation_length_ratio)
    return max((_clamp(1.0 - abs(ratio - 1.0)) for ratio in ratios if ratio > 0), default=0.0)


def assess_voice(evidence: VoiceEvidence,
                 policy: VoiceAssessmentPolicy = DEFAULT_VOICE_POLICY) -> VoiceAssessment:
    """Assign exactly one semantic voice state to an evidence snapshot."""
    a = evidence.alignment
    association, completion, contradiction = _alignment_scores(a)
    target_tail_reached = _target_tail_reached(a, completion, contradiction, policy)

    timestamp_score = 0.0
    if target_tail_reached and evidence.target_boundary_ms is not None:
        timestamp_score = _clamp(evidence.post_target_pcm_ms / policy.post_target_full_ms)
    vad_score = _clamp(evidence.vad_tail_ms / policy.vad_full_tail_ms)
    vad_endpoint = .75 * vad_score
    stability_endpoint = (policy.stability_endpoint_weight
                          if target_tail_reached and evidence.stable_alignment_observations >= 2
                          else 0.0)
    endpoint_score = 1.0 - ((1.0 - timestamp_score)
                            * (1.0 - vad_endpoint)
                            * (1.0 - stability_endpoint))
    endpoint_score = _clamp(endpoint_score)
    length_score = _length_evidence(a)
    evidence_score = _clamp(
        .50 * completion
        + .18 * association
        + .16 * endpoint_score
        + .10 * length_score
        + .06 * float(evidence.phonetic_progress_delta is None
                       or evidence.phonetic_progress_delta >= -policy.progress_regression_tolerance)
        - .14 * contradiction)
    sufficient_progress = bool(
        max(a.phonetic_coverage, a.coverage) >= policy.progress_min
        and a.phonetic_matches >= min(4, max(1, a.phonetic_target_units)))

    ended = bool(
        sufficient_progress
        and endpoint_score >= policy.endpoint_min
        and evidence_score >= policy.completion_evidence_min)

    estimated_end_ms = None
    endpoint_source = ''
    if ended:
        if (evidence.target_boundary_ms is not None and timestamp_score > 0
                and (evidence.vad_end_ms is None or timestamp_score >= vad_endpoint)):
            estimated_end_ms = evidence.target_boundary_ms
            endpoint_source = 'asr_timestamp'
        elif evidence.vad_end_ms is not None:
            estimated_end_ms = evidence.vad_end_ms
            endpoint_source = 'asr_vad'
        elif evidence.target_boundary_ms is not None and timestamp_score > 0:
            estimated_end_ms = evidence.target_boundary_ms
            endpoint_source = 'asr_timestamp'
        return VoiceAssessment(
            VoiceState.ENDED, association, completion, contradiction,
            endpoint_score, timestamp_score, vad_endpoint, stability_endpoint,
            length_score, evidence_score, target_tail_reached, sufficient_progress,
            evidence.target_last_token_age_ms, estimated_end_ms, endpoint_source)

    age = evidence.target_last_token_age_ms
    min_playing_units = min(3, max(1, a.phonetic_target_units))
    playing = bool(
        association >= policy.association_min
        and a.phonetic_matches >= min_playing_units
        and age is not None
        and age <= policy.playing_target_tail_ms)
    state = VoiceState.PLAYING if playing else VoiceState.INCONCLUSIVE
    return VoiceAssessment(
        state, association, completion, contradiction,
        endpoint_score, timestamp_score, vad_endpoint, stability_endpoint,
        length_score, evidence_score, target_tail_reached, sufficient_progress,
        age, None, '')


class AlignmentObservationHistory:
    """Stateful observation history; it never assigns voice semantics."""
    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self._signature = None
        self._coverage: float | None = None
        self._stable = 0

    def observe(self, alignment: TargetAlignment) -> tuple[int, float | None]:
        if alignment.signature == self._signature:
            self._stable += 1
        else:
            self._stable = 1
        previous = self._coverage
        delta = (alignment.phonetic_coverage - previous
                 if previous is not None else None)
        self._signature = alignment.signature
        self._coverage = alignment.phonetic_coverage
        return self._stable, delta
