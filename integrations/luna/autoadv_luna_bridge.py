"""Copy beside LunaTranslator.py ONLY when the optional patch is installed."""
from __future__ import annotations
import itertools
import json
import os
import socket
import threading
import time

_seq=itertools.count(1)
_lock=threading.Lock()
_sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)


def emit(raw, *, from_hook=False):
    if not isinstance(raw,str):
        return
    with _lock:
        seq=next(_seq)
    data=json.dumps({
        "schema_version":1,
        "source_seq":seq,
        "observed_at_mono_ms":time.monotonic_ns()//1_000_000,
        "raw_text":raw,
        "source":"HOOK" if from_hook else "UNCLASSIFIED",
        "event_kind":"line_candidate",
    },ensure_ascii=False,separators=(",",":")).encode("utf-8")
    if len(data)>65000:
        return
    try:
        port=int(os.environ.get("AUTOADV_BRIDGE_PORT","47916"))
        _sock.sendto(data,("127.0.0.1",port))
    except (OSError,ValueError):
        # Never interfere with LunaTranslator's extraction/translation.
        pass