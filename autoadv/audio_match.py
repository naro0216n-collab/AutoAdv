"""Incremental target-text/ASR matching; audio *evidence*, never input rights.

Repetitions, backward progress and unstable recognizer tails are explicit
blockers. Hypotheses for BGM/SE must not be converted into verified speech.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
import unicodedata


def phonetic_key(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    return ''.join(c for c in text if unicodedata.category(c)[0] in 'LN')


@dataclass(frozen=True)
class ASRProgress:
    coverage: float
    onset_score: float
    tail_confidence: float
    monotonic: bool
    matched_chars: int
    observed: str


class TargetMatcher:
    """Require monotonic prefix coverage, not arbitrary subsequence matching.

    A recognizer is allowed to emit partial text, but reversing the recognized
    prefix invalidates the current utterance rather than triggering a fast end.
    """
    def __init__(self, target: str):
        self.target = phonetic_key(target)
        self._last = ''
        self._previous = ''
        self._monotonic = True
        self._observations = 0

    def observe(self, hypothesis: str) -> ASRProgress:
        key = phonetic_key(hypothesis)
        self._observations += 1
        # Count exact prefix: arbitrary LCS over a long repeated line gives
        # deceptively high coverage even when the source utterance is unrelated.
        shared = 0
        for a, b in zip(self.target, key):
            if a != b:
                break
            shared += 1
        if self._last and (len(key) < len(self._last) or not key.startswith(self._last[:max(1, len(self._last) - 1)])):
            self._monotonic = False
        stable_tail = bool(self._previous and key == self._previous and key == self.target)
        # An extremely short hypothesis never confirms onset by acoustic matching.
        onset = (shared / max(1, min(len(self.target), len(key)))) if shared >= 2 else 0.0
        result = ASRProgress(shared / max(1, len(self.target)), onset,
                             1.0 if stable_tail and self._monotonic else 0.0,
                             self._monotonic, shared, key)
        self._previous, self._last = key, key
        return result


@dataclass(frozen=True)
class VADDecision:
    active: bool
    tail_silence_ms: int


class VoiceActivity:
    """WebRTC VAD over 48kHz mono int16, fixed 20ms frames.

    A VAD positive alone never attributes sound to the current line; a VAD
    negative alone never verifies end or absence.
    """
    def __init__(self, mode: int = 2):
        import webrtcvad  # optional module
        self.vad = webrtcvad.Vad(mode)
        self.tail_ms = 0
        self._pending = bytearray()

    def observe_float32_stereo(self, frames: bytes) -> VADDecision:
        import numpy as np  # optional module
        if not frames or len(frames) % 8:
            raise ValueError('invalid float32 stereo PCM')
        stereo = np.frombuffer(frames, dtype='<f4').reshape((-1, 2))
        mono = np.clip(stereo.mean(axis=1), -1, 1)
        pcm = (mono * 32767).astype('<i2').tobytes()
        unit = 48000 * 20 // 1000 * 2
        self._pending.extend(pcm)
        voiced = False
        while len(self._pending) >= unit:
            segment = bytes(self._pending[:unit])
            del self._pending[:unit]
            speech = self.vad.is_speech(segment, 48000)
            voiced = voiced or speech
            self.tail_ms = 0 if speech else self.tail_ms + 20
        return VADDecision(voiced, self.tail_ms)
