"""Process-scoped target-conditioned native-audio observation.

Windows Process Loopback is continuous across dialogue lines.  For an active
non-TTS audio-judgement Utterance, cumulative PCM is passed to one phoneme-CTC
forced aligner for the known HOOK text.  NativeAudio never owns game input,
presentation completion, or text fallback policy.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import RLock
from typing import Callable
import time

import numpy as np

from .audio_buffer import AudioHistory, PCMChunk
from .audio_knowledge import NativeAudioObservation
from .conversation import Token
from .forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from .process_audio import ProcessCapture, CaptureHealth


class NativeAudio:
    """Own one process capture and one active target; never inject game input."""

    DEFAULT_REPLAY_HISTORY_MS = 10000
    CAPTURE_HISTORY_MARGIN_MS = 1000
    INFERENCE_INTERVAL_MS = 500
    MIN_AUDIO_MS = 300

    def __init__(self, pid: int, helper: Path, model: Path, vocab: Path,
                 on_error: Callable[[Token | None, str], None],
                 *, model_backend=None, g2p=None, clock=None,
                 replay_lookback_ms: int = DEFAULT_REPLAY_HISTORY_MS,
                 on_pcm: Callable[[Token, int], None] | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None,
                 on_observation: Callable[[Token, NativeAudioObservation], None] | None = None):
        self.pid = pid
        self.on_error = on_error
        self.on_pcm = on_pcm
        self.on_inference = on_inference
        self.on_observation = on_observation
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        if replay_lookback_ms < 0:
            raise ValueError("native replay lookback must be nonnegative")
        self.history = AudioHistory(horizon_ms=max(
            self.DEFAULT_REPLAY_HISTORY_MS,
            int(replay_lookback_ms) + self.CAPTURE_HISTORY_MARGIN_MS,
        ))
        self.model = model_backend if model_backend is not None else OnnxPhoneCTCModel(model, vocab)
        if g2p is None:
            from .g2p import default_g2p
            g2p = default_g2p()
        self.g2p = g2p
        self._lock = RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autoadv-align")
        self._capture = ProcessCapture(pid, helper, self.history, self.feed, self.health, self.clock)

        self.token: Token | None = None
        self.aligner: TargetConditionedAligner | None = None
        self.start_ms = 0
        self.boundary_ms = 0
        self.window_start_ms = 0
        self._awaiting_hook = False
        self._last_processed_ms = -1
        self._last_processed_chunk: PCMChunk | None = None
        self._inference_at = -1
        self._inflight = False
        self._inflight_generation: int | None = None
        self._generation = 0
        self._closed = False
        self._capture_pcm_packets = 0
        self._capture_pcm_frames = 0
        self._capture_heartbeats = 0
        self._capture_error: str | None = None
        self._capture_stereo_peak = 0.0
        self._pcm_reported = False
        self._last_pcm_end_ms = -1

    def start(self) -> None:
        # Capture begins before the first HOOK so an early first voice is not
        # discarded while Luna flushes its text.
        self.boundary()
        self._capture.start()
        self.history.retain_from(self.window_start_ms)

    def boundary(self, at_ms: int | None = None) -> None:
        """Invalidate the old generation and open the next continuous interval."""
        boundary_ms = self.clock() if at_ms is None else int(at_ms)
        if boundary_ms <= 0:
            raise ValueError("audio boundary must be a positive monotonic timestamp")
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.aligner = None
            self.boundary_ms = boundary_ms
            self.window_start_ms = boundary_ms
            self._awaiting_hook = True
            self._last_processed_ms = -1
            self._last_processed_chunk = None
            self.history.retain_from(self.window_start_ms)
            self._reset_observation()

    def _reset_observation(self) -> None:
        self._inference_at = -1
        self._pcm_reported = False
        self._last_pcm_end_ms = -1
        self._capture_stereo_peak = 0.0

    def begin(self, token: Token, match_text: str, *, replay_from_ms: int | None = None) -> None:
        """Bind one known target text to retained PCM for this Utterance."""
        aligner = TargetConditionedAligner(self.model, g2p=self.g2p)
        aligner.set_target(match_text)
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._inflight = False
            self._inflight_generation = None
            self.token = token
            self.aligner = aligner
            self.start_ms = self.clock()
            if replay_from_ms is not None:
                self.window_start_ms = max(0, int(replay_from_ms))
            else:
                self.window_start_ms = self.boundary_ms if self._awaiting_hook else self.start_ms
            self._awaiting_hook = False
            self._last_processed_ms = -1
            self._last_processed_chunk = None
            self.history.retain_from(self.window_start_ms)
            self._reset_observation()

            buffered = self.history.since(self.window_start_ms)
            pending = [self._consume_locked(chunk, replay=True) for chunk in buffered]
            if buffered:
                samples = self.history.since(self.window_start_ms)
                if samples:
                    self._inflight = True
                    self._inflight_generation = generation
                    self._inference_at = buffered[-1].at_ms
                    pending.append((token, generation, None, samples, aligner))
        for item in pending:
            self._dispatch(item)
        with self._lock:
            failure = self._capture_error if self.token == token else None
        if failure:
            self.on_error(token, failure)

    def finish_line(self, token: Token) -> bool:
        with self._lock:
            if token != self.token:
                return False
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.aligner = None
            self.history.release_retention()
            self._reset_observation()
            return True

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.aligner = None
            self.history.release_retention()

    def health(self, status: CaptureHealth) -> None:
        observation = None
        token = None
        drain = None
        with self._lock:
            if status.healthy:
                if status.pcm_frames:
                    self._capture_pcm_packets += 1
                    self._capture_pcm_frames += status.pcm_frames
                else:
                    self._capture_heartbeats += 1
                    if self.token is not None and self._last_pcm_end_ms >= 0:
                        observation = NativeAudioObservation(
                            kind="transport",
                            pcm_last_frame_ms=self._last_pcm_end_ms,
                            heartbeat_at_ms=status.at_ms,
                        )
                        token = self.token
                        drain = self._idle_drain_locked()
            else:
                self._capture_error = status.reason
                token = self.token
        if observation is not None and token is not None and self.on_observation is not None:
            self.on_observation(token, observation)
        if drain is not None:
            self._dispatch(drain)
        if not status.healthy and token is not None:
            self.on_error(token, status.reason)

    def _idle_drain_locked(self):
        """Analyze newly captured tail PCM when the transport becomes idle.

        A heartbeat is only a scheduling opportunity.  It is not added to the
        acoustic evidence and does not itself support association or endpoint
        confirmation.  This closes the otherwise unprocessed tail left when a
        short voice ends before the normal 500 ms inference cadence is due.
        """
        if (self._closed or self.token is None or self.aligner is None or self._inflight
                or self._last_pcm_end_ms < 0 or self._last_pcm_end_ms <= self._inference_at):
            return None
        samples = self.history.since(self.window_start_ms)
        if not samples:
            return None
        token, generation, aligner = self.token, self._generation, self.aligner
        self._inflight = True
        self._inflight_generation = generation
        # Keep cadence in the capture clock domain.  A heartbeat time is
        # transport evidence only and must not become an acoustic timestamp.
        self._inference_at = self._last_pcm_end_ms
        return token, generation, None, samples, aligner

    def capture_status(self) -> dict:
        with self._lock:
            return dict(
                scope="capture_since_start",
                pcm_packets=self._capture_pcm_packets,
                pcm_frames=self._capture_pcm_frames,
                heartbeats=self._capture_heartbeats,
                capture_stereo_peak=round(self._capture_stereo_peak, 6),
                failure=self._capture_error,
            )

    @staticmethod
    def _chunk_peak(chunk: PCMChunk) -> float:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            return 0.0
        values = np.frombuffer(chunk.frames, dtype="<f4")
        return float(np.max(np.abs(values))) if values.size else 0.0

    def _consume_locked(self, chunk: PCMChunk, *, replay: bool = False):
        if (self._closed or self.token is None or self.aligner is None
                or chunk.at_ms < self.window_start_ms
                or chunk.at_ms < self._last_processed_ms
                or (chunk.at_ms == self._last_processed_ms and chunk is self._last_processed_chunk)):
            return None
        self._last_processed_ms = chunk.at_ms
        self._last_processed_chunk = chunk
        self._last_pcm_end_ms = max(self._last_pcm_end_ms, int(chunk.at_ms))
        self._capture_stereo_peak = max(self._capture_stereo_peak, self._chunk_peak(chunk))
        pcm_frames = None
        if not self._pcm_reported:
            self._pcm_reported = True
            pcm_frames = len(chunk.frames) // (chunk.channels * chunk.sample_width_bytes)

        samples = None
        token, generation, aligner = self.token, self._generation, self.aligner
        if not replay:
            enough_pcm = chunk.at_ms - self.window_start_ms >= self.MIN_AUDIO_MS
            inference_due = (self._inference_at < 0
                             or chunk.at_ms - self._inference_at >= self.INFERENCE_INTERVAL_MS)
            if not self._inflight and enough_pcm and inference_due:
                candidate = self.history.since(self.window_start_ms)
                if candidate:
                    self._inflight = True
                    self._inflight_generation = generation
                    self._inference_at = chunk.at_ms
                    samples = candidate
        return token, generation, pcm_frames, samples, aligner

    def _dispatch(self, item) -> None:
        if item is None:
            return
        token, generation, pcm_frames, samples, aligner = item
        with self._lock:
            if generation != self._generation or token != self.token:
                return
        if pcm_frames is not None and self.on_pcm is not None:
            self.on_pcm(token, pcm_frames)
        if samples:
            submitted_ns = time.perf_counter_ns() if self.on_inference else 0
            try:
                self._pool.submit(self._infer, token, generation, samples, aligner, submitted_ns)
            except RuntimeError:
                with self._lock:
                    if self._inflight_generation == generation:
                        self._inflight = False
                        self._inflight_generation = None

    def feed(self, chunk: PCMChunk) -> None:
        with self._lock:
            item = self._consume_locked(chunk)
        self._dispatch(item)

    @staticmethod
    def _audio_offset_to_wall_ms(samples: tuple[PCMChunk, ...], offset_ms: float) -> float | None:
        """Map concatenated-model time back to captured packet time without inventing gaps."""
        if offset_ms < 0:
            return None
        consumed = 0.0
        for chunk in samples:
            duration = chunk.duration_ms
            if offset_ms <= consumed + duration + 1e-6:
                inside = max(0.0, min(duration, offset_ms - consumed))
                return (chunk.at_ms - duration) + inside
            consumed += duration
        return None

    def _infer(self, token: Token, generation: int, samples: tuple[PCMChunk, ...],
               aligner: TargetConditionedAligner, submitted_ns: int = 0) -> None:
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        try:
            alignment = aligner.align(samples)
            decoded_ns = time.perf_counter_ns() if self.on_inference else 0
            audio_ms = sum(chunk.duration_ms for chunk in samples)
            capture_start_ms = int(round(samples[0].at_ms - samples[0].duration_ms)) if samples else None
            capture_end_ms = int(samples[-1].at_ms) if samples else None
            target_end_ms = None
            if alignment.target_end_frame is not None:
                end_offset = alignment.target_end_frame * alignment.frame_ms
                wall = self._audio_offset_to_wall_ms(samples, end_offset)
                if wall is not None:
                    target_end_ms = int(round(wall))

            observation = NativeAudioObservation(
                kind="alignment",
                alignment=alignment,
                capture_start_ms=capture_start_ms,
                capture_end_ms=capture_end_ms,
                target_acoustic_end_ms=target_end_ms,
                pcm_last_frame_ms=capture_end_ms,
            )
            with self._lock:
                if (self._closed or generation != self._generation or token != self.token
                        or aligner is not self.aligner or not self.history.healthy):
                    return

            if self.on_inference is not None:
                fit = alignment.fit
                metrics = {
                    "queue_ms": round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    "align_ms": round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    "audio_ms": round(audio_ms, 3),
                    "frame_count": alignment.frame_count,
                    "target_units": alignment.target_units,
                    "aligned_target_units": alignment.aligned_target_units,
                    "target_coverage": round(alignment.coverage, 4),
                    "target_associated": alignment.target_associated,
                    "complete_alignment": alignment.complete_alignment,
                    "committed_end": alignment.committed_end,
                    "target_end_frame": alignment.target_end_frame,
                    "post_target_free_frames": alignment.post_target_free_frames,
                    "reading_rank": alignment.reading_rank,
                    "path_margin": round(fit.path_margin, 4),
                    "mean_unit_peak_margin": round(fit.mean_unit_peak_margin, 4),
                    "min_unit_peak_margin": round(fit.min_unit_peak_margin, 4),
                    "weak_units": fit.weak_units,
                    "boundary_margin": round(fit.boundary_margin, 4),
                    "capture_end_ms": capture_end_ms,
                    "target_acoustic_end_ms": target_end_ms,
                    "rejection": alignment.rejection,
                }
                try:
                    self.on_inference(token, metrics)
                except Exception:
                    pass
            if self.on_observation is not None:
                self.on_observation(token, observation)
        except Exception as exc:
            self.on_error(token, "alignment failure: " + str(exc))
        finally:
            with self._lock:
                if self._inflight_generation == generation:
                    self._inflight = False
                    self._inflight_generation = None

    def close(self) -> None:
        self.cancel()
        with self._lock:
            self._closed = True
        self._capture.close()
        self._pool.shutdown(wait=True, cancel_futures=True)
