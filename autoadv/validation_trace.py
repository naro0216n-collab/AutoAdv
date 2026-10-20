"""Opt-in, non-authoritative records of the *real* AutoAdv GUI execution path.

Only hashes of dialogue text are recorded. The source game and test expectations
are never consulted by production code. This is diagnostic observation, not a
second dialogue controller or an input trigger.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import time
import unicodedata


def text_digest(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ValidationTrace:
    def __init__(self, path: str | None = None):
        if path is None:
            path = os.environ.get("AUTOADV_VALIDATION_TRACE", "")
        self.path = Path(path).expanduser() if path else None
        self._sequence = 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Refuse to append to a previous session; comparison must use one run.
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps({"event": "trace_started", "version": 1,
                    "at_ns": time.monotonic_ns()}, ensure_ascii=False) + "\n")

    def emit(self, event: str, **fields: object) -> None:
        if self.path is None:
            return
        self._sequence += 1
        item = {"event": event, "seq": self._sequence, "at_ns": time.monotonic_ns(), **fields}
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
