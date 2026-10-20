"""One selected text source per game/profile session; never a second controller.

A decoded HOOK output or an OCR result is a candidate, not a line boundary,
visual readiness, choice clearance, or permission to inject input.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SourceKind(str, Enum):
    HOOK = 'hook'
    OCR = 'ocr'


@dataclass(frozen=True)
class SourceSelection:
    kind: SourceKind = SourceKind.HOOK
    tesseract_executable: str = 'tesseract'
    language: str = 'jpn'
    region: Optional[tuple[int, int, int, int]] = None
    stable_ms: int = 350

    def __post_init__(self):
        if self.stable_ms < 100 or self.stable_ms > 5000:
            raise ValueError('text stability window must be between 100 and 5000ms')
        if self.kind is SourceKind.OCR:
            if not self.tesseract_executable.strip() or not self.language.strip():
                raise ValueError('OCR executable and language are required')
            if self.region is None or len(self.region) != 4 or any(type(v) is not int for v in self.region):
                raise ValueError('OCR requires an explicit client-relative rectangle')
            x, y, w, h = self.region
            if min(x, y) < 0 or min(w, h) <= 0:
                raise ValueError('invalid OCR region')

    def to_dict(self) -> dict:
        return {'kind': self.kind.value, 'tesseract_executable': self.tesseract_executable,
                'language': self.language, 'region': list(self.region) if self.region else None,
                'stable_ms': self.stable_ms}

    @classmethod
    def from_dict(cls, doc: dict | None) -> 'SourceSelection':
        if not doc:
            return cls()
        if not isinstance(doc, dict) or set(doc) != {
                'kind', 'tesseract_executable', 'language', 'region', 'stable_ms'}:
            raise ValueError('unsupported acquisition settings')
        try:
            kind = SourceKind(doc['kind'])
        except ValueError as exc:
            raise ValueError('unsupported acquisition source') from exc
        region = tuple(doc['region']) if doc['region'] is not None else None
        return cls(kind, doc['tesseract_executable'], doc['language'], region, doc['stable_ms'])


@dataclass(frozen=True)
class TextCandidate:
    game_id: str
    profile_id: str
    kind: SourceKind
    source_generation: int
    sequence: int
    at_ms: int
    text: str
    origin: str
    # 'stable' applies to candidate text only, NOT screen/input permission.
    stable: bool = False


class AcquisitionSession:
    """Reject old/foreign results at entry; switching invalidates the epoch."""
    def __init__(self, game_id: str, profile_id: str, selection: SourceSelection):
        if not game_id or not profile_id:
            raise ValueError('game and profile are required')
        self.game_id, self.profile_id, self.selection = game_id, profile_id, selection
        self.generation = 1
        self.last_seq = 0

    def change(self, game_id: str, profile_id: str, selection: SourceSelection) -> None:
        if not game_id or not profile_id:
            raise ValueError('game and profile are required')
        self.game_id, self.profile_id, self.selection = game_id, profile_id, selection
        self.generation += 1
        self.last_seq = 0

    def accept(self, event: TextCandidate) -> bool:
        if (event.game_id != self.game_id or event.profile_id != self.profile_id or
            event.kind != self.selection.kind or event.source_generation != self.generation or
            event.sequence <= self.last_seq or not event.origin.strip() or not event.text.strip()):
            return False
        self.last_seq = event.sequence
        return True
