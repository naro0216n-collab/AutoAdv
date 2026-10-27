"""Continuous acoustic measurements over retained PCM.

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
from .audio_evidence import (
    HumanVocalEvidence, VocalActivityInterval, VocalEvidenceSeries,
    VocalRecurrenceEvidence,
)


@dataclass(frozen=True)
class VocalActivityFrame:
    """One observer score with the exact input support that produced it."""

    at_ms: int
    score: float
    support_start_ms: int | None = None
    observer: str = "speech"
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
                observer="speech",
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
                observer="nonlexical",
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
    MIN_RECURRENCE_CYCLES = 4
    MIN_INACTIVE_SUPPORT_MS = 240

    def __init__(self, *, horizon_ms: int = 10 * 60 * 1000):
        self.horizon_ms = int(horizon_ms)
        # Raw observer frames are retained only for recurrence and diagnostics.
        # Activity intervals are derived once as frames arrive rather than rebuilt
        # for every Presentation assessment.
        self._vocal: deque[VocalActivityFrame] = deque()
        self._activity: dict[str, deque[VocalActivityInterval]] = {}
        self._activity_required: dict[str, int] = {}
        self._quiet_support: dict[str, tuple[int, int] | None] = {}

    def reset(self) -> None:
        self._vocal.clear()
        self._activity.clear()
        self._activity_required.clear()
        self._quiet_support.clear()

    @property
    def latest_ms(self) -> int | None:
        return int(self._vocal[-1].at_ms) if self._vocal else None

    @property
    def earliest_ms(self) -> int | None:
        return int(self._vocal[0].at_ms) if self._vocal else None

    @classmethod
    def _threshold(cls, observer: str) -> float:
        return float(cls.ACTIVE_SCORE_THRESHOLD.get(observer, 0.45))

    @staticmethod
    def _support(frame: VocalActivityFrame) -> tuple[int, int]:
        end = int(frame.at_ms)
        start = int(frame.support_start_ms if frame.support_start_ms is not None else end)
        return start, end

    def _record_activity(self, frame: VocalActivityFrame, *, required: int) -> None:
        """Advance one observer-local activity measurement exactly once."""
        observer = frame.observer
        threshold = self._threshold(observer)
        support_start, at_ms = self._support(frame)
        intervals = self._activity.setdefault(observer, deque())

        if float(frame.score) >= threshold:
            if not intervals or not intervals[-1].ongoing:
                intervals.append(VocalActivityInterval(
                    start_ms=at_ms, last_active_ms=at_ms,
                    inactive_required_ms=required,
                ))
                self._quiet_support[observer] = None
                return
            current = intervals[-1]
            intervals[-1] = VocalActivityInterval(
                start_ms=current.start_ms,
                last_active_ms=at_ms,
                inactive_required_ms=required,
            )
            self._quiet_support[observer] = None
            return

        if not intervals:
            return
        if not intervals[-1].ongoing:
            current = intervals[-1]
            if (current.inactive_start_ms is not None
                    and current.inactive_end_ms is not None
                    and support_start <= current.inactive_end_ms):
                intervals[-1] = VocalActivityInterval(
                    start_ms=current.start_ms,
                    last_active_ms=current.last_active_ms,
                    inactive_required_ms=required,
                    inactive_start_ms=current.inactive_start_ms,
                    inactive_end_ms=max(current.inactive_end_ms, at_ms),
                )
            return
        current = intervals[-1]
        quiet = self._quiet_support.get(observer)
        if quiet is None or support_start > quiet[1]:
            quiet_start, quiet_end = support_start, at_ms
        else:
            quiet_start, quiet_end = quiet[0], max(quiet[1], at_ms)
        self._quiet_support[observer] = (quiet_start, quiet_end)
        if quiet_start >= current.last_active_ms and quiet_end - quiet_start >= required:
            intervals[-1] = VocalActivityInterval(
                start_ms=current.start_ms,
                last_active_ms=current.last_active_ms,
                inactive_required_ms=required,
                inactive_start_ms=quiet_start,
                inactive_end_ms=quiet_end,
            )

    def add_vocal(self, frames: tuple[VocalActivityFrame, ...]) -> None:
        if not frames:
            return
        ordered = tuple(sorted(
            frames,
            key=lambda frame: (frame.at_ms, self._support(frame)[0], frame.observer),
        ))
        for frame in ordered:
            self._vocal.append(frame)
            observer = frame.observer
            support_start, at_ms = self._support(frame)
            previous = self._activity_required.get(observer, self.MIN_INACTIVE_SUPPORT_MS)
            required = max(previous, self.MIN_INACTIVE_SUPPORT_MS, at_ms - support_start)
            self._activity_required[observer] = required
            intervals = self._activity.get(observer)
            if required > previous and intervals and intervals[-1].ongoing:
                current = intervals[-1]
                intervals[-1] = VocalActivityInterval(
                    start_ms=current.start_ms,
                    last_active_ms=current.last_active_ms,
                    inactive_required_ms=required,
                )
            self._record_activity(frame, required=required)
        self._trim(ordered[-1].at_ms)

    def _trim(self, now_ms: int) -> None:
        cutoff = int(now_ms) - self.horizon_ms
        while self._vocal and self._vocal[0].at_ms < cutoff:
            self._vocal.popleft()
        for observer in tuple(self._activity):
            intervals = self._activity[observer]
            while (intervals and not intervals[0].ongoing
                   and intervals[0].inactive_end_ms is not None
                   and intervals[0].inactive_end_ms < cutoff):
                intervals.popleft()
            if not intervals:
                del self._activity[observer]

    @staticmethod
    def _interval_at(
        interval: VocalActivityInterval, *, end_ms: int,
    ) -> VocalActivityInterval | None:
        if interval.start_ms > end_ms:
            return None
        inactive_start = interval.inactive_start_ms
        inactive_end = interval.inactive_end_ms
        if inactive_start is None or inactive_start > end_ms:
            inactive_start = inactive_end = None
        elif inactive_end is not None:
            inactive_end = min(int(inactive_end), int(end_ms))
            if inactive_end - inactive_start < interval.inactive_required_ms:
                inactive_start = inactive_end = None
        return VocalActivityInterval(
            start_ms=interval.start_ms,
            last_active_ms=min(interval.last_active_ms, int(end_ms)),
            inactive_required_ms=interval.inactive_required_ms,
            inactive_start_ms=inactive_start,
            inactive_end_ms=inactive_end,
        )

    def summarize_vocal(self, *, start_ms: int, end_ms: int) -> HumanVocalEvidence:
        """Return cached activity measurements intersecting the requested range."""
        series: list[VocalEvidenceSeries] = []
        for observer, intervals in sorted(self._activity.items()):
            selected: list[VocalActivityInterval] = []
            for interval in intervals:
                measured = self._interval_at(interval, end_ms=int(end_ms))
                if measured is None:
                    continue
                measured_end = (
                    measured.inactive_end_ms
                    if measured.inactive_end_ms is not None else measured.last_active_ms
                )
                if measured_end < int(start_ms):
                    continue
                selected.append(measured)
            if selected:
                series.append(VocalEvidenceSeries(observer=observer, activity=tuple(selected)))
        return HumanVocalEvidence(series=tuple(series))

    def vocal_diagnostics(self, *, start_ms: int, end_ms: int) -> list[dict]:
        """Summarize raw observer facts for telemetry without exporting them upward."""
        by_observer: dict[str, list[VocalActivityFrame]] = {}
        for frame in self._vocal:
            if int(start_ms) <= int(frame.at_ms) <= int(end_ms):
                by_observer.setdefault(frame.observer, []).append(frame)
        result: list[dict] = []
        for observer, frames in sorted(by_observer.items()):
            threshold = self._threshold(observer)
            result.append({
                "observer": observer,
                "frame_count": len(frames),
                "active_count": sum(float(frame.score) >= threshold for frame in frames),
                "peak_score": max((float(frame.score) for frame in frames), default=0.0),
                "active_score_threshold": threshold,
                "measurement_support_start_ms": min(
                    (self._support(frame)[0] for frame in frames), default=None),
                "measurement_support_end_ms": max(
                    (int(frame.at_ms) for frame in frames), default=None),
            })
        return result

    @classmethod
    def _series_recurrence_candidates(
        cls, frames: list[VocalActivityFrame], *, observer: str, threshold: float,
    ) -> tuple[VocalRecurrenceEvidence, ...]:
        if len(frames) < cls.MIN_RECURRENCE_CYCLES * 2:
            return ()
        times = np.asarray([frame.at_ms for frame in frames], dtype=np.float64)
        gaps = np.diff(times)
        gaps = gaps[gaps > 0]
        if not gaps.size:
            return ()
        cadence = float(np.median(gaps))
        if not np.isfinite(cadence) or cadence <= 0:
            return ()

        # Resample only this observer's measurements to its own median cadence.
        # Different observers are never averaged onto one probability scale.
        # When the observer exposes a class-score vector, preserve that structure
        # for recurrence instead of first collapsing it to the maximum class.
        # The scalar activity score remains the observer-local gate for whether
        # the interval contains human-vocal evidence at all.
        grid = np.arange(times[0], times[-1] + cadence * 0.5, cadence, dtype=np.float64)
        if grid.size < cls.MIN_RECURRENCE_CYCLES * 2:
            return ()
        activity = np.interp(
            grid, times, np.asarray([frame.score for frame in frames], dtype=np.float64))
        activity = np.clip(activity, 0.0, 1.0)
        if np.count_nonzero(activity >= threshold) < cls.MIN_RECURRENCE_CYCLES:
            return ()

        component_size = len(frames[0].component_scores) if frames else 0
        use_components = bool(
            component_size
            and all(len(frame.component_scores) == component_size for frame in frames)
        )
        if use_components:
            raw_features = np.asarray(
                [frame.component_scores for frame in frames], dtype=np.float64)
            values = np.column_stack([
                np.interp(grid, times, raw_features[:, index])
                for index in range(component_size)
            ])
        else:
            values = activity[:, None]
        values = np.clip(values, 0.0, 1.0)

        minimum_lag = max(1, int(round(cls.MIN_RECURRENCE_LAG_MS / cadence)))
        maximum_lag = int(grid.size // cls.MIN_RECURRENCE_CYCLES)
        if maximum_lag < minimum_lag:
            return ()

        measured: list[tuple[int, float]] = []
        for lag in range(minimum_lag, maximum_lag + 1):
            # A recurrence candidate must reproduce the same *shape* over several
            # consecutive cycles, including the cycle ending at the current
            # frontier.  Compare adjacent recent cycles rather than averaging
            # unrelated notions of level, recency and stability into separate
            # acceptance gates.  Center every cycle independently so a steady
            # vocal level is persistence, not recurrence.
            local_count = lag * cls.MIN_RECURRENCE_CYCLES
            if values.shape[0] < local_count:
                continue
            local = values[-local_count:]
            cycles = tuple(
                local[index * lag:(index + 1) * lag]
                for index in range(cls.MIN_RECURRENCE_CYCLES)
            )
            correlations: list[float] = []
            for earlier, later in zip(cycles, cycles[1:]):
                earlier_centered = earlier - earlier.mean(axis=0, keepdims=True)
                later_centered = later - later.mean(axis=0, keepdims=True)
                denom = float(
                    np.linalg.norm(earlier_centered) * np.linalg.norm(later_centered))
                if denom <= 1e-8:
                    correlations = []
                    break
                correlations.append(max(0.0, min(1.0, float(
                    np.vdot(earlier_centered, later_centered) / denom))))
            if len(correlations) != cls.MIN_RECURRENCE_CYCLES - 1:
                continue
            # The weakest adjacent transition is the repeatability of this lag.
            # This makes a broken latest cycle immediately stop being recurrent
            # without retaining a sticky historical loop state.
            measured.append((lag, min(correlations)))

        if not measured:
            return ()

        # High self-similarity alone is not periodicity: a slowly changing finite
        # sound can correlate at almost every lag.  Period prominence measures
        # whether this lag stands out from the other admissible lags in the same
        # observer/query.  It therefore distinguishes a period from generic
        # temporal smoothness without introducing source or loop labels.
        candidates: list[VocalRecurrenceEvidence] = []
        for lag, strength in measured:
            other_strengths = [value for other_lag, value in measured if other_lag != lag]
            baseline = float(np.median(other_strengths)) if other_strengths else strength
            candidates.append(VocalRecurrenceEvidence(
                observer=observer,
                strength=float(strength),
                period_ms=int(round(lag * cadence)),
                prominence=max(0.0, float(strength) - baseline),
            ))

        # Progression owns the acceptance contract. Keep every measured period
        # candidate so one stronger but non-specific lag cannot hide another
        # genuinely periodic candidate.
        return tuple(candidates)

    def recurrences(
        self, *, start_ms: int, end_ms: int, observer: str | None = None,
    ) -> tuple[VocalRecurrenceEvidence, ...]:
        """Return current recurrence candidates without crossing observer provenance."""
        selected = [
            frame for frame in self._vocal
            if start_ms <= frame.at_ms <= end_ms
            and int(frame.support_start_ms if frame.support_start_ms is not None else frame.at_ms)
                >= start_ms
            and (observer is None or frame.observer == observer)
        ]
        by_observer: dict[str, list[VocalActivityFrame]] = {}
        for frame in selected:
            by_observer.setdefault(frame.observer, []).append(frame)
        candidates: list[VocalRecurrenceEvidence] = []
        for observer, frames in by_observer.items():
            candidates.extend(self._series_recurrence_candidates(
                frames, observer=observer, threshold=self._threshold(observer)))
        return tuple(candidates)
