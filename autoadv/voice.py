"""Irodori HTTP synthesis and genuinely completed, cancellable device playback."""
from __future__ import annotations
import io
import json
import wave
import time
from threading import Event, Thread
from typing import Callable
from urllib import parse, request
from .conversation import Token


def synthesize(base_url: str, reading: str, voice: str) -> bytes:
    if not reading.strip() or not voice.strip():
        raise ValueError('nonempty reading and resolved Voice ID required')
    base = parse.urlsplit(base_url)
    if base.scheme not in ('http', 'https') or not base.netloc:
        raise ValueError('invalid TTS URL')
    payload = json.dumps({'model': 'irodori-tts', 'input': reading, 'voice': voice,
                          'speed': 1.0, 'response_format': 'wav'}).encode('utf-8')
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
    """One active presentation; every completion is tied to a session/line token."""
    def __init__(self, report: Callable[[Token, bool, str], None],
                 on_timing: Callable[[Token, str, float, float], None] | None = None):
        self.report = report
        self.on_timing = on_timing
        self.cancelled: Event | None = None

    def _timing(self, token: Token, stage: str, started_ns: int, audio_ms: float) -> None:
        try:
            self.on_timing(token, stage,
                           round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
                           round(audio_ms, 3))
        except Exception:
            pass  # Observability is not part of the TTS completion contract.

    def cancel(self) -> None:
        if self.cancelled:
            self.cancelled.set()
            self.cancelled = None

    def start(self, token: Token, url: str, reading: str, voice: str) -> None:
        self.cancel()
        event = Event()
        self.cancelled = event
        def run():
            try:
                started_ns = time.perf_counter_ns() if self.on_timing else 0
                audio = synthesize(url, reading, voice)
                if self.on_timing:
                    # Diagnostic inspection must never turn successful synthesis
                    # into a playback failure.
                    audio_ms = 0.0
                    try:
                        with wave.open(io.BytesIO(audio), 'rb') as wav:
                            audio_ms = wav.getnframes() * 1000.0 / wav.getframerate()
                    except Exception:
                        pass
                    self._timing(token, 'synthesis', started_ns, audio_ms)
                playback_ns = time.perf_counter_ns() if self.on_timing else 0
                success = not event.is_set() and play(audio, event)
                if self.on_timing and not event.is_set():
                    self._timing(token, 'playback', playback_ns, audio_ms)
                self.report(token, success, '' if success else 'TTS cancelled')
            except Exception as exc:
                if not event.is_set():
                    self.report(token, False, f'{type(exc).__name__}: {exc}')
        Thread(target=run, name='autoadv-tts', daemon=True).start()
