"""LunaTranslator origin WebSocket: OBSERVATION ONLY, not a line-identity API.

The official stream loses empty and duplicate events and does not prove text
readiness, scene state, or distinct identical successive line epochs. It must
not be used by itself for automatic advancement.
"""
from __future__ import annotations
from dataclasses import dataclass
from collections.abc import AsyncIterator
import json


@dataclass(frozen=True)
class OriginObservation:
    sequence: int
    raw_packet: str
    text: str
    authoritative_for_autoplay: bool = False


async def observe_origin(uri: str = "ws://127.0.0.1:2333/api/ws/text/origin") -> AsyncIterator[OriginObservation]:
    import websockets  # optional dependency
    async with websockets.connect(uri) as conn:
        sequence = 0
        async for message in conn:
            sequence += 1
            raw = message if isinstance(message, str) else message.decode("utf-8")
            # Preserve packet verbatim; never fabricate a line boundary or ready flag.
            try:
                parsed = json.loads(raw)
                value = parsed if isinstance(parsed, str) else parsed.get("text", raw) if isinstance(parsed, dict) else raw
                text = value if isinstance(value, str) else raw
            except json.JSONDecodeError:
                text = raw
            yield OriginObservation(sequence, raw, text)