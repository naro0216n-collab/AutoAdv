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


def decode_origin_text(message: str | bytes) -> str:
    raw = message if isinstance(message,str) else message.decode("utf-8")
    try:
        parsed=json.loads(raw)
        value=parsed if isinstance(parsed,str) else parsed.get("text",raw) if isinstance(parsed,dict) else raw
        return value if isinstance(value,str) else raw
    except json.JSONDecodeError:
        return raw


async def observe_origin(uri: str = "ws://127.0.0.1:2333/api/ws/text/origin") -> AsyncIterator[OriginObservation]:
    import websockets
    async with websockets.connect(uri) as conn:
        sequence = 0
        async for message in conn:
            sequence += 1
            raw = message if isinstance(message, str) else message.decode("utf-8")
            yield OriginObservation(sequence,raw,decode_origin_text(raw))


def run_origin_listener(uri:str,stop,received,on_status=None) -> None:
    """Blocking worker-thread entrypoint with reconnection and explicit disconnect."""
    import asyncio
    import time
    on_status = on_status or (lambda *_:None)
    async def loop():
        import websockets
        seq=0
        while not stop.is_set():
            try:
                async with websockets.connect(uri,open_timeout=5) as ws:
                    on_status(True,'connected')
                    while not stop.is_set():
                        try:message=await asyncio.wait_for(ws.recv(),timeout=.5)
                        except asyncio.TimeoutError:continue
                        seq+=1
                        raw=message if isinstance(message,str) else message.decode('utf-8')
                        received(OriginObservation(seq,raw,decode_origin_text(raw)))
            except Exception as exc:
                on_status(False,f'{type(exc).__name__}: {exc}')
            else:
                on_status(False,'WebSocket closed')
            if not stop.is_set():await asyncio.sleep(1)
    asyncio.run(loop())
