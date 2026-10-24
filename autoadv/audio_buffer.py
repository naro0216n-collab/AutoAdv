"""Timestamped bounded PCM history shared by process-loopback and alignment adapters.

Ring buffer is intentionally independent of any audio library. It does not
classify silence as 'native voice absent'. Upstream per-process loopback must
supply timestamped PCM and health events; device loss clears continuity.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True)
class PCMChunk:
    at_ms: int  # local receiver time at packet receipt (end of captured frames)
    frames: bytes
    sample_rate: int
    channels: int
    sample_width_bytes: int

    @property
    def duration_ms(self) -> float:
        count = len(self.frames) / (self.channels * self.sample_width_bytes)
        return count * 1000 / self.sample_rate


class AudioHistory:
    def __init__(self, horizon_ms: int = 185000):
        if horizon_ms <= 0:
            raise ValueError("audio horizon must be positive")
        self.horizon_ms = horizon_ms
        self._lock = RLock()
        self._chunks: deque[PCMChunk] = deque()
        self.healthy = False
        self.target_pid: int | None = None
        self.fail_reason: str | None = "not_started"
        self._last_ms: int | None = None

    def reset(self, reason: str = "capture_reset") -> None:
        with self._lock:
            self._chunks.clear()
            self.healthy = False
            self.fail_reason = reason
            self._last_ms = None

    def start(self, target_pid: int) -> None:
        if target_pid <= 0:
            raise ValueError("target pid required")
        with self._lock:
            self.reset("not_yet_observed")
            self.target_pid = target_pid

    def push(self, chunk: PCMChunk, *, target_pid: int) -> None:
        if chunk.sample_rate <= 0 or chunk.channels <= 0 or chunk.sample_width_bytes not in (1, 2, 3, 4):
            raise ValueError("invalid PCM format")
        if len(chunk.frames) % (chunk.channels * chunk.sample_width_bytes):
            raise ValueError("unaligned PCM frame")
        with self._lock:
            if target_pid != self.target_pid:
                self.reset("target_pid_changed")
                self.target_pid = target_pid
                return
            if self._last_ms is not None and chunk.at_ms < self._last_ms:
                self.reset("timestamp_regression")
                return
            self._last_ms = chunk.at_ms
            self._chunks.append(chunk)
            self._trim_locked()
            self.healthy = True
            self.fail_reason = None

    def _trim_locked(self) -> None:
        if self._last_ms is None:
            return
        cutoff = self._last_ms - self.horizon_ms
        while self._chunks and self._chunks[0].at_ms < cutoff:
            self._chunks.popleft()

    def since(self, at_ms: int) -> tuple[PCMChunk, ...]:
        """Return captured frames at or after an exact monotonic boundary.

        ``PCMChunk.at_ms`` timestamps the end of a packet.  A Presentation Epoch
        can therefore fall inside the first retained packet; returning that
        packet whole would leak frames from the preceding utterance.
        """
        boundary = int(at_ms)
        result: list[PCMChunk] = []
        with self._lock:
            for chunk in self._chunks:
                if chunk.at_ms <= boundary:
                    continue
                frame_width = chunk.channels * chunk.sample_width_bytes
                total_frames = len(chunk.frames) // frame_width
                retain_frames = min(
                    total_frames,
                    int((chunk.at_ms - boundary) * chunk.sample_rate / 1000),
                )
                if retain_frames <= 0:
                    continue
                if retain_frames == total_frames:
                    result.append(chunk)
                    continue
                result.append(PCMChunk(
                    at_ms=chunk.at_ms,
                    frames=chunk.frames[-retain_frames * frame_width:],
                    sample_rate=chunk.sample_rate,
                    channels=chunk.channels,
                    sample_width_bytes=chunk.sample_width_bytes,
                ))
        return tuple(result)
