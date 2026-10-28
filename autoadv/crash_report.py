"""Persistent crash evidence for the GUI and its isolated native Hook Host.

Keep the diagnostic stream open: faulthandler needs a live file descriptor to
record faults from native code even when pythonw has no stderr console.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import faulthandler
import os
from pathlib import Path
import sys
import threading
import traceback


_stream = None
_path: Path | None = None
_reported = False
_retained = False
_lock = threading.Lock()


def crash_directory() -> Path:
    # Only an explicit crash-report destination overrides the installation folder.
    # In particular, AUTOADV_DATA_DIR changes product data during validation but
    # must not redirect a crash report away from the launched AutoAdv.cmd.
    configured = os.environ.get('AUTOADV_CRASH_DIR')
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1]


def _record(title: str, exception_type, value, tb) -> None:
    global _reported
    if _stream is None:
        return
    with _lock:
        _reported = True
        _stream.write(f'\n{datetime.now(timezone.utc).isoformat()} {title}\n')
        traceback.print_exception(exception_type, value, tb, file=_stream)
        _stream.flush()


def record_stage(stage: str, *, retain: bool = False) -> None:
    """Persist a runtime boundary even when termination bypasses Python hooks.

    A native Hook interaction can outlive a GUI incident. Preserve both reports
    once the interaction starts, even if their processes later exit normally.
    """
    global _retained
    try:
        with _lock:
            if _stream is not None:
                _stream.write(f'{datetime.now(timezone.utc).isoformat()} stage: {stage}\n')
                _stream.flush()
                if retain:
                    _retained = True
    except (OSError, ValueError):
        # Diagnostics must never take down the product during shutdown.
        pass


def record_exception(title: str, error: BaseException) -> None:
    """Record failures deliberately converted to a status or an IPC error."""
    _record(title, type(error), error, error.__traceback__)


def record_failure(detail: str) -> None:
    """Record an unexpected exit without an in-process Python traceback."""
    global _reported
    if _stream is None:
        return
    with _lock:
        _reported = True
        _stream.write(f'\n{datetime.now(timezone.utc).isoformat()} {detail}\n')
        _stream.flush()


def _main_exception(exception_type, value, tb) -> None:
    _record('unhandled Python exception', exception_type, value, tb)
    if sys.stderr is not None:
        sys.__excepthook__(exception_type, value, tb)


def _thread_exception(args) -> None:
    _record(f'unhandled exception in thread {args.thread.name if args.thread else "unknown"}',
            args.exc_type, args.exc_value, args.exc_traceback)
    if sys.stderr is not None:
        threading.__excepthook__(args)


def _unraisable_exception(args) -> None:
    _record('unraisable exception',
            args.exc_type, args.exc_value, args.exc_traceback)
    if sys.stderr is not None:
        sys.__unraisablehook__(args)


def _close() -> None:
    global _stream
    if _stream is None:
        return
    stream = _stream
    _stream = None
    try:
        faulthandler.disable()
        stream.flush()
        stream.close()
        if not (_reported or _retained) and _path is not None:
            _path.unlink(missing_ok=True)
    except OSError:
        pass


def install_crash_reporting(role: str) -> Path | None:
    """Install once per process, before loading Qt or the native Hook library.

    Idle reports are removed on clean exit; active Hook sessions and failures survive.
    """
    global _stream, _path
    if _stream is not None:
        return _path
    try:
        directory = crash_directory()
        directory.mkdir(parents=True, exist_ok=True)
        started = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path = directory / f'{role}-{started}-{os.getpid()}.log'
        stream = path.open('w', encoding='utf-8', buffering=1)
        try:
            stream.write(f'AutoAdv {role} | PID {os.getpid()} | {datetime.now(timezone.utc).isoformat()}\n'
                         f'Python {sys.version} | executable {sys.executable}\n')
            stream.flush()
            faulthandler.enable(file=stream, all_threads=True)
        except Exception:
            stream.close()
            path.unlink(missing_ok=True)
            raise
    except (OSError, RuntimeError, ValueError) as error:
        if sys.stderr is not None:
            print(f'AutoAdv crash reporting unavailable: {error}', file=sys.stderr)
        return None
    _stream, _path = stream, path
    sys.excepthook = _main_exception
    threading.excepthook = _thread_exception
    sys.unraisablehook = _unraisable_exception
    atexit.register(_close)
    return path
