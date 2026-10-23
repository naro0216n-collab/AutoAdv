"""Pure target alignment and acoustic observations for native audio.

TargetMatcher does not decide whether a voice is playing or ended. It only
normalizes the configured target and an ASR hypothesis, then reports bounded
surface/pronunciation alignment measurements. Temporal/acoustic observations
and progression policy live outside this module.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
import unicodedata


_DEFAULT_G2P = object()


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
    """Measurements from aligning one ASR hypothesis to one target utterance.

    All fields are observations or edit-alignment counts. There is deliberately
    no ``terminal``, ``playing`` or ``completed`` flag here. Those meanings are
    assigned exactly once by the voice-evidence assessment model.
    """
    observed: str = ''
    surface_matches: int = 0
    surface_target_units: int = 0
    surface_observed_units: int = 0
    surface_prefix_units: int = 0
    surface_edits: int = 0
    phonetic_matches: int = 0
    phonetic_target_units: int = 0
    phonetic_observed_units: int = 0
    phonetic_prefix_units: int = 0
    phonetic_edits: int = 0
    reading_rank: int = 0
    observed_token_count: int = 0

    @property
    def coverage(self) -> float:
        return self.surface_matches / max(1, self.surface_target_units, self.surface_observed_units)

    @property
    def matched_chars(self) -> int:
        return self.surface_matches

    @property
    def edit_count(self) -> int:
        return self.surface_edits

    @property
    def phonetic_coverage(self) -> float:
        return self.phonetic_matches / max(1, self.phonetic_target_units, self.phonetic_observed_units)

    @property
    def matched_phonemes(self) -> int:
        return self.phonetic_matches

    @property
    def phonetic_edit_count(self) -> int:
        return self.phonetic_edits


class TargetMatcher:
    """Pure bounded surface/G2P alignment for one configured utterance.

    Candidate selection chooses the best *alignment* only. It never decides that
    the selected voice exists, is still playing, or has ended.
    """
    MAX_CHARS = 512
    MAX_PHONEMES = 1024
    MAX_TOKEN_PREFIXES = 128

    def __init__(self, target: str, g2p=_DEFAULT_G2P):
        from .g2p import default_g2p
        self.target = surface_key(target)
        self._g2p = default_g2p() if g2p is _DEFAULT_G2P else g2p
        self._target_pronunciations = ()
        if self._g2p is not None:
            self._target_pronunciations = self._g2p.candidates(target, max_candidates=4)

    @staticmethod
    def _alignment_rank(alignment: TargetAlignment) -> tuple:
        """Choose the strongest raw alignment without weighted confidence math."""
        return (
            alignment.phonetic_matches - alignment.phonetic_edits,
            alignment.phonetic_matches,
            -alignment.phonetic_edits,
            alignment.phonetic_prefix_units,
            -alignment.reading_rank,
        )

    def _evaluate(self, hypothesis: str) -> TargetAlignment:
        key = surface_key(hypothesis)
        n, m = len(self.target), len(key)
        if not n or not m or n > self.MAX_CHARS or m > self.MAX_CHARS:
            return TargetAlignment(observed=key, surface_target_units=n, surface_observed_units=m)

        matched, prefix_len, errors = _align(self.target, key)
        best_phone = None
        if self._g2p is not None and self._target_pronunciations:
            observed_candidates = self._g2p.candidates(hypothesis, max_candidates=1)
            if observed_candidates:
                observed_phones = observed_candidates[0].phonemes
                if 0 < len(observed_phones) <= self.MAX_PHONEMES:
                    for candidate in self._target_pronunciations:
                        target_phones = candidate.phonemes
                        if not target_phones or len(target_phones) > self.MAX_PHONEMES:
                            continue
                        pm, pprefix, perrors = _align(target_phones, observed_phones)
                        item = TargetAlignment(
                            observed=key,
                            surface_matches=matched,
                            surface_target_units=n,
                            surface_observed_units=m,
                            surface_prefix_units=prefix_len,
                            surface_edits=errors,
                            phonetic_matches=pm,
                            phonetic_target_units=len(target_phones),
                            phonetic_observed_units=len(observed_phones),
                            phonetic_prefix_units=pprefix,
                            phonetic_edits=perrors,
                            reading_rank=candidate.rank,
                        )
                        rank = self._alignment_rank(item)
                        if best_phone is None or rank > best_phone[0]:
                            best_phone = (rank, item)

        if best_phone is not None:
            return best_phone[1]
        # Surface-only operation remains an explicit test/investigation fallback.
        return TargetAlignment(
            observed=key,
            surface_matches=matched,
            surface_target_units=n,
            surface_observed_units=m,
            surface_prefix_units=prefix_len,
            surface_edits=errors,
            phonetic_matches=matched,
            phonetic_target_units=n,
            phonetic_observed_units=m,
            phonetic_prefix_units=prefix_len,
            phonetic_edits=errors,
        )

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
