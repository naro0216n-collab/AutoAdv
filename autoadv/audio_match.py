"""Bounded lexical/pronunciation cues for associating ASR with an utterance.

Recognition text is not an end-of-speech signal. Target attribution and target
completion are scored here; timing/acoustic evidence and session ownership stay
with NativeAudio, while game-input authority stays with Controller.
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
class ASRProgress:
    coverage: float
    onset_score: float
    tail_confidence: float
    monotonic: bool
    matched_chars: int
    observed: str
    edit_count: int = 0
    length_ratio: float = 0.0
    terminal_candidate: bool = False
    phonetic_coverage: float = 0.0
    matched_phonemes: int = 0
    phonetic_edit_count: int = 0
    attribution_score: float = 0.0
    completion_score: float = 0.0
    contradiction_score: float = 0.0
    reading_rank: int = 0
    target_units: int = 0
    observed_token_count: int = 0


class TargetMatcher:
    """Align ASR using surface spelling and Japanese pronunciation evidence.

    Surface spelling remains useful when ASR preserves orthography. A swappable
    G2P backend makes kanji/hiragana/katakana variants comparable. Bounded
    dictionary reading alternatives may rescue an on/kun/context mismatch, but
    arbitrary readings and unrestricted substring/LCS matching are not allowed.

    If ASR tokenization is available, prefixes are evaluated so speech after a
    completed target can be treated as later/background material. Leading
    unrelated speech is still charged as edit/contradiction evidence.
    """
    MAX_CHARS = 512
    MAX_PHONEMES = 1024
    MAX_TOKEN_PREFIXES = 128
    TERMINAL_LOOKAHEAD_TOKENS = 4

    def __init__(self, target: str, g2p=_DEFAULT_G2P):
        from .g2p import default_g2p
        self.target = surface_key(target)
        self._g2p = default_g2p() if g2p is _DEFAULT_G2P else g2p
        self._target_pronunciations = ()
        if self._g2p is not None:
            self._target_pronunciations = self._g2p.candidates(target, max_candidates=4)
        self._previous_signature = None
        self._previous_coverage = 0.0

    def _evaluate(self, hypothesis: str) -> ASRProgress:
        key = surface_key(hypothesis)
        n, m = len(self.target), len(key)
        if not n or not m or n > self.MAX_CHARS or m > self.MAX_CHARS:
            return ASRProgress(0, 0, 0, False, 0, key)

        matched, prefix_len, errors = _align(self.target, key)
        surface_coverage = matched / max(n, m)
        surface_onset = matched / max(1, prefix_len, m)
        surface_length_ratio = m / n

        best_phone = None
        if self._g2p is not None and self._target_pronunciations:
            observed = self._g2p.candidates(hypothesis, max_candidates=1)
            if observed:
                observed_phones = observed[0].phonemes
                if 0 < len(observed_phones) <= self.MAX_PHONEMES:
                    for candidate in self._target_pronunciations:
                        target_phones = candidate.phonemes
                        if not target_phones or len(target_phones) > self.MAX_PHONEMES:
                            continue
                        pm, pprefix, perrors = _align(target_phones, observed_phones)
                        pn, po = len(target_phones), len(observed_phones)
                        pcoverage = pm / max(pn, po)
                        ponset = pm / max(1, pprefix, po)
                        perror_ratio = perrors / max(1, pprefix, po)
                        rank_penalty = min(.08, .025 * candidate.rank)
                        completion = max(0.0, min(1.0,
                            .92 * pcoverage + .08 * surface_coverage
                            - .22 * perror_ratio - rank_penalty))
                        contradiction = max(0.0, min(1.0,
                            .65 * perror_ratio + .35 * max(0.0, po / pn - 1.15)))
                        score = completion - .20 * contradiction
                        item = (score, pcoverage, ponset, pm, pprefix, perrors,
                                po / pn, completion, contradiction, candidate.rank, pn)
                        if best_phone is None or item[0] > best_phone[0]:
                            best_phone = item

        if best_phone is None:
            phonetic_coverage = surface_coverage
            phonetic_onset = surface_onset
            matched_phonemes = matched
            phonetic_errors = errors
            phonetic_ratio = surface_length_ratio
            completion_score = max(0.0, min(1.0,
                surface_coverage - .20 * (errors / max(1, prefix_len, m))))
            contradiction_score = min(1.0, errors / max(1, prefix_len, m))
            reading_rank = 0
            phone_prefix = prefix_len
            phone_target_len = n
        else:
            (_, phonetic_coverage, phonetic_onset, matched_phonemes, phone_prefix,
             phonetic_errors, phonetic_ratio, completion_score,
             contradiction_score, reading_rank, phone_target_len) = best_phone

        attribution_score = max(
            surface_onset,
            phonetic_onset * .88 + surface_onset * .12 - .08 * contradiction_score)

        trailing_allowance = max(1, phone_target_len // 10)
        terminal = (
            len(self.target) >= 3
            and matched_phonemes >= max(
                3, phone_target_len - trailing_allowance - max(1, phone_target_len // 8))
            and phone_prefix >= phone_target_len - trailing_allowance
            and .72 <= phonetic_ratio <= 1.28
            and phonetic_errors <= max(2, phone_target_len // 6)
            and completion_score >= .78
            and contradiction_score <= .35
        )
        return ASRProgress(
            surface_coverage, surface_onset, 0.0, False,
            matched, key, errors, surface_length_ratio, terminal,
            phonetic_coverage, matched_phonemes, phonetic_errors,
            attribution_score, completion_score, contradiction_score,
            reading_rank, phone_target_len, 0)

    @staticmethod
    def _rank(progress: ASRProgress, token_count: int):
        # Completion is the desired endpoint; on ties choose the earliest ASR
        # prefix so trailing background speech is not absorbed into the target.
        return (
            int(progress.terminal_candidate),
            progress.completion_score - .18 * progress.contradiction_score,
            progress.attribution_score,
            progress.phonetic_coverage,
            progress.matched_phonemes,
            -progress.phonetic_edit_count,
            -token_count,
        )

    def _select_token_prefix(self, hypothesis: str, tokens) -> ASRProgress:
        tokens = tuple(str(t) for t in (tokens or ()))
        if not tokens or surface_key(''.join(tokens)) != surface_key(hypothesis):
            return self._evaluate(hypothesis)
        best = None
        first_terminal = None
        prefix = ''
        for index, token in enumerate(tokens[:self.MAX_TOKEN_PREFIXES], 1):
            prefix += token
            progress = replace(self._evaluate(prefix), observed_token_count=index)
            if best is None or self._rank(progress, index) > self._rank(best, best.observed_token_count):
                best = progress
            if progress.terminal_candidate and first_terminal is None:
                first_terminal = index
            if first_terminal is not None and index >= first_terminal + self.TERMINAL_LOOKAHEAD_TOKENS:
                break
        return best if best is not None else self._evaluate(hypothesis)

    def observe(self, hypothesis: str, *, tokens=()) -> ASRProgress:
        progress = self._select_token_prefix(hypothesis, tokens)
        signature = (
            round(progress.phonetic_coverage, 4), progress.matched_phonemes,
            progress.phonetic_edit_count, progress.reading_rank,
            progress.observed_token_count, progress.observed,
        )
        stable = progress.terminal_candidate and self._previous_signature == signature
        monotonic = progress.phonetic_coverage >= self._previous_coverage - .15
        self._previous_signature = signature
        self._previous_coverage = progress.phonetic_coverage
        return replace(progress, tail_confidence=float(stable), monotonic=monotonic)


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
