"""Continuous target-independent acoustic measurements.

Observers report only model-local measurements tied to the PCM time ranges that
produced them.  They never own Presentation state or advancement decisions.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio_buffer import PCMChunk
from .forced_alignment import _downsample_48k_to_16k
from .utterance_progress import (
    HumanVocalEvidence, VocalEvidenceSeries, VocalRecurrenceEvidence, VocalScoreSample,
)


@dataclass(frozen=True)
class VocalActivityFrame:
    """One observer score with the exact input support that produced it."""

    at_ms: int
    score: float
    support_start_ms: int | None = None
    source: str = "speech"
    component_scores: tuple[float, ...] = ()


class VocalActivityObserver(Protocol):
    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]: ...


class AudioEventModel(Protocol):
    def infer(self, waveform: np.ndarray) -> np.ndarray: ...


class TenVadActivityObserver:
    """Streaming TEN-VAD score adapter for process-loopback PCM."""

    SAMPLE_RATE = 16000
    HOP_SIZE = 256

    def __init__(self, *, threshold: float = 0.5, backend=None):
        if backend is None:
            from ten_vad import TenVad
            backend = TenVad(self.HOP_SIZE, float(threshold))
        self.backend = backend
        self._tail = np.empty(0, dtype=np.float32)
        self._tail_start_ms: float | None = None

    def reset(self) -> None:
        self._tail = np.empty(0, dtype=np.float32)
        self._tail_start_ms = None

    @staticmethod
    def _waveform(chunk: PCMChunk) -> np.ndarray:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            raise ValueError("TEN-VAD observer requires 48 kHz stereo float32 PCM")
        stereo = np.frombuffer(chunk.frames, dtype="<f4").reshape(-1, 2)
        mono = stereo.mean(axis=1, dtype=np.float32)
        return _downsample_48k_to_16k(mono)

    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]:
        waveform = self._waveform(chunk)
        chunk_start = float(chunk.start_ms)
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
            pcm16 = np.ascontiguousarray(np.clip(raw, -1.0, 1.0) * 32767.0, dtype=np.int16)
            probability, _flag = self.backend.process(pcm16)
            frame_start = int(round(start_ms + index * hop_ms))
            frames.append(VocalActivityFrame(
                at_ms=int(round(start_ms + (index + 1) * hop_ms)),
                score=max(0.0, min(1.0, float(probability))),
                support_start_ms=frame_start,
                source="speech",
            ))

        consumed = full * self.HOP_SIZE
        self._tail = np.ascontiguousarray(waveform[consumed:], dtype=np.float32)
        self._tail_start_ms = start_ms + consumed * 1000.0 / self.SAMPLE_RATE
        return tuple(frames)


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


# YAMNet / AudioSet classes that directly describe a human vocal or respiratory
# sound. Crowd/hubbub classes remain excluded; source ownership is not an
# observer responsibility.
AUDIOSET_HUMAN_VOCAL_CLASSES = tuple(range(0, 35)) + tuple(range(36, 46))


class AudioSetHumanVocalObserver:
    """Streaming non-lexical human-vocal observation from AudioSet scores."""

    SAMPLE_RATE = 16000
    WINDOW_SAMPLES = 15_360  # 0.96 s native receptive field
    HOP_SAMPLES = 3_840      # 0.24 s observation cadence

    def __init__(self, model: AudioEventModel):
        self.model = model
        self._waveform = np.empty(0, dtype=np.float32)
        self._start_ms: float | None = None
        self._next_emit = self.HOP_SAMPLES

    def reset(self) -> None:
        self._waveform = np.empty(0, dtype=np.float32)
        self._start_ms = None
        self._next_emit = self.HOP_SAMPLES

    @staticmethod
    def _waveform_16k(chunk: PCMChunk) -> np.ndarray:
        if (chunk.sample_rate, chunk.channels, chunk.sample_width_bytes) != (48000, 2, 4):
            raise ValueError("AudioSet observer requires 48 kHz stereo float32 PCM")
        stereo = np.frombuffer(chunk.frames, dtype="<f4").reshape(-1, 2)
        mono = stereo.mean(axis=1, dtype=np.float32)
        return _downsample_48k_to_16k(mono)

    def _reset_span(self, waveform: np.ndarray, start_ms: float) -> None:
        self._waveform = np.ascontiguousarray(waveform, dtype=np.float32)
        self._start_ms = float(start_ms)
        self._next_emit = self.HOP_SAMPLES

    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]:
        waveform = self._waveform_16k(chunk)
        if not waveform.size:
            return ()
        chunk_start = float(chunk.start_ms)
        if self._start_ms is None or not self._waveform.size:
            self._reset_span(waveform, chunk_start)
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
            human_scores = tuple(
                max(0.0, min(1.0, float(scores[index])))
                for index in AUDIOSET_HUMAN_VOCAL_CLASSES
            )
            at_ms = int(round(self._start_ms + end * 1000.0 / self.SAMPLE_RATE))
            support_start = int(round(self._start_ms + start * 1000.0 / self.SAMPLE_RATE))
            frames.append(VocalActivityFrame(
                at_ms=at_ms,
                score=max(human_scores, default=0.0),
                support_start_ms=support_start,
                source="nonlexical",
                component_scores=human_scores,
            ))
            self._next_emit += self.HOP_SAMPLES

        removable = max(0, self._next_emit - self.WINDOW_SAMPLES - self.HOP_SAMPLES)
        if removable:
            self._waveform = np.ascontiguousarray(self._waveform[removable:], dtype=np.float32)
            self._start_ms += removable * 1000.0 / self.SAMPLE_RATE
            self._next_emit -= removable
        return tuple(frames)


class HumanVocalActivityObserver:
    """One product observer preserving speech and non-lexical score provenance."""

    def __init__(self, event_model, *, speech_observer: VocalActivityObserver | None = None):
        self.speech_observer = speech_observer or TenVadActivityObserver()
        model = event_model if hasattr(event_model, 'infer') else OnnxAudioSetModel(event_model)
        self.event_observer = AudioSetHumanVocalObserver(model)

    def reset(self) -> None:
        for observer in (self.speech_observer, self.event_observer):
            reset = getattr(observer, "reset", None)
            if callable(reset):
                reset()

    def observe(self, chunk: PCMChunk) -> tuple[VocalActivityFrame, ...]:
        frames = self.speech_observer.observe(chunk) + self.event_observer.observe(chunk)
        return tuple(sorted(
            frames,
            key=lambda frame: (
                frame.at_ms,
                frame.support_start_ms if frame.support_start_ms is not None else frame.at_ms,
            ),
        ))


class AcousticObservationHistory:
    """Target-independent measurement cache over the retained PCM timeline."""

    ACTIVE_SCORE_THRESHOLD = {
        "speech": 0.45,
        "nonlexical": 0.45,
    }
    MIN_RECURRENCE_LAG_MS = 300
    MIN_RECURRENCE_CYCLES = 3

    def __init__(self, *, horizon_ms: int = 10 * 60 * 1000):
        self.horizon_ms = int(horizon_ms)
        self._vocal: deque[VocalActivityFrame] = deque()

    def reset(self) -> None:
        self._vocal.clear()

    @property
    def latest_ms(self) -> int | None:
        return int(self._vocal[-1].at_ms) if self._vocal else None

    @property
    def earliest_ms(self) -> int | None:
        return int(self._vocal[0].at_ms) if self._vocal else None

    def add_vocal(self, frames: tuple[VocalActivityFrame, ...]) -> None:
        if not frames:
            return
        self._vocal.extend(frames)
        self._trim(frames[-1].at_ms)

    def _trim(self, now_ms: int) -> None:
        cutoff = int(now_ms) - self.horizon_ms
        while self._vocal and self._vocal[0].at_ms < cutoff:
            self._vocal.popleft()

    @classmethod
    def _threshold(cls, source: str) -> float:
        return float(cls.ACTIVE_SCORE_THRESHOLD.get(source, 0.45))

    @staticmethod
    def _series_intervals(
        frames: list[VocalActivityFrame], *, start_ms: int, end_ms: int, threshold: float,
    ) -> tuple[tuple[int, int], ...]:
        active = [frame for frame in frames if frame.score >= threshold]
        if not active:
            return ()
        gaps = [b.at_ms - a.at_ms for a, b in zip(frames, frames[1:]) if b.at_ms > a.at_ms]
        nominal_gap = float(np.median(gaps)) if gaps else 16.0
        # This grouping belongs to the observer measurement scale.  It is not a
        # physical endpoint or lexical-association margin.
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
            active_count = sum(frame.score >= threshold for frame in frames)
            series.append(VocalEvidenceSeries(
                source=source,
                frame_count=len(frames),
                active_count=active_count,
                peak_score=max((frame.score for frame in frames), default=0.0),
                active_score_threshold=threshold,
                active_intervals=self._series_intervals(
                    frames, start_ms=start_ms, end_ms=end_ms, threshold=threshold),
                samples=tuple(
                    VocalScoreSample(
                        at_ms=int(frame.at_ms),
                        score=float(frame.score),
                        support_start_ms=int(
                            frame.support_start_ms
                            if frame.support_start_ms is not None else frame.at_ms),
                        component_scores=tuple(float(value) for value in frame.component_scores),
                    )
                    for frame in frames
                ),
            ))
        return HumanVocalEvidence(
            start_ms=int(start_ms), end_ms=int(end_ms), series=tuple(series))

    @classmethod
    def _series_recurrence(
        cls, frames: list[VocalActivityFrame], *, threshold: float,
    ) -> VocalRecurrenceEvidence | None:
        if len(frames) < cls.MIN_RECURRENCE_CYCLES * 2:
            return None
        times = np.asarray([frame.at_ms for frame in frames], dtype=np.float64)
        gaps = np.diff(times)
        gaps = gaps[gaps > 0]
        if not gaps.size:
            return None
        cadence = float(np.median(gaps))
        if not np.isfinite(cadence) or cadence <= 0:
            return None

        # Resample only this observer's score series to its own median cadence.
        # Different observers are never averaged onto one probability scale.
        grid = np.arange(times[0], times[-1] + cadence * 0.5, cadence, dtype=np.float64)
        if grid.size < cls.MIN_RECURRENCE_CYCLES * 2:
            return None
        values = np.interp(grid, times, np.asarray([frame.score for frame in frames], dtype=np.float64))
        values = np.clip(values, 0.0, 1.0)
        if np.count_nonzero(values >= threshold) < cls.MIN_RECURRENCE_CYCLES:
            return None

        minimum_lag = max(1, int(round(cls.MIN_RECURRENCE_LAG_MS / cadence)))
        maximum_lag = int(grid.size // cls.MIN_RECURRENCE_CYCLES)
        if maximum_lag < minimum_lag:
            return None

        best: tuple[float, VocalRecurrenceEvidence] | None = None
        for lag in range(minimum_lag, maximum_lag + 1):
            left = values[:-lag]
            right = values[lag:]
            if left.size < lag * (cls.MIN_RECURRENCE_CYCLES - 1):
                continue
            left_centered = left - left.mean()
            right_centered = right - right.mean()
            denom = float(np.linalg.norm(left_centered) * np.linalg.norm(right_centered))
            if denom <= 1e-8:
                # A sustained flat vocal score is persistence, not evidence of
                # recurrence.  Releasing it as a loop would cut long finite cries.
                continue
            correlation = max(0.0, min(1.0, float(left_centered @ right_centered / denom)))
            pair_similarity = np.clip(1.0 - np.abs(left - right), 0.0, 1.0)
            level_consistency = float(pair_similarity.mean())
            recent_count = min(lag, pair_similarity.size)
            recent_strength = float(pair_similarity[-recent_count:].mean())

            cycle_scores: list[float] = []
            for offset in range(0, pair_similarity.size, lag):
                part = pair_similarity[offset:offset + lag]
                if part.size:
                    cycle_scores.append(float(part.mean()))
            if len(cycle_scores) < cls.MIN_RECURRENCE_CYCLES - 1:
                continue
            spread = float(np.std(cycle_scores))
            period_stability = max(0.0, min(1.0, 1.0 - spread / 0.25))
            evidence = VocalRecurrenceEvidence(
                strength=correlation,
                period_ms=int(round(lag * cadence)),
                period_stability=period_stability,
                recent_strength=recent_strength,
                level_consistency=level_consistency,
            )
            rank = min(correlation, recent_strength, level_consistency, period_stability)
            if best is None or rank > best[0]:
                best = (rank, evidence)
        return best[1] if best is not None else None

    def recurrence(self, *, start_ms: int, end_ms: int) -> VocalRecurrenceEvidence:
        """Measure repetition of human-vocal observer time series only.

        BGM-dominated full-mix fingerprints are deliberately absent.  Recurrence
        remains target-independent and source-agnostic, but it must be carried by
        a human-vocal measurement rather than by unrelated process-mix structure.
        """
        selected = [frame for frame in self._vocal if start_ms <= frame.at_ms <= end_ms]
        by_source: dict[str, list[VocalActivityFrame]] = {}
        for frame in selected:
            by_source.setdefault(frame.source, []).append(frame)
        candidates = [
            candidate
            for source, frames in by_source.items()
            if (candidate := self._series_recurrence(
                frames, threshold=self._threshold(source))) is not None
        ]
        if not candidates:
            return VocalRecurrenceEvidence()
        return max(
            candidates,
            key=lambda item: min(
                item.strength, item.period_stability,
                item.recent_strength, item.level_consistency,
            ),
        )
