"""Target-conditioned phoneme CTC alignment for native game audio.

The backend answers one narrow question: where, if anywhere, does the known
utterance pronunciation fit the observed process PCM?  It does not transcribe
free speech and it does not decide whether AutoAdv should advance the game.

Frames not assigned to target phones are *unaligned* frames.  They are not
classified as silence, BGM, SE, or background speech.  Target identity and a
committed *target-pronunciation* boundary are derived only from evidence inside
the same CTC posterior/path provenance.  That boundary is not a voice-file or
playback-channel EOF.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Protocol

import numpy as np

from .audio_buffer import PCMChunk
from .g2p import G2PBackend, PronunciationCandidate, default_g2p


@dataclass(frozen=True)
class FrameSpan:
    start: int
    end: int  # exclusive

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("invalid frame span")


@dataclass(frozen=True)
class AlignmentFit:
    """Backend-local evidence for one target-conditioned path.

    Margins are logit differences between the requested phone and the strongest
    competing CTC symbol at the same frame.  They are meaningful only inside
    this acoustic model; they are never combined with VAD/ASR/transport scores.
    """

    path_margin: float = float("-inf")
    mean_unit_peak_margin: float = float("-inf")
    min_unit_peak_margin: float = float("-inf")
    weak_units: int = 0
    boundary_margin: float = float("-inf")


@dataclass(frozen=True)
class TargetTimeAlignment:
    """One immutable alignment result from one analyzed PCM snapshot."""

    target_phonemes: tuple[str, ...] = ()
    reading_rank: int = -1
    frame_ms: float = 0.0
    frame_count: int = 0
    target_unit_spans: tuple[FrameSpan, ...] = ()
    unaligned_spans: tuple[FrameSpan, ...] = ()
    aligned_target_units: int = 0
    target_associated: bool = False
    complete_alignment: bool = False
    committed_target_end: bool = False
    target_end_frame: int | None = None
    post_target_free_frames: int = 0
    fit: AlignmentFit = AlignmentFit()
    rejection: str = ""

    @property
    def target_units(self) -> int:
        return len(self.target_phonemes)

    @property
    def coverage(self) -> float:
        return self.aligned_target_units / self.target_units if self.target_units else 0.0


@dataclass(frozen=True)
class AlignmentPolicy:
    """Calibration owned by this CTC backend, not Controller policy."""

    # A supported unit must have at least one frame where the target phone is
    # not badly dominated by another CTC symbol.  The sequence as a whole must
    # have positive target-vs-competitor evidence.
    min_unit_peak_margin: float = -0.35
    min_mean_unit_peak_margin: float = 0.30
    max_weak_fraction: float = 0.20
    min_path_margin_per_unit: float = 0.12
    # A target association needs a bounded prefix; short targets naturally use
    # their whole sequence rather than an arbitrary special-case exact string.
    association_units: int = 3
    # Internal pauses are allowed, but arbitrary target phones may not be picked
    # from unrelated moments spread across a very long scene.
    max_internal_free_ms: float = 2000.0
    # A terminal path needs right context and must remain better than any later
    # completion candidate in the same posterior before the target end is committed.
    right_context_ms: float = 140.0
    min_boundary_margin: float = 0.35
    reading_switch_margin: float = 0.50


class PosteriorModel(Protocol):
    frame_stride_ms: float
    vocab: dict[str, int]

    def infer(self, chunks: tuple[PCMChunk, ...]) -> np.ndarray:
        """Return [frames, labels] logits for the exact concatenated PCM."""


SPECIAL_TOKENS = frozenset({"PAD", "UNK", "SOS", "EOS"})


def _log_softmax(values: np.ndarray) -> np.ndarray:
    x = np.asarray(values, dtype=np.float32)
    if x.ndim != 2:
        raise ValueError("CTC posterior must be [frames, labels]")
    maximum = np.max(x, axis=1, keepdims=True)
    shifted = x - maximum
    return shifted - np.log(np.exp(shifted).sum(axis=1, keepdims=True))


def _compact_spans(states: np.ndarray, *, free: bool) -> tuple[FrameSpan, ...]:
    wanted = (states % 2 == 0) if free else (states % 2 == 1)
    spans: list[FrameSpan] = []
    start = None
    for i, flag in enumerate(wanted.tolist() + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            spans.append(FrameSpan(start, i))
            start = None
    return tuple(spans)


def _backtrack(back: np.ndarray, terminal_state: int) -> np.ndarray:
    frames = back.shape[0]
    states = np.empty(frames, dtype=np.int32)
    state = int(terminal_state)
    for t in range(frames - 1, -1, -1):
        states[t] = state
        previous = int(back[t, state])
        if t and previous < 0:
            raise RuntimeError("broken CTC alignment provenance")
        state = previous
    return states


def _phone_spans(states: np.ndarray, units: int) -> tuple[FrameSpan, ...]:
    spans: list[FrameSpan] = []
    for unit in range(units):
        state = 2 * unit + 1
        indices = np.flatnonzero(states == state)
        if not len(indices):
            break
        spans.append(FrameSpan(int(indices[0]), int(indices[-1]) + 1))
    return tuple(spans)


def _unit_peaks(margins: np.ndarray, spans: tuple[FrameSpan, ...]) -> tuple[float, ...]:
    peaks = []
    for unit, span in enumerate(spans):
        peaks.append(float(np.max(margins[span.start:span.end, unit])))
    return tuple(peaks)


def _support_from_path(margins: np.ndarray, states: np.ndarray, units: int,
                       policy: AlignmentPolicy) -> tuple[bool, AlignmentFit, tuple[FrameSpan, ...]]:
    spans = _phone_spans(states, units)
    if len(spans) != units or not spans:
        return False, AlignmentFit(), spans
    peaks = _unit_peaks(margins, spans)
    weak = sum(value < policy.min_unit_peak_margin for value in peaks)
    allowed_weak = int(units * policy.max_weak_fraction)
    mean_peak = float(np.mean(peaks))
    path_margin = float(sum(
        margins[span.start:span.end, unit].sum()
        for unit, span in enumerate(spans)
    ))
    supported = bool(
        weak <= allowed_weak
        and mean_peak >= policy.min_mean_unit_peak_margin
        and path_margin / units >= policy.min_path_margin_per_unit
    )
    return supported, AlignmentFit(
        path_margin=path_margin,
        mean_unit_peak_margin=mean_peak,
        min_unit_peak_margin=float(min(peaks)),
        weak_units=weak,
    ), spans


def _candidate_alignment(log_probs: np.ndarray, phonemes: tuple[str, ...],
                         ids: tuple[int, ...], reading_rank: int,
                         frame_ms: float, policy: AlignmentPolicy) -> TargetTimeAlignment:
    frames = int(log_probs.shape[0])
    units = len(ids)
    if not frames or not units:
        return TargetTimeAlignment(target_phonemes=phonemes, reading_rank=reading_rank,
                                   frame_ms=frame_ms, frame_count=frames,
                                   rejection="empty_target_or_audio")

    # Target-vs-best-competitor log margin at every frame.  A free state has no
    # semantic emission and simply preserves a frame as unaligned evidence.
    margins = np.empty((frames, units), dtype=np.float32)
    labels = log_probs.shape[1]
    for unit, label in enumerate(ids):
        if not 0 <= label < labels:
            return TargetTimeAlignment(target_phonemes=phonemes, reading_rank=reading_rank,
                                       frame_ms=frame_ms, frame_count=frames,
                                       rejection="target_token_out_of_model_range")
        competitor = log_probs.copy()[:, :]
        competitor[:, label] = -np.inf
        margins[:, unit] = log_probs[:, label] - np.max(competitor, axis=1)

    state_count = units * 2 + 1
    neg = np.float32(-1e30)
    previous = np.full(state_count, neg, dtype=np.float32)
    previous[0] = 0.0
    back = np.full((frames, state_count), -1, dtype=np.int32)
    score_history = np.full((frames, state_count), neg, dtype=np.float32)
    max_gap_frames = max(1, int(round(policy.max_internal_free_ms / max(frame_ms, 1e-6))))
    # Last frame at which each free state was entered; used only to prevent an
    # arbitrarily long internal free span from stitching unrelated utterances.
    gap_age = np.zeros(state_count, dtype=np.int32)

    for t in range(frames):
        current = np.full(state_count, neg, dtype=np.float32)
        current_age = np.zeros(state_count, dtype=np.int32)
        # free_0: unlimited leading context.
        current[0] = previous[0]
        back[t, 0] = 0
        for i in range(units):
            phone_state = 2 * i + 1
            free_before = 2 * i
            free_after = 2 * i + 2

            # phone_i can continue, begin after free_i, or directly follow the
            # previous phone when the labels differ (ordinary CTC transition).
            sources = [(previous[phone_state], phone_state),
                       (previous[free_before], free_before)]
            if i and ids[i] != ids[i - 1]:
                sources.append((previous[phone_state - 2], phone_state - 2))
            base, source = max(sources, key=lambda item: float(item[0]))
            if base > neg / 2:
                if source == free_before and i and gap_age[free_before] > max_gap_frames:
                    pass
                else:
                    current[phone_state] = base + margins[t, i]
                    back[t, phone_state] = source

            # free_{i+1} may continue or begin immediately after phone_i.  It
            # is unclassified target-external frame ownership, not VAD/silence.
            stay = previous[free_after]
            enter = previous[phone_state]
            if stay >= enter:
                current[free_after] = stay
                back[t, free_after] = free_after
                current_age[free_after] = gap_age[free_after] + 1
            else:
                current[free_after] = enter
                back[t, free_after] = phone_state
                current_age[free_after] = 1
        previous = current
        gap_age = current_age
        score_history[t] = current

    # Find the longest prefix whose own best path has model-local support.  A
    # merely reachable path is deliberately insufficient: with enough frames a
    # forced DP can place any target somewhere even when it was never spoken.
    supported_units = 0
    supported_states: np.ndarray | None = None
    supported_fit = AlignmentFit()
    supported_spans: tuple[FrameSpan, ...] = ()
    supported_terminal = -1
    for prefix in range(units, 0, -1):
        free_state = 2 * prefix
        phone_state = free_state - 1
        terminal = free_state if previous[free_state] >= previous[phone_state] else phone_state
        if previous[terminal] <= neg / 2:
            continue
        states = _backtrack(back, terminal)
        ok, fit, spans = _support_from_path(margins[:, :prefix], states, prefix, policy)
        if ok:
            supported_units = prefix
            supported_states = states
            supported_fit = fit
            supported_spans = spans
            supported_terminal = terminal
            break

    if supported_states is None:
        return TargetTimeAlignment(
            target_phonemes=phonemes, reading_rank=reading_rank,
            frame_ms=frame_ms, frame_count=frames,
            rejection="insufficient_model_support",
        )

    association_units = min(policy.association_units, units)
    associated = supported_units >= association_units
    complete = supported_units == units
    target_end_frame = supported_spans[-1].end if complete else None
    trailing = max(0, frames - target_end_frame) if target_end_frame is not None else 0

    boundary_margin = float("-inf")
    committed = False
    if complete and associated and target_end_frame is not None:
        last_phone_state = 2 * units - 1
        chosen_end_index = target_end_frame - 1
        chosen_completion = float(score_history[chosen_end_index, last_phone_state])
        later = score_history[chosen_end_index + 1:, last_phone_state]
        finite_later = later[later > neg / 2]
        if finite_later.size:
            boundary_margin = chosen_completion - float(np.max(finite_later))
        else:
            boundary_margin = float("inf")
        right_context_frames = max(1, int(round(policy.right_context_ms / max(frame_ms, 1e-6))))
        committed = bool(
            supported_terminal == 2 * units
            and trailing >= right_context_frames
            and boundary_margin >= policy.min_boundary_margin
        )

    fit = AlignmentFit(
        path_margin=supported_fit.path_margin,
        mean_unit_peak_margin=supported_fit.mean_unit_peak_margin,
        min_unit_peak_margin=supported_fit.min_unit_peak_margin,
        weak_units=supported_fit.weak_units,
        boundary_margin=boundary_margin,
    )
    return TargetTimeAlignment(
        target_phonemes=phonemes,
        reading_rank=reading_rank,
        frame_ms=frame_ms,
        frame_count=frames,
        target_unit_spans=supported_spans,
        unaligned_spans=_compact_spans(supported_states, free=True),
        aligned_target_units=supported_units,
        target_associated=associated,
        complete_alignment=complete,
        committed_target_end=committed,
        target_end_frame=target_end_frame,
        post_target_free_frames=trailing,
        fit=fit,
    )


class TargetConditionedAligner:
    """Map known Japanese text directly onto one phone-CTC posterior."""

    def __init__(self, model: PosteriorModel, *, g2p: G2PBackend | None = None,
                 policy: AlignmentPolicy = AlignmentPolicy(), max_readings: int = 4):
        self.model = model
        self.g2p = g2p or default_g2p()
        self.policy = policy
        self.max_readings = max_readings
        self._target_text = ""
        self._candidates: tuple[PronunciationCandidate, ...] = ()
        self._encoded: tuple[tuple[PronunciationCandidate, tuple[int, ...]], ...] = ()

    def set_target(self, text: str, *, candidates: tuple[PronunciationCandidate, ...] | None = None) -> None:
        if candidates is None:
            candidates = self.g2p.candidates(text, max_candidates=self.max_readings)
        encoded = []
        vocab = self.model.vocab
        for candidate in candidates:
            ids = []
            valid = True
            for phone in candidate.phonemes:
                token = phone
                if token not in vocab:
                    valid = False
                    break
                ids.append(int(vocab[token]))
            if valid and ids:
                encoded.append((candidate, tuple(ids)))
        if not encoded:
            raise ValueError("target text has no pronunciation representable by alignment model")
        self._target_text = text
        self._candidates = candidates
        self._encoded = tuple(encoded)

    def align(self, chunks: tuple[PCMChunk, ...]) -> TargetTimeAlignment:
        if not self._encoded:
            raise RuntimeError("alignment target is not set")
        logits = np.asarray(self.model.infer(chunks), dtype=np.float32)
        if logits.ndim != 2 or not logits.shape[0]:
            raise ValueError("alignment model returned no frame posterior")
        log_probs = _log_softmax(logits)
        frame_ms = float(self.model.frame_stride_ms)
        if not np.isfinite(frame_ms) or frame_ms <= 0:
            raise ValueError("alignment model must provide a positive fixed frame stride")
        results = [
            _candidate_alignment(log_probs, candidate.phonemes, ids, candidate.rank,
                                 frame_ms, self.policy)
            for candidate, ids in self._encoded
        ]
        primary = min(results, key=lambda item: item.reading_rank)
        best = primary
        for candidate in results:
            if candidate is primary:
                continue
            candidate_key = (candidate.complete_alignment, candidate.aligned_target_units,
                             candidate.fit.mean_unit_peak_margin)
            best_key = (best.complete_alignment, best.aligned_target_units,
                        best.fit.mean_unit_peak_margin)
            if candidate_key[:2] > best_key[:2]:
                best = candidate
            elif candidate_key[:2] == best_key[:2] and (
                    candidate.fit.mean_unit_peak_margin
                    > best.fit.mean_unit_peak_margin + self.policy.reading_switch_margin):
                best = candidate
        return best


def _downsample_48k_to_16k(mono_48k: np.ndarray) -> np.ndarray:
    """Band-limit Process Loopback PCM before the exact 3:1 decimation.

    Windows mixing produces a 48 kHz stream; it does not imply that content
    above the 16 kHz target Nyquist has already been removed.  A compact
    windowed-sinc FIR prevents music/SFX energy above 8 kHz from aliasing into
    the phone model's speech band.  ``same`` keeps target timing centered; the
    symmetric filter therefore adds no model-time shift.
    """
    values = np.asarray(mono_48k, dtype=np.float32)
    if not values.size:
        return values
    taps = 63
    center = (taps - 1) / 2
    n = np.arange(taps, dtype=np.float64) - center
    # 7.6 kHz passband edge at 48 kHz leaves transition room before 8 kHz.
    cutoff = 7600.0 / 48000.0
    kernel = (2.0 * cutoff * np.sinc(2.0 * cutoff * n)
              * np.hamming(taps))
    kernel /= kernel.sum()
    filtered = np.convolve(values.astype(np.float64, copy=False), kernel, mode="same")
    return np.ascontiguousarray(filtered[::3], dtype=np.float32)



class OnnxPhoneCTCModel:
    """Phone-CTC adapter over one explicit PCM snapshot.

    The pinned HuBERT backend is bidirectional rather than streaming.  Each
    distinct snapshot is therefore evaluated as a whole; scheduler backpressure
    is handled by NativeAudio instead of changing posterior semantics to make
    incremental tests cheaper.
    """

    frame_stride_ms = 20.0

    def __init__(self, model: Path, vocab: Path, *, num_threads: int = 2):
        if not model.is_file() or not vocab.is_file():
            raise FileNotFoundError("configured alignment model and vocabulary required")
        raw = json.loads(vocab.read_text(encoding="utf-8"))
        self.vocab = {str(token): int(index) for token, index in raw.items()}
        if self.vocab.get("PAD") != 0:
            raise ValueError("alignment vocabulary must use PAD=0 CTC blank")
        import onnxruntime as ort
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(num_threads))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model), sess_options=options,
                                            providers=["CPUExecutionProvider"])
        inputs = self.session.get_inputs()
        outputs = self.session.get_outputs()
        if len(inputs) != 1 or not outputs:
            raise ValueError("unsupported phone-CTC ONNX signature")
        self.input_name = inputs[0].name
        self.output_name = outputs[0].name
        expected_labels = max(self.vocab.values()) + 1
        output_shape = tuple(outputs[0].shape)
        if output_shape and isinstance(output_shape[-1], int) and output_shape[-1] != expected_labels:
            raise ValueError(
                f"alignment vocabulary/model label mismatch: {expected_labels} != {output_shape[-1]}"
            )

    @staticmethod
    def _waveform(chunks: tuple[PCMChunk, ...]) -> np.ndarray:
        if not chunks:
            return np.empty(0, dtype=np.float32)
        if any((c.sample_rate, c.channels, c.sample_width_bytes) != (48000, 2, 4)
               for c in chunks):
            raise ValueError("alignment requires 48 kHz stereo float32 PCM")
        stereo = np.concatenate([
            np.frombuffer(chunk.frames, dtype="<f4").reshape(-1, 2)
            for chunk in chunks
        ])
        mono_48k = stereo.mean(axis=1, dtype=np.float32)
        # The HuBERT frontend expects raw (not feature-normalized) 16 kHz
        # samples.  Band-limit before exact 3:1 decimation so unrelated high
        # frequency game audio cannot alias into the phone evidence.
        return _downsample_48k_to_16k(mono_48k)

    def _infer_waveform(self, waveform: np.ndarray) -> np.ndarray:
        values = np.ascontiguousarray(waveform, dtype=np.float32)
        if not values.size:
            return np.empty((0, len(self.vocab)), dtype=np.float32)
        output = np.asarray(
            self.session.run([self.output_name], {self.input_name: values[None, :]})[0],
            dtype=np.float32,
        )
        if output.ndim == 3 and output.shape[0] == 1:
            output = output[0]
        if output.ndim != 2 or output.shape[1] != len(self.vocab):
            raise ValueError("alignment model returned unsupported logits shape")
        return output

    def infer(self, chunks: tuple[PCMChunk, ...]) -> np.ndarray:
        return self._infer_waveform(self._waveform(chunks))
