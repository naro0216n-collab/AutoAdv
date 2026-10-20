"""Process-scoped native audio recognition, observed completion and AUTO attribution.

Actual audio is captured by a Windows Process Loopback helper, then analyzed in
one worker. Only matched ASR + VAD evidence for the active Utterance can report
native completion. Neither capture silence nor a timer asserts voice absence.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import Callable
import time
from .audio_buffer import AudioHistory, PCMChunk
from .audio_match import TargetMatcher, VoiceActivity
from .conversation import Token
from .process_audio import ProcessCapture, CaptureHealth


class JapaneseASR:
    def __init__(self, model: Path, tokens: Path):
        if not model.is_file() or not tokens.is_file():
            raise FileNotFoundError('configured ASR model and tokens required')
        import sherpa_onnx
        self.engine = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), num_threads=2, use_itn=True, debug=False)

    def recognize(self, chunks: tuple[PCMChunk, ...]) -> str:
        import numpy as np
        if not chunks:
            return ''
        if any((c.sample_rate, c.channels, c.sample_width_bytes) != (48000, 2, 4) for c in chunks):
            raise ValueError('ASR requires 48 kHz stereo float32 PCM')
        stereo = np.concatenate([np.frombuffer(c.frames, dtype='<f4').reshape(-1, 2) for c in chunks])
        mono = stereo.mean(axis=1).astype('float32')
        stream = self.engine.create_stream()
        stream.accept_waveform(48000, mono)
        self.engine.decode_stream(stream)
        return stream.result.text


class NativeAudio:
    """Owns one process's capture and one active line; never injects game input."""
    def __init__(self, pid: int, helper: Path, model: Path, tokens: Path,
                 notify: Callable[[Token | None, str, str], None],
                 *, recognizer=None, activity=None, clock=None):
        self.notify = notify
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.history = AudioHistory(horizon_ms=10000)
        self.asr = recognizer or JapaneseASR(model, tokens)
        self.activity = activity or VoiceActivity()
        self._lock = RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='autoadv-asr')
        self._capture = ProcessCapture(pid, helper, self.history, self.feed, self.health, self.clock)
        self.token: Token | None = None
        self.matcher: TargetMatcher | None = None
        self.start_ms = 0
        self.boundary_ms = 0
        self.rise_ms = -1
        self.active = False
        self.attributed = False
        self.completed = False
        self.tail_ms = 0
        self._inference_at = -1
        self._inflight = False
        self._generation = 0
        self._closed = False
        self._last_vad = False

    def start(self):
        self._capture.start()

    def boundary(self):
        with self._lock:
            self.boundary_ms = self.clock()

    def begin(self, token: Token, match_text: str):
        with self._lock:
            self._generation += 1
            self.token = token
            self.matcher = TargetMatcher(match_text)
            self.start_ms = self.clock()
            self.active = self.attributed = self.completed = False
            self._inference_at = -1

    def cancel(self):
        with self._lock:
            self._generation += 1
            self.token = None
            self.matcher = None

    def health(self, status: CaptureHealth):
        if not status.healthy:
            with self._lock:
                token = self.token
            self.notify(token, 'error', status.reason)

    def feed(self, chunk: PCMChunk):
        try:
            activity = self.activity.observe_float32_stereo(chunk.frames)
        except Exception as exc:
            self.notify(self.token, 'error', 'VAD failure: ' + str(exc))
            return
        with self._lock:
            if self._closed:
                return
            if activity.frames_analyzed:
                if activity.active and not self._last_vad:
                    self.rise_ms = chunk.at_ms
                self._last_vad = activity.active
            self.tail_ms = activity.tail_silence_ms
            if (self.token is None or self._inflight or self.completed or
                    (self._inference_at >= 0 and chunk.at_ms - self._inference_at < 500)):
                return
            token, generation = self.token, self._generation
            self._inflight = True
            self._inference_at = chunk.at_ms
            samples = self.history.since(max(self.boundary_ms, self.start_ms - 2000))
        self._pool.submit(self._infer, token, generation, samples)

    def _infer(self, token, generation, samples):
        try:
            hypothesis = self.asr.recognize(samples)
            events = []
            with self._lock:
                if (self._closed or self._generation != generation or token != self.token
                        or not self.matcher or not self.history.healthy):
                    return
                progress = self.matcher.observe(hypothesis)
                recent = self.rise_ms >= max(self.boundary_ms, self.start_ms - 2000)
                if not self.attributed and recent and progress.matched_chars >= 2 and progress.onset_score >= .85:
                    self.attributed = True
                    events.append('started')
                if (self.attributed and progress.monotonic and progress.coverage >= .90
                        and progress.tail_confidence >= .90 and self.tail_ms >= 300):
                    self.completed = True
                    events.append('completed')
            for event in events:
                self.notify(token, event, '')
        except Exception as exc:
            self.notify(token, 'error', 'ASR failure: ' + str(exc))
        finally:
            with self._lock:
                self._inflight = False

    def close(self):
        self.cancel()
        with self._lock:
            self._closed = True
        self._capture.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
