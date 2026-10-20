"""Observed text from the directly managed LunaHost selection.

An observation alone never proves a displayed line or grants game input.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class OriginObservation:
    sequence: int
    raw_packet: str
    text: str
    authoritative_for_autoplay: bool = False
    # Native provenance is retained for diagnostics and source/generation review.
    kind: str = "hook"
    source_generation: int = 1
    game_id: str = ""
    profile_id: str = ""
    origin: str = ""
