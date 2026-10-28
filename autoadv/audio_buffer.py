"""Timestamped bounded PCM timeline for process-loopback audio.

The timeline is the raw acoustic source of truth.  It preserves packet capture
provenance and wall-clock continuity without assigning speech, silence, source,
or Presentation meaning.  Consumers query exact ranges; retention is only a
resource bound and never an analysis window by itself.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import RLock


# Packet starts within this distance of the previous packet end are considered
# one captured span.  This is a transport continuity tolerance, not an
# utterance/endpoint threshold.  Every PCM consumer uses the same rule.
PCM_CONTIGUITY_TOLERANCE_MS = 40.0


@dataclass(frozen=True)
class PCMChunk:
    at_ms: int  # captured-frame end in the Controller monotonic clock domain
    frames: bytes
    sample_rate: int
    channels: int
    sample_width_bytes: int

    @property
    def duration_ms(self) -> float:
        count = len(self.frames) / (self.channels * self.sample_width_bytes)
        return count * 1000 / self.sample_rate

    @property
    def start_ms(self) -> float:
        return float(self.at_ms) - self.duration_ms


@dataclass(frozen=True)
class PCMWindow:
    """One exact PCM query with transport continuity preserved.

    ``continues_from_start`` tells incremental consumers whether the first
    returned raw packet is transport-contiguous with the packet immediately
    preceding ``start_ms``.  Internal discontinuities are represented by
    separate spans.
    """

    spans: tuple[tuple[PCMChunk, ...], ...] = ()
    continues_from_start: bool = False


class AudioHistory:
    """Finite raw PCM timeline with exact range and continuity queries."""

    def __init__(self, horizon_ms: int = 185000):
        if horizon_ms <= 0:
            raise ValueError("audio horizon must be positive")
        self.horizon_ms = int(horizon_ms)
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

    def push(self, chunk: PCMChunk, *, target_pid: int) -> bool:
        if chunk.sample_rate <= 0 or chunk.channels <= 0 or chunk.sample_width_bytes not in (1, 2, 3, 4):
            raise ValueError("invalid PCM format")
        if len(chunk.frames) % (chunk.channels * chunk.sample_width_bytes):
            raise ValueError("unaligned PCM frame")
        with self._lock:
            if target_pid != self.target_pid:
                self.reset("target_pid_changed")
                self.target_pid = target_pid
                return False
            if self._last_ms is not None and chunk.at_ms < self._last_ms:
                self.reset("timestamp_regression")
                return False
            self._last_ms = int(chunk.at_ms)
            self._chunks.append(chunk)
            self._trim_locked()
            self.healthy = True
            self.fail_reason = None
            return True

    def _trim_locked(self) -> None:
        if self._last_ms is None:
            return
        cutoff = self._last_ms - self.horizon_ms
        while self._chunks and self._chunks[0].at_ms < cutoff:
            self._chunks.popleft()

    @property
    def earliest_ms(self) -> int | None:
        """Wall-clock start of the oldest retained PCM packet."""
        with self._lock:
            if not self._chunks:
                return None
            return int(round(self._chunks[0].start_ms))

    @property
    def latest_ms(self) -> int | None:
        """Wall-clock end of the newest retained PCM packet."""
        with self._lock:
            return int(self._chunks[-1].at_ms) if self._chunks else None

    @staticmethod
    def _crop(chunk: PCMChunk, start_ms: int, end_ms: int) -> PCMChunk | None:
        start = max(float(start_ms), chunk.start_ms)
        end = min(float(end_ms), float(chunk.at_ms))
        if end <= start:
            return None
        frame_width = chunk.channels * chunk.sample_width_bytes
        total_frames = len(chunk.frames) // frame_width
        first = max(0, min(
            total_frames,
            int(round((start - chunk.start_ms) * chunk.sample_rate / 1000.0)),
        ))
        last = max(first, min(
            total_frames,
            int(round((end - chunk.start_ms) * chunk.sample_rate / 1000.0)),
        ))
        if last <= first:
            return None
        frames = chunk.frames[first * frame_width:last * frame_width]
        actual_end = chunk.start_ms + last * 1000.0 / chunk.sample_rate
        return PCMChunk(
            at_ms=int(round(actual_end)), frames=frames,
            sample_rate=chunk.sample_rate, channels=chunk.channels,
            sample_width_bytes=chunk.sample_width_bytes,
        )

    def read(self, start_ms: int, end_ms: int | None = None) -> PCMWindow:
        """Return an exact range partitioned by raw transport continuity.

        Continuity is decided from retained raw packet timestamps before range
        cropping.  This keeps an incremental consumer's cursor boundary from
        hiding a gap or a large overlap between the last consumed packet and the
        first newly returned packet.
        """
        start = int(start_ms)
        with self._lock:
            if not self._chunks:
                return PCMWindow()
            end = int(self._chunks[-1].at_ms if end_ms is None else end_ms)
            if end <= start:
                return PCMWindow()

            spans: list[list[PCMChunk]] = []
            current: list[PCMChunk] = []
            previous_raw: PCMChunk | None = None
            continues_from_start = False
            first_returned = True

            for chunk in self._chunks:
                if chunk.at_ms <= start:
                    previous_raw = chunk
                    continue
                if chunk.start_ms >= end:
                    break

                discontinuity = False
                if previous_raw is not None:
                    gap = chunk.start_ms - float(previous_raw.at_ms)
                    discontinuity = abs(gap) > PCM_CONTIGUITY_TOLERANCE_MS

                if first_returned:
                    # If the query begins inside the oldest retained packet there
                    # is no raw boundary to cross at ``start``. Otherwise the raw
                    # predecessor decides whether an incremental stream may
                    # continue without resetting its model state.
                    if previous_raw is None and chunk.start_ms <= start < chunk.at_ms:
                        continues_from_start = True
                    else:
                        continues_from_start = previous_raw is not None and not discontinuity
                    first_returned = False
                elif discontinuity:
                    if current:
                        spans.append(current)
                    current = []

                crop_start = start
                if current:
                    crop_start = max(crop_start, int(current[-1].at_ms))
                cropped = self._crop(chunk, crop_start, end)
                if cropped is not None:
                    current.append(cropped)
                previous_raw = chunk

            if current:
                spans.append(current)
            return PCMWindow(
                spans=tuple(tuple(span) for span in spans),
                continues_from_start=continues_from_start,
            )
