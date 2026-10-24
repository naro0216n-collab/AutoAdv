"""Deterministic pronunciation backend for domain tests."""
from __future__ import annotations

import unicodedata

from autoadv.g2p import PronunciationCandidate


class FakeG2P:
    def __init__(self, mapping: dict[str, tuple[str, ...]] | None = None):
        self.mapping = mapping or {}

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
