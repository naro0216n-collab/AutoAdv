"""Irodori-TTS-Server client and cancellable, device-draining WAV playback.

HTTP completion is synthesis completion. TTS_FINISHED success is emitted only
when the PCM playback device has finished and the output stream has stopped.
Audio remains outside the game process and is not routed into its loopback.
"""
from __future__ import annotations
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Callable
from urllib import request, parse
import io
import json
import time
import wave

from .contracts import Effect, EffectKind, Envelope, EventKind


@dataclass(frozen=True)
class IrodoriConfig:
    url: str = "http://127.0.0.1:8088/v1/audio/speech"
    model: str = "irodori-tts"
    speed: float = 1.0
    timeout_sec: float = 90
    api_key: str | None = None

    def __post_init__(self):
        parsed = parse.urlsplit(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path != "/v1/audio/speech":
            raise ValueError("expected a configured /v1/audio/speech URL")
        if not 0.25 <= self.speed <= 4:
            raise ValueError("Irodori speed must be within 0.25..4.0")


class IrodoriClient:
    def __init__(self, config: IrodoriConfig | None = None):
        self.config = config or IrodoriConfig()

    def synthesize(self, text: str, voice: str) -> bytes:
        if not text.strip() or not voice.strip():
            raise ValueError("TTS requires text and resolved voice")
        payload = json.dumps({
            "model": self.config.model, "input": text,
            "voice": voice, "speed": self.config.speed, "response_format": "wav",
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "audio/wav"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        req = request.Request(self.config.url, data=payload, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.config.timeout_sec) as resp:
            data = resp.read()
            with wave.open(io.BytesIO(data), "rb") as wav:
                if wav.getnframes() <= 0 or wav.getsampwidth() not in (1, 2, 3, 4):
                    raise ValueError("Irodori returned no playable PCM WAV")
            return data


def play_wav(audio: bytes, canceled: Event) -> bool:
    """Return True only after full device playback. Require sounddevice.

    Uses raw PCM frames, including 24-bit samples; cancels by aborting the
    stream immediately. Does not infer playback from wall time or file length.
    """
    import sounddevice as sd  # type: ignore[import-untyped]

    with wave.open(io.BytesIO(audio), "rb") as wav:
        dtype = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}[wav.getsampwidth()]
        stream = sd.RawOutputStream(samplerate=wav.getframerate(), channels=wav.getnchannels(),
                                    dtype=dtype, blocksize=1024)
        stream.start()
        try:
            while not canceled.is_set():
                frames = wav.readframes(1024)
                if not frames:
                    if canceled.is_set():
                        stream.abort()
                        return False
                    # PortAudio stop waits until any buffered output finishes.
                    stream.stop()
                    return not canceled.is_set()
                stream.write(frames)
            stream.abort()
            return False
        finally:
            stream.close()


class TTSWorker:
    """Asynchronous TTS worker. Callback posts to Controller's serialized queue."""

    def __init__(self, client: IrodoriClient, emit: Callable[[Envelope], None],
                 player: Callable[[bytes, Event], bool] = play_wav,
                 monotonic_ms: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000):
        self.client = client
        self.emit = emit
        self.player = player
        self.clock = monotonic_ms
        self._jobs: dict[tuple[str, int, int], Event] = {}
        self._lock = Lock()

    def apply(self, effect: Effect) -> None:
        key = (effect.session_id, effect.line_epoch, effect.cancel_token)
        if effect.kind is EffectKind.CANCEL_TTS:
            with self._lock:
                cancel = self._jobs.get(key)
                if cancel:
                    cancel.set()
            return
        if effect.kind is not EffectKind.START_TTS:
            raise ValueError("TTSWorker accepts only TTS effects")
        if not effect.voice_id:
            raise ValueError("unresolved voice must not start a job")
        with self._lock:
            if key in self._jobs:
                raise RuntimeError("duplicate TTS job")
            cancellation = Event()
            self._jobs[key] = cancellation

        def work():
            success = False
            try:
                wav = self.client.synthesize(effect.text, effect.voice_id or "")
                if not cancellation.is_set():
                    success = self.player(wav, cancellation)
            except Exception:
                success = False
            finally:
                with self._lock:
                    self._jobs.pop(key, None)
                if not cancellation.is_set():
                    self.emit(Envelope(EventKind.TTS_FINISHED, effect.session_id, self.clock(),
                                       line_epoch=effect.line_epoch, cancel_token=effect.cancel_token,
                                       tts_success=success))

        Thread(target=work, name="autoadv-irodori", daemon=True).start()

    def cancel_all(self) -> None:
        with self._lock:
            for ev in self._jobs.values():
                ev.set()