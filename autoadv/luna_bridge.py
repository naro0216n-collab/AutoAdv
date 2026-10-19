"""Receive opt-in pre-normalization Luna line candidates over loopback UDP.

Packets are NOT authoritative about line identity, text readiness or scene
state. No packet from this bridge may directly trigger input.
"""
from __future__ import annotations
import dataclasses
import json
import socket
from collections.abc import Iterator


@dataclasses.dataclass(frozen=True)
class Candidate:
    schema_version: int
    source_seq: int
    observed_at_mono_ms: int
    raw_text: str
    source: str
    event_kind: str = "line_candidate"
    line_identity_verified: bool = False
    text_ready: bool = False


def decode(blob: bytes) -> Candidate:
    if len(blob) > 65000:
        raise ValueError("line packet exceeds UDP transport bound")
    obj=json.loads(blob)
    if not isinstance(obj,dict) or set(obj) != {
        "schema_version", "source_seq", "observed_at_mono_ms", "raw_text", "source", "event_kind"
    }:
        raise ValueError("unexpected Luna bridge payload schema")
    if obj["schema_version"] != 1 or obj["event_kind"] != "line_candidate":
        raise ValueError("unknown bridge version/kind")
    if (type(obj["source_seq"]) is not int or obj["source_seq"] <= 0 or
        type(obj["observed_at_mono_ms"]) is not int or obj["observed_at_mono_ms"] < 0 or
        not isinstance(obj["raw_text"],str) or obj["source"] not in ("HOOK","UNCLASSIFIED")):
        raise ValueError("invalid candidate fields")
    return Candidate(**obj)


def listen(host: str = "127.0.0.1",port: int = 47916) -> Iterator[Candidate]:
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("candidate receiver must bind loopback")
    family=socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family,socket.SOCK_DGRAM) as sock:
        sock.bind((host,port))
        while True:
            body,_=sock.recvfrom(65507)
            try:
                yield decode(body)
            except (ValueError,UnicodeDecodeError,json.JSONDecodeError):
                continue