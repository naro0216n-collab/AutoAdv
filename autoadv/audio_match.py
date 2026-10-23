"""Pure target alignment and acoustic observations for native audio.

TargetMatcher does not decide whether a voice is playing or ended. It only
normalizes the configured target and an ASR hypothesis, then reports bounded pronunciation alignment measurements. Temporal/acoustic observations
and progression policy live outside this module.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
import unicodedata


def surface_key(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    return ''.join(c for c in text if unicodedata.category(c)[0] in 'LN')


def _align(expected, observed):
    """Target-prefix edit alignment; neither leading side can be skipped free."""
    n, m = len(expected), len(observed)
    if not n or not m:
        return 0, 0, max(n, m)
    row = [(j, 0) for j in range(m + 1)]
    best = (0, 0, 0)  # exact matches, target-prefix length, edit distance
    for i, unit in enumerate(expected, 1):
        next_row = [(i, 0)]
        for j, found in enumerate(observed, 1):
            same = int(unit == found)
            candidates = (
                (row[j][0] + 1, row[j][1]),
                (next_row[-1][0] + 1, next_row[-1][1]),
                (row[j - 1][0] + (not same), row[j - 1][1] + same),
            )
            next_row.append(min(candidates, key=lambda state: (state[0], -state[1])))
        row = next_row
        distance, correct = row[m]
        if (correct, -distance, i) > (best[0], -best[2], best[1]):
            best = (correct, i, distance)
    return best


@dataclass(frozen=True)
class TargetAlignment:
    """Raw G2P alignment counts for one ASR hypothesis and target utterance."""
    phonetic_matches: int = 0
    phonetic_target_units: int = 0
    phonetic_observed_units: int = 0
    phonetic_prefix_units: int = 0
    phonetic_edits: int = 0
    reading_rank: int = 0
    observed_token_count: int = 0

    @property
    def phonetic_coverage(self) -> float:
        return self.phonetic_matches / max(1, self.phonetic_target_units, self.phonetic_observed_units)


class TargetMatcher:
    """Pure bounded G2P alignment for one configured utterance.

    Candidate selection chooses the best *alignment* only. It never decides that
    the selected voice exists, is still playing, or has ended.
    """
    MAX_PHONEMES = 1024
    MAX_TOKEN_PREFIXES = 128

    def __init__(self, target: str, g2p=None):
        if g2p is None:
            from .g2p import default_g2p
            g2p = default_g2p()
        self._g2p = g2p
        self._target_pronunciations = self._g2p.candidates(target, max_candidates=4)

    @staticmethod
    def _alignment_rank(alignment: TargetAlignment) -> tuple:
        """Choose the best prefix edit alignment; primary reading only breaks ties."""
        return (
            alignment.phonetic_matches - alignment.phonetic_edits,
            alignment.phonetic_prefix_units,
            -alignment.phonetic_edits,
            -alignment.reading_rank,
        )

    def _evaluate(self, hypothesis: str) -> TargetAlignment:
        observed_candidates = self._g2p.candidates(hypothesis, max_candidates=1)
        if not self._target_pronunciations or not observed_candidates:
            return TargetAlignment()
        observed_phones = observed_candidates[0].phonemes
        if not observed_phones or len(observed_phones) > self.MAX_PHONEMES:
            return TargetAlignment()

        best = None
        for candidate in self._target_pronunciations:
            target_phones = candidate.phonemes
            if not target_phones or len(target_phones) > self.MAX_PHONEMES:
                continue
            matches, prefix_units, edits = _align(target_phones, observed_phones)
            alignment = TargetAlignment(
                phonetic_matches=matches,
                phonetic_target_units=len(target_phones),
                phonetic_observed_units=len(observed_phones),
                phonetic_prefix_units=prefix_units,
                phonetic_edits=edits,
                reading_rank=candidate.rank,
            )
            rank = self._alignment_rank(alignment)
            if best is None or rank > best[0]:
                best = (rank, alignment)
        return best[1] if best is not None else TargetAlignment()

    @classmethod
    def _prefix_rank(cls, alignment: TargetAlignment, token_count: int) -> tuple:
        # Prefer more target matches, then fewer edit operations.  Equal
        # alignments choose the earliest ASR prefix so trailing background
        # speech is not absorbed into the target observation.
        return (*cls._alignment_rank(alignment), -token_count)

    def align(self, hypothesis: str, *, tokens=()) -> TargetAlignment:
        tokens = tuple(str(t) for t in (tokens or ()))
        if not tokens or surface_key(''.join(tokens)) != surface_key(hypothesis):
            return self._evaluate(hypothesis)
        best = None
        prefix = ''
        for index, token in enumerate(tokens[:self.MAX_TOKEN_PREFIXES], 1):
            prefix += token
            alignment = replace(self._evaluate(prefix), observed_token_count=index)
            rank = self._prefix_rank(alignment, index)
            if best is None or rank > best[0]:
                best = (rank, alignment)
        return best[1] if best is not None else self._evaluate(hypothesis)


@dataclass(frozen=True)
class VADDecision:
    active: bool
    tail_silence_ms: int
    frames_analyzed: bool = True
    contains_speech: bool = False
    mono_peak: float = 0.0
    stereo_peak: float = 0.0


class VoiceActivity:
    """WebRTC VAD over 48kHz mono int16, fixed 20ms frames.

    VAD is acoustic supporting evidence. It neither attributes mixed process
    audio to the current line nor acts as a mandatory endpoint gate.
    """
    def __init__(self, mode: int = 2):
        import webrtcvad
        self.vad = webrtcvad.Vad(mode)
        self.tail_ms = 0
        self._pending = bytearray()

    def reset(self) -> None:
        self.tail_ms = 0
        self._pending.clear()

    def observe_float32_stereo(self, frames: bytes) -> VADDecision:
        import numpy as np
        if not frames or len(frames) % 8:
            raise ValueError('invalid float32 stereo PCM')
        stereo = np.frombuffer(frames, dtype='<f4').reshape((-1, 2))
        mono = np.clip(stereo.mean(axis=1), -1, 1)
        mono_peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        stereo_peak = float(np.max(np.abs(stereo))) if stereo.size else 0.0
        pcm = (mono * 32767).astype('<i2').tobytes()
        unit = 48000 * 20 // 1000 * 2
        self._pending.extend(pcm)
        voiced = False
        latest_active = False
        analyzed = False
        while len(self._pending) >= unit:
            analyzed = True
            segment = bytes(self._pending[:unit])
            del self._pending[:unit]
            speech = self.vad.is_speech(segment, 48000)
            voiced = voiced or speech
            latest_active = speech
            self.tail_ms = 0 if speech else self.tail_ms + 20
        return VADDecision(latest_active, self.tail_ms, analyzed, voiced, mono_peak, stereo_peak)
