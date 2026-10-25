"""Continuous, low-cost acoustic observations for utterance progression.

Observers in this module report only what they measure.  They do not decide
whether a dialogue line has completed and never own game input.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import math

import numpy as np

from .audio_buffer import PCMChunk
from .forced_alignment import _downsample_48k_to_16k
from .utterance_progress import HumanVocalEvidence, VocalEvidenceSeries, VocalRecurrenceEvidence


@dataclass(frozen=True)
class VocalActivityFrame:
    """Probability that one observed time span contains human vocal activity.

    ``support_start_ms`` is optional because low-latency VAD backends naturally
    report narrow frames, while event classifiers use a wider receptive field.
    It records input provenance only: downstream code must not reinterpret the
    whole support span as time during which the vocal event was active.
    """

    at_ms: int
    probability: float
    support_start_ms: int | None = None
    source: str = "speech"


@dataclass(frozen=True)
class AcousticFingerprint:
    """Target-independent spectral measurement for one PCM packet."""

    at_ms: int
    values: tuple[float, ...]
    rms: float


class VocalActivityObserver(Protocol):
    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]: ...


class AudioEventModel(Protocol):
    def infer(self, waveform: np.ndarray) -> np.ndarray: ...


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
            frame_start = int(round(start_ms + index * hop_ms))
            frames.append(VocalActivityFrame(
                at_ms=int(round(start_ms + (index + 1) * hop_ms)),
                probability=max(0.0, min(1.0, float(probability))),
                support_start_ms=frame_start, source="speech",
            ))

        consumed = full * self.HOP_SIZE
        self._tail = np.ascontiguousarray(waveform[consumed:], dtype=np.float32)
        self._tail_start_ms = start_ms + consumed * 1000.0 / self.SAMPLE_RATE
        return tuple(frames)


# YAMNet / AudioSet classes that directly describe a human vocal or respiratory
# sound.  Crowd/hubbub classes are intentionally excluded: a process mix can
# contain them as background, and current-event ownership remains a separate
# temporal/lexical inference problem.
AUDIOSET_HUMAN_VOCAL_CLASSES = tuple(range(0, 35)) + tuple(range(36, 46))


class OnnxAudioSetModel:
    """Small ONNX adapter for the pinned raw-waveform YAMNet conversion."""

    def __init__(self, model: str | Path, *, num_threads: int = 1):
        path = Path(model)
        if not path.is_file():
            raise FileNotFoundError(f"configured human-vocal model required: {path}")
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(num_threads))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(path), sess_options=options, providers=['CPUExecutionProvider'])
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if len(inputs) != 1 or not outputs:
            raise ValueError("unsupported human-vocal ONNX signature")
        self.input_name = inputs[0].name
        self.output_name = outputs[0].name

    def infer(self, waveform: np.ndarray) -> np.ndarray:
        values = np.ascontiguousarray(waveform, dtype=np.float32)
        if values.ndim != 1 or not values.size:
            raise ValueError("human-vocal model requires one non-empty mono waveform")
        scores = np.asarray(
            self.session.run([self.output_name], {self.input_name: values})[0],
            dtype=np.float32)
        if scores.ndim != 2 or scores.shape[1] != 521 or not scores.shape[0]:
            raise ValueError("human-vocal model returned unsupported AudioSet scores")
        return scores[-1]


class AudioSetHumanVocalObserver:
    """Streaming non-lexical human-vocal observation from AudioSet scores.

    YAMNet's receptive field is wider than TEN-VAD's speech frames.  Each output
    therefore carries its factual input span instead of pretending to provide a
    precise voice onset/endpoint.  Short inputs are intentionally allowed; the
    pinned model zero-pads them and this keeps cries/pants observable before a
    full 0.96 s patch has accumulated.
    """

    SAMPLE_RATE = 16000
    WINDOW_SAMPLES = 15_360  # 0.96 s, YAMNet's native receptive field.
    HOP_SAMPLES = 3_840      # 0.24 s product observation cadence.
    GAP_TOLERANCE_MS = 40.0

    def __init__(self, model: AudioEventModel):
        self.model = model
        self._waveform = np.empty(0, dtype=np.float32)
        self._start_ms: float | None = None
        self._next_emit = self.HOP_SAMPLES

    @staticmethod
    def _waveform_16k(chunk: PCMChunk) -> np.ndarray:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            raise ValueError("AudioSet observer requires 48 kHz stereo float32 PCM")
        stereo = np.frombuffer(chunk.frames, dtype="<f4").reshape(-1, 2)
        mono = stereo.mean(axis=1, dtype=np.float32)
        return _downsample_48k_to_16k(mono)

    def _reset(self, waveform: np.ndarray, start_ms: float) -> None:
        self._waveform = np.ascontiguousarray(waveform, dtype=np.float32)
        self._start_ms = float(start_ms)
        self._next_emit = self.HOP_SAMPLES

    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]:
        waveform = self._waveform_16k(chunk)
        if not waveform.size:
            return ()
        chunk_start = float(chunk.at_ms) - chunk.duration_ms
        if self._start_ms is None or not self._waveform.size:
            self._reset(waveform, chunk_start)
        else:
            expected = self._start_ms + self._waveform.size * 1000.0 / self.SAMPLE_RATE
            if abs(chunk_start - expected) > self.GAP_TOLERANCE_MS:
                # Absence of process-loopback PCM is not fabricated as silence.
                # Start a new contiguous observation span instead.
                self._reset(waveform, chunk_start)
            else:
                self._waveform = np.concatenate((self._waveform, waveform))

        assert self._start_ms is not None
        frames: list[VocalActivityFrame] = []
        while self._next_emit <= self._waveform.size:
            end = self._next_emit
            start = max(0, end - self.WINDOW_SAMPLES)
            scores = np.asarray(self.model.infer(self._waveform[start:end]), dtype=np.float32)
            if scores.ndim != 1 or scores.size < 521:
                raise ValueError("human-vocal event model must return one 521-class score vector")
            probability = max(float(scores[index]) for index in AUDIOSET_HUMAN_VOCAL_CLASSES)
            at_ms = int(round(self._start_ms + end * 1000.0 / self.SAMPLE_RATE))
            support_start = int(round(self._start_ms + start * 1000.0 / self.SAMPLE_RATE))
            frames.append(VocalActivityFrame(
                at_ms=at_ms,
                probability=max(0.0, min(1.0, probability)),
                support_start_ms=support_start, source="nonlexical",
            ))
            self._next_emit += self.HOP_SAMPLES

        # Keep only what the next receptive field can still consume. This is a
        # streaming resource bound, not a semantic time boundary.
        removable = max(0, self._next_emit - self.WINDOW_SAMPLES - self.HOP_SAMPLES)
        if removable:
            self._waveform = np.ascontiguousarray(self._waveform[removable:], dtype=np.float32)
            self._start_ms += removable * 1000.0 / self.SAMPLE_RATE
            self._next_emit -= removable
        return tuple(frames)


class HumanVocalActivityObserver:
    """One product observer combining speech and non-lexical vocal evidence."""

    def __init__(self, event_model, *, speech_observer: VocalActivityObserver | None = None):
        self.speech_observer = speech_observer or TenVadActivityObserver()
        model = event_model if hasattr(event_model, 'infer') else OnnxAudioSetModel(event_model)
        self.event_observer = AudioSetHumanVocalObserver(model)

    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]:
        frames = self.speech_observer.observe(chunk) + self.event_observer.observe(chunk)
        return tuple(sorted(
            frames, key=lambda frame: (
                frame.at_ms, frame.support_start_ms if frame.support_start_ms is not None else frame.at_ms)))


class AcousticObservationHistory:
    """Target-independent measurement cache over the retained audio timeline.

    The cache is only a cheaper representation of PCM.  It does not store
    utterance/event/source identity and every semantic assessment is rebuilt for
    the requested time range.
    """

    ACTIVE_PROBABILITY = {
        "speech": 0.45,
        "nonlexical": 0.45,
    }
    FINGERPRINT_BANDS = 12
    LOW_VOCAL_REFERENCE_MS = 8_000

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

    def add_fingerprint(self, chunk: PCMChunk) -> None:
        values = self._spectral_fingerprint(chunk)
        if values is None:
            return
        raw = np.frombuffer(chunk.frames, dtype="<f4").reshape(-1, 2)
        mono = raw.mean(axis=1, dtype=np.float32)
        rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64)))) if mono.size else 0.0
        at_ms = int(chunk.at_ms)
        self._fingerprints.append(AcousticFingerprint(
            at_ms=at_ms, values=values, rms=rms,
        ))
        self._trim(at_ms)

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

    @classmethod
    def _threshold(cls, source: str) -> float:
        return float(cls.ACTIVE_PROBABILITY.get(source, 0.45))

    @staticmethod
    def _series_intervals(frames: list[VocalActivityFrame], *, start_ms: int,
                          end_ms: int, threshold: float) -> tuple[tuple[int, int], ...]:
        active = [frame for frame in frames if frame.probability >= threshold]
        if not active:
            return ()
        gaps = [b.at_ms - a.at_ms for a, b in zip(frames, frames[1:]) if b.at_ms > a.at_ms]
        nominal_gap = float(np.median(gaps)) if gaps else 16.0
        join_gap = max(32.0, nominal_gap * 2.0)
        intervals: list[tuple[int, int]] = []
        for frame in active:
            observed_at = min(end_ms, max(start_ms, int(frame.at_ms)))
            if intervals and observed_at - intervals[-1][1] <= join_gap:
                intervals[-1] = (intervals[-1][0], max(intervals[-1][1], observed_at))
            else:
                intervals.append((observed_at, observed_at))
        return tuple(intervals)

    def summarize_vocal(self, *, start_ms: int, end_ms: int) -> HumanVocalEvidence:
        selected = [frame for frame in self._vocal if start_ms <= frame.at_ms <= end_ms]
        by_source: dict[str, list[VocalActivityFrame]] = {}
        for frame in selected:
            by_source.setdefault(frame.source, []).append(frame)
        series: list[VocalEvidenceSeries] = []
        for source, frames in sorted(by_source.items()):
            threshold = self._threshold(source)
            active_count = sum(frame.probability >= threshold for frame in frames)
            series.append(VocalEvidenceSeries(
                source=source,
                frame_count=len(frames),
                active_count=active_count,
                peak_probability=max((frame.probability for frame in frames), default=0.0),
                active_intervals=self._series_intervals(
                    frames, start_ms=start_ms, end_ms=end_ms, threshold=threshold),
            ))
        return HumanVocalEvidence(
            start_ms=int(start_ms), end_ms=int(end_ms), series=tuple(series))

    def _active_near(self, at_ms: int, *, tolerance_ms: int = 320) -> bool:
        left = at_ms - tolerance_ms
        right = at_ms + tolerance_ms
        return any(
            left <= frame.at_ms <= right and frame.probability >= self._threshold(frame.source)
            for frame in self._vocal
        )

    @staticmethod
    def _spectral_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        return max(0.0, min(1.0, float(np.dot(a, b))))

    @staticmethod
    def _level_consistency(a: AcousticFingerprint, b: AcousticFingerprint) -> float:
        if max(a.rms, b.rms) <= 1e-12:
            return 1.0
        return max(0.0, min(1.0, min(a.rms, b.rms) / max(a.rms, b.rms)))

    def recurrence(self, *, start_ms: int, end_ms: int) -> VocalRecurrenceEvidence:
        # Recurrence is measured only at time points where at least one human-
        # vocal observer has positive support.  The score scales themselves are
        # not averaged or multiplied into spectral similarity.
        current = [
            fp for fp in self._fingerprints
            if start_ms <= fp.at_ms <= end_ms and self._active_near(fp.at_ms)
        ]
        if len(current) < 3:
            return VocalRecurrenceEvidence()

        pairs: list[tuple[float, float, int]] = []
        for index, later in enumerate(current):
            for earlier in current[:index]:
                lag = later.at_ms - earlier.at_ms
                if lag < 300:
                    continue
                spectral = self._spectral_similarity(later.values, earlier.values)
                level = self._level_consistency(later, earlier)
                if spectral >= 0.65 and level >= 0.40:
                    pairs.append((spectral, level, lag))

        if not pairs:
            return VocalRecurrenceEvidence()

        pairs.sort(key=lambda item: item[0], reverse=True)
        top = pairs[:min(16, len(pairs))]
        strength = float(np.mean([spectral for spectral, _, _ in top]))
        level_consistency = float(np.mean([level for _, level, _ in top]))
        lags = np.asarray([lag for _, _, lag in top], dtype=np.float64)
        period = int(round(float(np.median(lags))))
        spread = float(np.median(np.abs(lags - period))) if len(lags) else 0.0
        stability = max(0.0, 1.0 - spread / max(250.0, float(period)))

        recent = [fp for fp in current if fp.at_ms >= end_ms - 1500]
        reference = [fp for fp in current if fp.at_ms < end_ms - 300]
        recent_scores: list[float] = []
        for now in recent:
            candidates = [before for before in reference if before.at_ms <= now.at_ms - 300]
            if candidates:
                recent_scores.append(max(
                    self._spectral_similarity(now.values, before.values)
                    for before in candidates
                ))
        recent_strength = float(np.mean(recent_scores)) if recent_scores else 0.0

        low_vocal_refs = [
            fp for fp in self._fingerprints
            if start_ms - self.LOW_VOCAL_REFERENCE_MS <= fp.at_ms < end_ms
            and not self._active_near(fp.at_ms)
        ]
        low_vocal_similarity = 0.0
        if low_vocal_refs and recent:
            low_vocal_similarity = max(
                self._spectral_similarity(now.values, ref.values)
                for now in recent for ref in low_vocal_refs
            )

        return VocalRecurrenceEvidence(
            strength=strength,
            period_ms=period,
            period_stability=stability,
            recent_strength=recent_strength,
            level_consistency=level_consistency,
            low_vocal_reference_similarity=low_vocal_similarity,
        )
