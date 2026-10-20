"""Explicitly selected Japanese OCR adapter; never installs a model or grants input.

Tesseract CLI is used from an existing installation. The OCR region is relative
 to the verified target client's pixels. Recognition failures are errors, never
 synthetic empty dialogue or evidence of absent native speech.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from threading import Thread, Event
from collections.abc import Callable
import subprocess
import time

from .acquisition import SourceSelection, SourceKind, TextCandidate


class TesseractOCR:
    def __init__(self, selection: SourceSelection, capture: Callable):
        if selection.kind is not SourceKind.OCR:
            raise ValueError('OCR reader requires explicit OCR selection')
        self.selection, self.capture = selection, capture

    def recognize(self) -> str:
        import cv2  # Imported only when OCR has explicitly been selected.
        frame = self.capture()
        x, y, w, h = self.selection.region
        if (frame is None or len(frame.shape) != 3 or
            x + w > frame.shape[1] or y + h > frame.shape[0]):
            raise RuntimeError('OCR region is outside verified game client area')
        ok, data = cv2.imencode('.png', frame[y:y+h, x:x+w])
        if not ok:
            raise RuntimeError('failed to encode OCR region')
        try:
            p = subprocess.run([self.selection.tesseract_executable, 'stdin', 'stdout',
                                '-l', self.selection.language], input=data.tobytes(),
                               capture_output=True, timeout=4, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('OCR engine not available or timed out') from exc
        if p.returncode != 0:
            raise RuntimeError('OCR engine failed: ' + p.stderr.decode('utf-8', 'replace')[:240])
        return p.stdout.decode('utf-8', 'replace').strip()


class OCRWorker:
    """At most one active OCR call; observation only, no Controller ownership."""
    def __init__(self, *, reader: TesseractOCR, game_id: str, profile_id: str,
                 generation: int, on_candidate: Callable[[TextCandidate], None],
                 on_failure: Callable[[str], None], on_blank: Callable[[], None] | None=None,
                 interval_ms: int = 300):
        self.reader = reader
        self.game_id, self.profile_id, self.generation = game_id, profile_id, generation
        self.on_candidate, self.on_failure = on_candidate, on_failure
        self.on_blank = on_blank or (lambda:None)
        self.interval_ms = max(200, interval_ms)
        self.stop_event = Event()
        self.thread: Thread | None = None

    def start(self) -> None:
        if self.thread is not None:
            raise RuntimeError('OCR worker already started')
        self.thread = Thread(target=self._run, daemon=True, name='autoadv-ocr')
        self.thread.start()

    def _run(self):
        previous = None
        sequence = 0
        while not self.stop_event.is_set():
            try:
                text = self.reader.recognize()
            except Exception as exc:
                self.on_failure(f'{type(exc).__name__}: {exc}')
                return
            if text != previous:
                previous = text
                if not text.strip():
                    self.on_blank()
                if text.strip():
                    sequence += 1
                    self.on_candidate(TextCandidate(self.game_id,self.profile_id,SourceKind.OCR,
                        self.generation,sequence,time.monotonic_ns()//1_000_000,text,
                        f'ocr:hwnd={getattr(self.reader.capture, "hwnd", "unknown")}'
                        f':roi={self.reader.selection.region}'))
            self.stop_event.wait(self.interval_ms/1000)

    def close(self):
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
