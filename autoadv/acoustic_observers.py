"""Continuous, low-cost acoustic observations for utterance progression.

Observers in this module report only what they measure.  They do not decide
whether a dialogue line has completed and never own game input.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol
import math

import numpy as np

from .audio_buffer import PCMChunk
from .forced_alignment import _downsample_48k_to_16k
from .utterance_progress import HumanVocalEvidence, VocalRecurrenceEvidence


@dataclass(frozen=True)
class VocalActivityFrame:
    """Frame-level probability that the process mix contains human vocal activity."""

    at_ms: int
    probability: float


@dataclass(frozen=True)
class AcousticFingerprint:
    """Compact spectral representation with an independent vocal relevance weight."""

    at_ms: int
    values: tuple[float, ...]
    vocal_relevance: float


class VocalActivityObserver(Protocol):
    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]: ...


class TenVadActivityObserver:
    """Streaming TEN-VAD adapter for 48 kHz stereo Process Loopback PCM.

    TEN-VAD is deliberately used only as a probability observer.  Its binary
    flag is not exposed as an utterance endpoint.
    """

    SAMPLE_RATE = 16000
    HOP_SIZE = 256

    def __init__(self, *, threshold: float = 0.5, backend=None):
        if backend is None:
            from ten_vad import TenVad
            backend = TenVad(self.HOP_SIZE, float(threshold))
        self.backend = backend
        self._tail = np.empty(0, dtype=np.float32)
        self._tail_start_ms: float | None = None

    @staticmethod
    def _waveform(chunk: PCMChunk) -> np.ndarray:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            raise ValueError("TEN-VAD observer requires 48 kHz stereo float32 PCM")
        stereo = np.frombuffer(chunk.frames, dtype="<f4").reshape(-1, 2)
        mono = stereo.mean(axis=1, dtype=np.float32)
        return _downsample_48k_to_16k(mono)

    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]:
        waveform = self._waveform(chunk)
        chunk_start = float(chunk.at_ms) - chunk.duration_ms
        if self._tail.size:
            start_ms = self._tail_start_ms if self._tail_start_ms is not None else chunk_start
            waveform = np.concatenate((self._tail, waveform))
        else:
            start_ms = chunk_start

        full = waveform.size // self.HOP_SIZE
        frames: list[VocalActivityFrame] = []
        hop_ms = self.HOP_SIZE * 1000.0 / self.SAMPLE_RATE
        for index in range(full):
            raw = waveform[index * self.HOP_SIZE:(index + 1) * self.HOP_SIZE]
            pcm16 = np.ascontiguousarray(
                np.clip(raw, -1.0, 1.0) * 32767.0, dtype=np.int16)
            probability, _flag = self.backend.process(pcm16)
            frames.append(VocalActivityFrame(
                at_ms=int(round(start_ms + (index + 1) * hop_ms)),
                probability=max(0.0, min(1.0, float(probability))),
            ))

        consumed = full * self.HOP_SIZE
        self._tail = np.ascontiguousarray(waveform[consumed:], dtype=np.float32)
        self._tail_start_ms = start_ms + consumed * 1000.0 / self.SAMPLE_RATE
        return tuple(frames)


class AcousticObservationHistory:
    """Compact continuous feature history independent of HOOK arrival."""

    ACTIVE_PROBABILITY = 0.45
    FINGERPRINT_BANDS = 12

    def __init__(self, *, horizon_ms: int = 10 * 60 * 1000):
        self.horizon_ms = int(horizon_ms)
        self._vocal: deque[VocalActivityFrame] = deque()
        self._fingerprints: deque[AcousticFingerprint] = deque()

    def reset(self) -> None:
        self._vocal.clear()
        self._fingerprints.clear()

    @property
    def latest_ms(self) -> int | None:
        values = []
        if self._vocal:
            values.append(self._vocal[-1].at_ms)
        if self._fingerprints:
            values.append(self._fingerprints[-1].at_ms)
        return max(values) if values else None

    @property
    def earliest_ms(self) -> int | None:
        values = []
        if self._vocal:
            values.append(self._vocal[0].at_ms)
        if self._fingerprints:
            values.append(self._fingerprints[0].at_ms)
        return min(values) if values else None

    def add_vocal(self, frames: tuple[VocalActivityFrame, ...]) -> None:
        if not frames:
            return
        self._vocal.extend(frames)
        self._trim(frames[-1].at_ms)

    def add_fingerprint(self, chunk: PCMChunk, *, vocal_relevance: float) -> None:
        values = self._spectral_fingerprint(chunk)
        if values is None:
            return
        self._fingerprints.append(AcousticFingerprint(
            at_ms=int(chunk.at_ms), values=values,
            vocal_relevance=max(0.0, min(1.0, float(vocal_relevance))),
        ))
        self._trim(int(chunk.at_ms))

    def _trim(self, now_ms: int) -> None:
        cutoff = int(now_ms) - self.horizon_ms
        while self._vocal and self._vocal[0].at_ms < cutoff:
            self._vocal.popleft()
        while self._fingerprints and self._fingerprints[0].at_ms < cutoff:
            self._fingerprints.popleft()

    @classmethod
    def _spectral_fingerprint(cls, chunk: PCMChunk) -> tuple[float, ...] | None:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            return None
        stereo = np.frombuffer(chunk.frames, dtype="<f4")
        if stereo.size < 64:
            return None
        mono = stereo.reshape(-1, 2).mean(axis=1, dtype=np.float32)
        waveform = _downsample_48k_to_16k(mono)
        if waveform.size < 128:
            return None
        # A compact log-spectrum is sufficient for recurrence; it is not a
        # semantic classifier and is always weighted by vocal relevance later.
        size = min(4096, waveform.size)
        segment = waveform[-size:]
        windowed = segment * np.hanning(segment.size)
        power = np.abs(np.fft.rfft(windowed)) ** 2
        if not np.isfinite(power).all() or float(power.sum()) <= 1e-12:
            return tuple(0.0 for _ in range(cls.FINGERPRINT_BANDS))
        edges = np.geomspace(1, max(2, power.size - 1), cls.FINGERPRINT_BANDS + 1).astype(int)
        bands = []
        for left, right in zip(edges[:-1], edges[1:]):
            right = max(left + 1, right)
            bands.append(math.log1p(float(power[left:right].mean())))
        vector = np.asarray(bands, dtype=np.float64)
        vector -= vector.mean()
        norm = float(np.linalg.norm(vector))
        if norm > 1e-9:
            vector /= norm
        return tuple(float(x) for x in vector)

    def summarize_vocal(self, *, start_ms: int, end_ms: int,
                        epoch_start_ms: int | None = None) -> HumanVocalEvidence:
        frames = [f for f in self._vocal if start_ms <= f.at_ms <= end_ms]
        probabilities = [f.probability for f in frames]
        active = [f for f in frames if f.probability >= self.ACTIVE_PROBABILITY]
        pre_epoch: list[VocalActivityFrame] = []
        if epoch_start_ms is not None:
            # Look behind a boundary only to describe persistence; the interval
            # is not used as an invented start time for the new utterance.
            pre_start = max(0, epoch_start_ms - 1500)
            pre_epoch = [f for f in self._vocal if pre_start <= f.at_ms < epoch_start_ms]
        return HumanVocalEvidence(
            start_ms=int(start_ms), end_ms=int(end_ms), frame_count=len(frames),
            mean_probability=float(np.mean(probabilities)) if probabilities else 0.0,
            peak_probability=max(probabilities, default=0.0),
            active_fraction=(len(active) / len(frames)) if frames else 0.0,
            first_active_ms=(active[0].at_ms if active else None),
            last_active_ms=(active[-1].at_ms if active else None),
            pre_epoch_active_fraction=(
                sum(f.probability >= self.ACTIVE_PROBABILITY for f in pre_epoch) / len(pre_epoch)
                if pre_epoch else 0.0),
        )

    @staticmethod
    def _similarity(a: AcousticFingerprint, b: AcousticFingerprint) -> float:
        if not a.values or not b.values or len(a.values) != len(b.values):
            return 0.0
        return max(0.0, min(1.0, float(np.dot(a.values, b.values)))) * min(
            a.vocal_relevance, b.vocal_relevance)

    def recurrence(self, *, epoch_start_ms: int | None, end_ms: int) -> VocalRecurrenceEvidence:
        current = [f for f in self._fingerprints
                   if (epoch_start_ms is None or f.at_ms >= epoch_start_ms) and f.at_ms <= end_ms
                   and f.vocal_relevance >= 0.25]
        if len(current) < 3:
            return VocalRecurrenceEvidence()

        pairs: list[tuple[float, int]] = []
        for i, later in enumerate(current):
            for earlier in current[:i]:
                lag = later.at_ms - earlier.at_ms
                if lag < 300:
                    continue
                score = self._similarity(later, earlier)
                if score >= 0.55:
                    pairs.append((score, lag))
        if not pairs:
            strength = 0.0
            period = None
            stability = 0.0
        else:
            pairs.sort(reverse=True)
            top = pairs[:min(12, len(pairs))]
            strength = float(np.mean([score for score, _ in top]))
            lags = np.asarray([lag for _, lag in top], dtype=np.float64)
            period = int(round(float(np.median(lags))))
            spread = float(np.median(np.abs(lags - period))) if len(lags) else 0.0
            stability = max(0.0, 1.0 - spread / max(250.0, float(period)))

        cross = 0.0
        previous: list[AcousticFingerprint] = []
        if epoch_start_ms is not None:
            previous = [f for f in self._fingerprints
                        if f.at_ms < epoch_start_ms and f.at_ms >= epoch_start_ms - 8000
                        and f.vocal_relevance >= 0.25]
            if previous:
                scores = [self._similarity(now, before)
                          for now in current[-6:] for before in previous[-12:]]
                cross = max(scores, default=0.0)

        # A historical loop classification is not enough to decide what the
        # post-target vocal tail currently contains.  Describe whether recent
        # vocal-weighted fingerprints themselves still match an older pattern.
        # A new finite cry/breath tail can therefore lower this value even when
        # a persistent source was observed earlier in the epoch.
        recent = [f for f in current if f.at_ms >= end_ms - 1500]
        reference = previous + [f for f in current if f.at_ms < end_ms - 300]
        best_recent: list[float] = []
        for now in recent:
            candidates = [before for before in reference if before.at_ms <= now.at_ms - 300]
            if candidates:
                best_recent.append(max(self._similarity(now, before) for before in candidates))
        recent_strength = float(np.mean(best_recent)) if best_recent else 0.0
        return VocalRecurrenceEvidence(
            strength=strength, period_ms=period,
            period_stability=stability, cross_epoch_persistence=cross,
            recent_strength=recent_strength)
