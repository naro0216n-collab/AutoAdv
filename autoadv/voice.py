"""Irodori HTTP synthesis and genuinely completed, cancellable device playback."""
from __future__ import annotations
import io
import json
import wave
import time
from threading import Event, Lock, Thread
from typing import Callable
from urllib import parse, request
from .conversation import Token


def synthesize(base_url: str, reading: str, voice: str, caption: str = '') -> bytes:
    if not reading.strip() or not voice.strip():
        raise ValueError('nonempty reading and resolved Voice ID required')
    base = parse.urlsplit(base_url)
    if base.scheme not in ('http', 'https') or not base.netloc:
        raise ValueError('invalid TTS URL')
    payload = {'model': 'irodori-tts', 'input': reading, 'voice': voice,
               'speed': 1.0, 'response_format': 'wav'}
    caption = caption.strip()
    if caption:
        payload['irodori'] = {'caption': caption}
    payload = json.dumps(payload).encode('utf-8')
    call = request.Request(base_url.rstrip('/') + '/v1/audio/speech', data=payload,
                           headers={'Content-Type': 'application/json', 'Accept': 'audio/wav'})
    with request.urlopen(call, timeout=90) as response:
        data = response.read()
    with wave.open(io.BytesIO(data), 'rb') as wav:
        if wav.getnframes() <= 0 or wav.getsampwidth() not in (1, 2, 3, 4):
            raise ValueError('Irodori returned invalid WAV')
    return data


def play(audio: bytes, cancelled: Event) -> bool:
    import sounddevice as sd
    with wave.open(io.BytesIO(audio), 'rb') as wav:
        dtype = {1: 'uint8', 2: 'int16', 3: 'int24', 4: 'int32'}[wav.getsampwidth()]
        with sd.RawOutputStream(samplerate=wav.getframerate(), channels=wav.getnchannels(),
                                dtype=dtype, blocksize=1024) as stream:
            while not cancelled.is_set():
                frames = wav.readframes(1024)
                if not frames:
                    # stop drains buffered audio, unlike finishing an HTTP request.
                    stream.stop()
                    return not cancelled.is_set()
                stream.write(frames)
            stream.abort()
    return False


class VoiceJobs:
    """One active presentation; superseded workers can never report as current."""
    def __init__(self, report: Callable[[Token, bool, str], None],
                 on_timing: Callable[[Token, str, float, float], None] | None = None):
        self.report = report
        self.on_timing = on_timing
        self.cancelled: Event | None = None
        self._generation = 0
        self._lock = Lock()

    def _timing(self, token: Token, stage: str, started_ns: int, audio_ms: float) -> None:
        try:
            self.on_timing(token, stage,
                           round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
                           round(audio_ms, 3))
        except Exception:
            pass  # Observability is not part of the TTS completion contract.

    def _is_current(self, generation: int, event: Event) -> bool:
        with self._lock:
            return (generation == self._generation and self.cancelled is event
                    and not event.is_set())

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            event, self.cancelled = self.cancelled, None
        if event is not None:
            event.set()

    def start(self, token: Token, url: str, reading: str, voice: str, caption: str = '') -> None:
        event = Event()
        with self._lock:
            previous = self.cancelled
            self._generation += 1
            generation = self._generation
            self.cancelled = event
        if previous is not None:
            previous.set()

        def finish_current() -> None:
            with self._lock:
                if generation == self._generation and self.cancelled is event:
                    self.cancelled = None

        def run():
            try:
                started_ns = time.perf_counter_ns() if self.on_timing else 0
                audio = synthesize(url, reading, voice, caption)
                if not self._is_current(generation, event):
                    return
                audio_ms = 0.0
                if self.on_timing:
                    # Diagnostic inspection must never turn successful synthesis
                    # into a playback failure.
                    try:
                        with wave.open(io.BytesIO(audio), 'rb') as wav:
                            audio_ms = wav.getnframes() * 1000.0 / wav.getframerate()
                    except Exception:
                        pass
                    self._timing(token, 'synthesis', started_ns, audio_ms)
                playback_ns = time.perf_counter_ns() if self.on_timing else 0
                success = play(audio, event)
                if not self._is_current(generation, event):
                    return
                if self.on_timing:
                    self._timing(token, 'playback', playback_ns, audio_ms)
                self.report(token, success, '' if success else 'TTS cancelled')
            except Exception as exc:
                if self._is_current(generation, event):
                    self.report(token, False, f'{type(exc).__name__}: {exc}')
            finally:
                finish_current()
        Thread(target=run, name='autoadv-tts', daemon=True).start()
