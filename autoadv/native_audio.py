"""Process-scoped native audio recognition for non-TTS audio-judgment utterances.

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
                 *, recognizer=None, activity=None, clock=None, vad_only: bool = False,
                 on_pcm: Callable[[Token, int], None] | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None):
        self.pid = pid
        self.notify = notify
        self.on_pcm = on_pcm
        self.on_inference = on_inference
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.history = AudioHistory(horizon_ms=10000)
        self.vad_only = vad_only
        self.asr = None if vad_only else (recognizer if recognizer is not None else JapaneseASR(model, tokens))
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
        self._inflight_generation: int | None = None
        self._generation = 0
        self._closed = False
        self._last_vad = False
        self._vad_voicing_since = -1
        self._pcm_reported = False
        self._last_active_ms = -1
        self._quiet_notified = False

    def start(self):
        self._capture.start()

    def boundary(self):
        with self._lock:
            self.boundary_ms = self.clock()

    def begin(self, token: Token, match_text: str):
        with self._lock:
            self._generation += 1
            # ASR decoding from the prior utterance may still be running. It
            # cannot own this utterance's inference slot or later clear it.
            self._inflight = False
            self._inflight_generation = None
            self.token = token
            self.matcher = TargetMatcher(match_text)
            self.start_ms = self.clock()
            self.active = self.attributed = self.completed = False
            self._inference_at = -1
            self._vad_voicing_since = -1
            self._pcm_reported = False
            self._last_vad = False
            self.rise_ms = -1
            self._last_active_ms = -1
            self._quiet_notified = False
            if hasattr(self.activity, 'reset'):
                self.activity.reset()

    def is_confirmed_playing(self, token: Token) -> bool:
        """Positive *recent* correlated VAD activity; never proof of voice absence.

        Old PCM or a heartbeat cannot keep a line active indefinitely. The
        1000ms window is a freshness bound only, not an end-of-speech threshold.
        """
        with self._lock:
            return bool(token == self.token and self.attributed and self._last_vad
                        and self._last_active_ms >= 0
                        and self.clock() - self._last_active_ms <= 1000)

    def cancel(self):
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.matcher = None

    def health(self, status: CaptureHealth):
        if not status.healthy:
            with self._lock:
                token = self.token
            self.notify(token, 'error', status.reason)

    def feed(self, chunk: PCMChunk):
        with self._lock:
            if self._closed or self.token is None:
                return  # no ASR/VAD inference for TTS and text-only lines
        try:
            activity = self.activity.observe_float32_stereo(chunk.frames)
        except Exception as exc:
            self.notify(self.token, 'error', 'VAD failure: ' + str(exc))
            return
        events: list[str] = []
        with self._lock:
            if self._closed:
                return
            if activity.frames_analyzed and self.token is not None and not self._pcm_reported:
                self._pcm_reported = True
                # A real process-loopback PCM packet reached production analysis.
                # This is evidence of capture, not evidence of matching speech.
                if self.on_pcm is not None:
                    self.on_pcm(self.token, len(chunk.frames) // 8)
            if activity.frames_analyzed:
                if (activity.active or activity.contains_speech) and not self._last_vad:
                    self.rise_ms = chunk.at_ms
                self._last_vad = activity.active
            self.tail_ms = activity.tail_silence_ms
            if activity.frames_analyzed and activity.active and self.attributed:
                self._last_active_ms = self.clock()
                self._quiet_notified = False
            elif (activity.frames_analyzed and not activity.active and self.attributed
                  and activity.tail_silence_ms >= 400 and not self._quiet_notified
                  and (not self.vad_only or activity.tail_silence_ms < 450)):
                self._quiet_notified = True
                events.append('quiet')
            if self.vad_only:
                # Only explicitly configured voice-only games may treat VAD as
                # correlated evidence; background sounds remain ambiguous.
                token = self.token
                if token is None or not activity.frames_analyzed or self.completed:
                    return
                if activity.active:
                    if self._vad_voicing_since < 0:
                        self._vad_voicing_since = chunk.at_ms
                    if not self.attributed and chunk.at_ms - self._vad_voicing_since >= 350:
                        self.attributed = True
                        self._last_active_ms = self.clock()
                        events.append('started')
                elif not self.attributed:
                    self._vad_voicing_since = -1
                if self.attributed and activity.tail_silence_ms >= 450:
                    self.completed = True
                    events.append('completed')
            else:
                if (self.token is None or self.completed or not activity.frames_analyzed):
                    return
                if self._inflight or (self._inference_at >= 0 and chunk.at_ms - self._inference_at < 500):
                    for event in events:
                        self.notify(self.token, event, '')
                    return
                token, generation = self.token, self._generation
                self._inflight = True
                self._inflight_generation = generation
                self._inference_at = chunk.at_ms
                samples = self.history.since(max(self.boundary_ms, self.start_ms - 2000))
        if self.vad_only:
            for event in events:
                self.notify(token, event, '')
        else:
            submitted_ns = time.perf_counter_ns() if self.on_inference else 0
            for event in events:
                self.notify(token, event, '')
            self._pool.submit(self._infer, token, generation, samples, submitted_ns)

    def _infer(self, token, generation, samples, submitted_ns=0):
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        try:
            hypothesis = self.asr.recognize(samples)
            decoded_ns = time.perf_counter_ns() if self.on_inference else 0
            events = []
            with self._lock:
                if (self._closed or self._generation != generation or token != self.token
                        or not self.matcher or not self.history.healthy):
                    return
                progress = self.matcher.observe(hypothesis)
                recent = self.rise_ms >= max(self.boundary_ms, self.start_ms - 2000)
                if not self.attributed and recent and progress.matched_chars >= 2 and progress.onset_score >= .85:
                    self.attributed = True
                    self._last_active_ms = self.clock()
                    events.append('started')
                if (self.attributed and progress.monotonic and progress.coverage >= .90
                        and progress.tail_confidence >= .90 and self.tail_ms >= 300):
                    self.completed = True
                    events.append('completed')
            if self.on_inference:
                audio_ms = sum(chunk.duration_ms for chunk in samples)
                measurements = {
                    'queue_ms': round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    'decode_ms': round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    'audio_ms': round(audio_ms, 3),
                    'coverage': round(progress.coverage, 4),
                    'monotonic': progress.monotonic,
                    'tail_confidence': progress.tail_confidence,
                }
                try:
                    self.on_inference(token, measurements)
                except Exception:
                    pass  # Optional diagnostics must never affect speech recognition.
            for event in events:
                self.notify(token, event, '')
        except Exception as exc:
            self.notify(token, 'error', 'ASR failure: ' + str(exc))
        finally:
            with self._lock:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None

    def close(self):
        self.cancel()
        with self._lock:
            self._closed = True
        self._capture.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
