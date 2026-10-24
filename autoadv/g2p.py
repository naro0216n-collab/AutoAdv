"""Swappable Japanese grapheme-to-phoneme adapters for target audio alignment."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from threading import RLock
from typing import Protocol
import unicodedata


@dataclass(frozen=True)
class PronunciationCandidate:
    phonemes: tuple[str, ...]
    rank: int = 0


class G2PBackend(Protocol):
    def candidates(self, text: str, *, max_candidates: int = 4) -> tuple[PronunciationCandidate, ...]: ...


class HaqumeiG2P:
    """Haqumei-backed G2P with bounded dictionary reading alternatives.

    ``g2p_candidates`` is intended for downstream forced-alignment use. Its
    candidate ``delta`` is a morphological lattice cost rather than a reading
    probability, so AutoAdv uses only documented candidate ordering: the first
    candidate is the context-selected primary reading and later candidates are
    weaker alternatives. Arbitrary on/kun expansion is never synthesized.
    """

    def __init__(self, engine=None):
        if engine is None:
            from haqumei import Haqumei
            engine = Haqumei()
        self.engine = engine
        # Target construction and acoustic alignment can briefly overlap across an
        # utterance hand-off. Do not assume the native extension is re-entrant.
        self._lock = RLock()

    @staticmethod
    def _clean_word(word) -> tuple[str, ...]:
        # Punctuation/space pauses are not target phones. Unknown words may
        # still carry a usable Haqumei pronunciation; only a literal ``unk``
        # remains text-specific so the acoustic adapter can reject it rather
        # than silently mapping unrelated text to one generic phone.
        phonemes = tuple(str(p) for p in word.phonemes if str(p) not in {'pau', 'sp', ''})
        if phonemes == ('unk',):
            surface = unicodedata.normalize('NFKC', str(getattr(word, 'word', ''))).casefold()
            return (f'unk:{surface}',) if surface else ()
        return tuple(p for p in phonemes if p != 'unk')

    def candidates(self, text: str, *, max_candidates: int = 4) -> tuple[PronunciationCandidate, ...]:
        normalized = unicodedata.normalize('NFKC', text)
        if not normalized or max_candidates <= 0:
            return ()
        with self._lock:
            got = self.engine.g2p_candidates(normalized)
            raw = tuple(got.candidates)[:max_candidates]
        sequences: list[tuple[str, ...]] = []
        for candidate in raw:
            phones = tuple(p for word in candidate.words for p in self._clean_word(word))
            if phones and phones not in sequences:
                sequences.append(phones)
        return tuple(PronunciationCandidate(phones, rank=i)
                     for i, phones in enumerate(sequences))


@lru_cache(maxsize=1)
def default_g2p() -> G2PBackend:
    """Return the product Japanese G2P backend; missing readiness is an error."""
    return HaqumeiG2P()
