"""Swappable Japanese grapheme-to-phoneme adapters used by native ASR matching."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
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

    Haqumei's candidate delta is a morphological lattice cost, not a reading
    probability, so AutoAdv uses only the documented candidate order. The
    first candidate is the context-selected primary reading.
    """

    def __init__(self, engine=None):
        if engine is None:
            from haqumei import Haqumei
            engine = Haqumei()
        self.engine = engine

    @staticmethod
    def _clean(phonemes) -> tuple[str, ...]:
        # Punctuation/space pauses vary between displayed text and ASR ITN and
        # are not speech identity. Unknown labels remain uncertainty evidence.
        cleaned = tuple(str(p) for p in phonemes if str(p) not in {'pau', 'sp', ''})
        # An unknown label has no acoustic identity. Treating two `unk` labels
        # as equal would create false phonetic evidence for unrelated words.
        return () if 'unk' in cleaned else cleaned

    def candidates(self, text: str, *, max_candidates: int = 4) -> tuple[PronunciationCandidate, ...]:
        normalized = unicodedata.normalize('NFKC', text)
        if not normalized or max_candidates <= 0:
            return ()
        got = self.engine.g2p_candidates(normalized)
        sequences: list[tuple[str, ...]] = []
        for candidate in list(got.candidates)[:max_candidates]:
            phones = self._clean(
                p for word in candidate.words for p in word.phonemes)
            if phones and phones not in sequences:
                sequences.append(phones)
        return tuple(PronunciationCandidate(p, rank=i)
                     for i, p in enumerate(sequences))


@lru_cache(maxsize=1)
def default_g2p() -> G2PBackend | None:
    """Return Haqumei when the native extra is installed.

    Core/unit installs intentionally do not require the native wheel. Product
    native-audio startup constructs Haqumei explicitly and therefore fails its
    readiness path instead of silently degrading when the installed backend is
    unusable.
    """
    try:
        return HaqumeiG2P()
    except ImportError:
        return None
