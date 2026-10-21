"""Bounded lexical cues for associating ASR with the current utterance.

Recognition text is *not* an end-of-speech signal. Timing/voice activity and
session ownership remain with NativeAudio; game-input authority remains with
Controller.
"""
from __future__ import annotations
from dataclasses import dataclass
import unicodedata


def phonetic_key(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    return ''.join(c for c in text if unicodedata.category(c)[0] in 'LN')


@dataclass(frozen=True)
class ASRProgress:
    coverage: float  # aligned characters / longer text (penalizes foreign ASR speech)
    onset_score: float  # estimated agreement with observed fragment
    tail_confidence: float  # repeated terminal *lexical* hypothesis, not voice end
    monotonic: bool
    matched_chars: int
    observed: str
    edit_count: int = 0
    length_ratio: float = 0.0
    terminal_candidate: bool = False


class TargetMatcher:
    """Align a whole ASR hypothesis to a *prefix* of displayed speech text.

    Unlike prefix-exact comparison, one substitution/deletion near the start
    does not erase all subsequent evidence. Unlike unrestricted substring/LCS
    scoring, words buried in unrelated preceding voice do not count as a match.
    This is a bounded cue, not a phonetic reading model or a speaker identifier.
    """
    MAX_CHARS = 512  # reject pathologically long input instead of quadratic work

    def __init__(self, target: str):
        self.target = phonetic_key(target)
        self._previous = ''
        self._previous_coverage = 0.0

    def observe(self, hypothesis: str) -> ASRProgress:
        key = phonetic_key(hypothesis)
        n, m = len(self.target), len(key)
        if not n or not m or n > self.MAX_CHARS or m > self.MAX_CHARS:
            self._previous, self._previous_coverage = key, 0.0
            return ASRProgress(0, 0, 0, False, 0, key)

        # Levenshtein target-prefix versus the complete ASR hypothesis. Target
        # prefixes accommodate streaming recognition; an ASR prefix/suffix from
        # other voice must still incur edit cost and cannot match for free.
        # Carry the number of exact aligned characters as well as edit cost.
        # Substitutions, insertions and deletions each cost one; on equal edit
        # cost the alignment with the most unchanged characters wins.
        row = [(j, 0) for j in range(m + 1)]
        best = (0, 0, 0)  # matched characters, target prefix length, edit count
        for i, ch in enumerate(self.target, 1):
            next_row = [(i, 0)]
            for j, found in enumerate(key, 1):
                matched_char = int(ch == found)
                candidates = (
                    (row[j][0] + 1, row[j][1]),
                    (next_row[-1][0] + 1, next_row[-1][1]),
                    (row[j - 1][0] + (not matched_char), row[j - 1][1] + matched_char),
                )
                next_row.append(min(candidates, key=lambda state: (state[0], -state[1])))
            row = next_row
            distance, correct = row[m]
            # Favor the best-supported target prefix without allowing free
            # skipping at either end of the ASR hypothesis.
            if (correct, -distance, i) > (best[0], -best[2], best[1]):
                best = (correct, i, distance)
        matched, prefix_len, errors = best
        coverage = matched / max(n, m)
        onset = matched / max(1, prefix_len, m)
        length_ratio = m / n

        # Repeated identical hypotheses are a lexical stability cue only. A
        # short stable prefix must never masquerade as the tail of a long line.
        # One missing character in a short line is permissible, while longer
        # lines require nearly all their text to have been observed.
        trailing_allowance = max(1, n // 10)
        terminal = (matched >= max(3, n - trailing_allowance - max(1, n // 8))
                    and prefix_len >= n - trailing_allowance
                    and m >= n - trailing_allowance
                    and errors <= max(1, n // 8)
                    and coverage >= .80)
        stable = terminal and self._previous == key
        monotonic = (coverage >= self._previous_coverage - .15)
        self._previous, self._previous_coverage = key, coverage
        return ASRProgress(coverage, onset, float(stable), monotonic,
                           matched, key, errors, length_ratio, terminal)


@dataclass(frozen=True)
class VADDecision:
    active: bool  # most recently analyzed frame, not any frame in the packet
    tail_silence_ms: int
    frames_analyzed: bool = True
    contains_speech: bool = False  # earlier speech in this packet, possibly now quiet
    mono_peak: float = 0.0  # observed VAD input amplitude; never completion evidence
    stereo_peak: float = 0.0  # original capture amplitude before channel averaging


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

    def reset(self) -> None:
        self.tail_ms = 0
        self._pending.clear()

    def observe_float32_stereo(self, frames: bytes) -> VADDecision:
        import numpy as np  # optional module
        if not frames or len(frames) % 8:
            raise ValueError('invalid float32 stereo PCM')
        stereo = np.frombuffer(frames, dtype='<f4').reshape((-1, 2))
        mono = np.clip(stereo.mean(axis=1), -1, 1)
        # This already-decoded PCM input is the exact signal seen by VAD.
        # A low peak explains a missing VAD positive without inferring that
        # a foreground voice or a game sound was absent.
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
