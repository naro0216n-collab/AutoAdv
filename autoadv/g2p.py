"""Japanese grapheme-to-phoneme contracts shared by timing and alignment."""
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
    reading_units: int | None = None


@dataclass(frozen=True)
class WordSpan:
    start: int
    end: int
    surface: str


class G2PBackend(Protocol):
    def candidates(self, text: str, *, max_candidates: int = 4) -> tuple[PronunciationCandidate, ...]: ...
    def word_spans(self, text: str) -> tuple[WordSpan, ...]: ...


_READING_NUCLEI = frozenset({"a", "i", "u", "e", "o", "I", "U"})
_READING_SPECIAL = frozenset({"N", "cl"})


def count_reading_units(phonemes: tuple[str, ...]) -> int:
    """Count mora-like timing units from one primary pronunciation.

    Vowel nuclei, moraic nasal and geminate closure each consume one timing
    unit. Consonants only qualify the following nucleus. An unresolved Haqumei
    surface remains explicit as ``unk:<surface>`` and contributes its visible
    Letter/Number length rather than disappearing from the timing estimate.
    """
    total = 0
    for phone in phonemes:
        if phone in _READING_NUCLEI or phone in _READING_SPECIAL:
            total += 1
        elif phone.startswith("unk:"):
            surface = phone[4:]
            visible = sum(unicodedata.category(ch)[0] in "LN" for ch in surface)
            total += max(1, visible) if surface else 0
    return total


def reading_units_from_candidates(text: str, candidates: tuple[PronunciationCandidate, ...]) -> int:
    if not candidates:
        if any(unicodedata.category(ch)[0] in "LN" for ch in text):
            raise ValueError("G2P returned no primary pronunciation for readable text")
        return 0
    primary = candidates[0]
    return primary.reading_units if primary.reading_units is not None else count_reading_units(primary.phonemes)


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
    def _span_bounds(span) -> tuple[int, int]:
        if hasattr(span, 'start') and hasattr(span, 'end'):
            return int(span.start), int(span.end)
        if isinstance(span, (tuple, list)) and len(span) == 2:
            return int(span[0]), int(span[1])
        raise ValueError('Haqumei mapping returned an unsupported char_span')

    def word_spans(self, text: str) -> tuple[WordSpan, ...]:
        """Return verified primary word spans in the G2P canonical coordinate space.

        Haqumei documents ``char_span`` against its normalized/text2mecab view,
        not necessarily the caller's source string. AutoAdv therefore exposes
        those spans only after verifying that each mapped surface still denotes
        the corresponding NFKC input slice. Source-coordinate projection stays
        with the caller that owns the source text.
        """
        normalized = unicodedata.normalize('NFKC', text)
        if not normalized:
            return ()
        with self._lock:
            words = tuple(self.engine.g2p_mapping_detailed(normalized))
        spans: list[WordSpan] = []
        previous_end = 0
        for word in words:
            start, end = self._span_bounds(word.char_span)
            if not 0 <= start < end <= len(normalized) or start < previous_end:
                raise ValueError('Haqumei mapping returned an invalid char_span')
            surface = str(getattr(word, 'word', ''))
            mapped = unicodedata.normalize('NFKC', surface)
            source_slice = normalized[start:end]
            if mapped != source_slice:
                raise ValueError('Haqumei word boundary cannot be projected to canonical input')
            spans.append(WordSpan(start, end, surface))
            previous_end = end
        return tuple(spans)

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
            got = self.engine.g2p_candidates_detailed(normalized)
            raw = tuple(got.candidates)[:max_candidates]
        sequences: list[PronunciationCandidate] = []
        for candidate in raw:
            phones = tuple(p for word in candidate.words for p in self._clean_word(word))
            if phones and all(item.phonemes != phones for item in sequences):
                units = 0
                for word in candidate.words:
                    cleaned = self._clean_word(word)
                    if not cleaned:
                        continue
                    if cleaned[0].startswith('unk:'):
                        units += count_reading_units(cleaned)
                    elif getattr(word, 'is_unknown', False) and not word.mora_count:
                        units += max(1, sum(unicodedata.category(ch)[0] in 'LN'
                                            for ch in str(word.word)))
                    else:
                        units += int(word.mora_count)
                sequences.append(PronunciationCandidate(phones, rank=len(sequences), reading_units=units))
        return tuple(sequences)


@lru_cache(maxsize=1)
def default_g2p() -> G2PBackend:
    """Return the product Japanese G2P backend; missing readiness is an error."""
    return HaqumeiG2P()
