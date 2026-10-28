"""Deterministic pronunciation backend for domain tests."""
from __future__ import annotations

import unicodedata

from autoadv.g2p import PronunciationCandidate, WordSpan


class FakeG2P:
    def __init__(self, mapping: dict[str, tuple[str, ...]] | None = None,
                 word_spans: dict[str, tuple[tuple[int, int], ...]] | None = None):
        self.mapping = mapping or {}
        self._word_spans = word_spans or {}

    def word_spans(self, text: str):
        normalized = unicodedata.normalize('NFKC', text)
        spans = self._word_spans.get(text)
        if spans is None:
            return (WordSpan(0, len(normalized), normalized),) if normalized else ()
        return tuple(WordSpan(start, end, normalized[start:end]) for start, end in spans)

    def candidates(self, text: str, *, max_candidates: int = 4):
        if max_candidates <= 0:
            return ()
        phones = self.mapping.get(text)
        if phones is None:
            phones = tuple(
                'a' for character in text
                if unicodedata.category(character)[0] in 'LN' or character == 'ー'
            )
        if not phones:
            return ()
        return (PronunciationCandidate(tuple(phones), rank=0),)


TEST_G2P = FakeG2P()
