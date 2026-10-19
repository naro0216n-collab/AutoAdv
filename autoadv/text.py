"""Keep the displayed original distinct from TTS and matching variants."""
from __future__ import annotations
from dataclasses import dataclass
import re
import unicodedata
from .contracts import ContentKind
from .speaker import split_speaker, repair_ruby_and_names

# Rubies and bracketed readings are intentionally NOT stripped automatically:
# the game may use parentheses as dialogue rather than furigana.
_SYMBOLS = frozenset("…‥―—－ー!！?？。、，．・･…〜～-‐–~♪♫☆★♡♥！？ \\t\\n\\r")


@dataclass(frozen=True, slots=True)
class LineText:
    raw: str
    spoken: str
    matching: str
    kind: ContentKind
    effective_chars: int


def normalize(raw: str, *, empty_confirmed: bool = False, names: dict[str, str] | None = None) -> LineText:
    if not raw or not raw.strip():
        kind = ContentKind.EMPTY_EVENT if empty_confirmed else ContentKind.NON_DIALOGUE
        return LineText(raw, "", "", kind, 0)
    _, spoken_body = split_speaker(raw)
    cleaned = unicodedata.normalize("NFKC", spoken_body).strip()
    cleaned = repair_ruby_and_names(cleaned, names)
    meaningful = [ch for ch in cleaned if not ch.isspace()]
    # The character set is illustrative, not a whitelist of Japanese phonemes.
    # Any letter, number, or unfamiliar character is considered speakable.
    symbolic = all((ch in _SYMBOLS or unicodedata.category(ch).startswith("P")) for ch in meaningful)
    kind = ContentKind.SYMBOLIC if symbolic else ContentKind.SPEAKABLE
    matching = re.sub(r"\s+", "", cleaned)
    return LineText(raw, "" if symbolic else cleaned, matching, kind, len(meaningful))