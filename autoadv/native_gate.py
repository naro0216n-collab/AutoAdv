"""Model-independent evidence aggregator for native-voice onset and offset.

An AudioFrame/VAD decision alone is never an onset attributed to the current
line. ASR coverage alone and trailing silence alone are never a verified end.
Real audio backends must feed this gate with evidence tied to the current line.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from .contracts import Envelope, EventKind, Evidence


@dataclass(frozen=True)
class MatchPolicy:
    onset_score: float = .85
    coverage: float = .90
    tail_confidence: float = .90
    min_tail_silence_ms: int = 300

    def __post_init__(self):
        if not all(0 < x <= 1 for x in (self.onset_score, self.coverage, self.tail_confidence)):
            raise ValueError("scores must be in (0,1]")
        if self.min_tail_silence_ms <= 0:
            raise ValueError("positive verified tail silence required")


@dataclass(frozen=True)
class AudioEvidence:
    session_id: str
    line_epoch: int
    cancel_token: int
    at_ms: int
    utterance_id: str
    capture_ok: bool
    # Current-line assignment, obtained from voice metadata or target-matched ASR.
    source_line_matches: bool
    target_score: float
    vad_speech: bool
    vad_tail_silence_ms: int
    asr_coverage: float
    asr_tail_confidence: float
    asr_order_consistent: bool
    metadata_voice_id_matches: bool = False


class NativeVoiceGate:
    def __init__(self, policy: MatchPolicy | None = None):
        self.policy = policy or MatchPolicy()
        self.key: Optional[tuple[str, int, int]] = None
        self.utterance_id: Optional[str] = None
        self.active = False
        self.complete = False
        self.last_ms = -1

    def begin_line(self, session_id: str, line_epoch: int, cancel_token: int) -> None:
        self.key = (session_id, line_epoch, cancel_token)
        self.utterance_id = None
        self.active = False
        self.complete = False
        self.last_ms = -1

    def observe(self, ev: AudioEvidence) -> list[Envelope]:
        if (self.key != (ev.session_id, ev.line_epoch, ev.cancel_token) or
            ev.at_ms < self.last_ms or not ev.capture_ok or not ev.utterance_id or
            not ev.source_line_matches or self.complete):
            return []
        self.last_ms = ev.at_ms
        result: list[Envelope] = []
        attributed = ev.metadata_voice_id_matches or ev.target_score >= self.policy.onset_score
        if not self.active:
            if not ev.vad_speech or not attributed:
                return []
            self.active = True
            self.utterance_id = ev.utterance_id
            result.append(Envelope(EventKind.NATIVE_STARTED, ev.session_id, ev.at_ms,
                                   line_epoch=ev.line_epoch, cancel_token=ev.cancel_token,
                                   evidence=Evidence.METADATA if ev.metadata_voice_id_matches
                                   else Evidence.AUDIO_CONFIRMED,
                                   native_utterance_id=ev.utterance_id))
        if ev.utterance_id != self.utterance_id:
            return result
        if (ev.vad_tail_silence_ms >= self.policy.min_tail_silence_ms and
            ev.asr_order_consistent and ev.asr_coverage >= self.policy.coverage and
            ev.asr_tail_confidence >= self.policy.tail_confidence):
            self.complete = True
            result.append(Envelope(EventKind.NATIVE_COMPLETED, ev.session_id, ev.at_ms,
                                   line_epoch=ev.line_epoch, cancel_token=ev.cancel_token,
                                   evidence=Evidence.AUDIO_CONFIRMED,
                                   native_utterance_id=ev.utterance_id))
        return result