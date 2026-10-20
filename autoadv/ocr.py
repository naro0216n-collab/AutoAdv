"""Explicit optional OCR acquisition; game advancement remains Controller-owned."""
from __future__ import annotations
from pathlib import Path
from threading import Event, Thread
from typing import Callable
import subprocess
from .model import OCRSettings


class TesseractOCR:
    def __init__(self, config: OCRSettings, capture: Callable):
        config.validate()
        self.config, self.capture = config, capture

    def recognize(self) -> str:
        import cv2
        frame = self.capture()
        x, y, w, h = self.config.region
        if (frame is None or len(frame.shape) != 3 or
                x + w > frame.shape[1] or y + h > frame.shape[0]):
            raise RuntimeError('OCR rectangle outside verified client area')
        ok, png = cv2.imencode('.png', frame[y:y+h, x:x+w])
        if not ok:
            raise RuntimeError('cannot encode game OCR image')
        try:
            result = subprocess.run([self.config.executable, 'stdin', 'stdout',
                                     '-l', self.config.language], input=png.tobytes(),
                                    capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError('configured Tesseract failed to execute') from exc
        if result.returncode:
            raise RuntimeError('Tesseract failure: ' + result.stderr.decode('utf-8', 'replace')[:200])
        return result.stdout.decode('utf-8', 'replace').strip()


class OCRWorker:
    def __init__(self, reader: TesseractOCR, on_text: Callable[[int, str], None],
                 on_error: Callable[[str], None]):
        self.reader = reader
        self.on_text, self.on_error = on_text, on_error
        self.stopping = Event()
        self.thread: Thread | None = None

    def start(self):
        if self.thread is not None:
            raise RuntimeError('OCR already started')
        self.thread = Thread(target=self._run, daemon=True, name='autoadv-ocr')
        self.thread.start()

    def _run(self):
        previous = None
        pending = None
        count = 0
        sequence = 0
        while not self.stopping.is_set():
            try:
                text = self.reader.recognize()
            except Exception as exc:
                if not self.stopping.is_set():
                    self.on_error(f'{type(exc).__name__}: {exc}')
                return
            if text == pending:
                count += 1
            else:
                pending, count = text, 1
            # Require consistent observations before interpreting a new OCR
            # result. OCR does not supply LunaHook's flushed output boundary.
            if count >= self.reader.config.stable_samples and pending != previous:
                previous = pending
                sequence += 1
                self.on_text(sequence, pending)  # Stable blank marks a boundary.
            self.stopping.wait(self.reader.config.interval_ms / 1000)

    def close(self):
        self.stopping.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=6)
