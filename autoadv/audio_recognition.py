"""Process-loopback PCM -> VAD + Japanese ASR -> attributed native events.

This is the application-side audio evidence pipeline. The offline Sherpa
adapter is invoked on accumulated PCM outside the capture thread. Model
inference, BGM and residual speech are never treated as absence evidence.
"""
from __future__ import annotations
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from threading import Lock
from pathlib import Path
import time
import uuid

from .audio_buffer import AudioHistory, PCMChunk
from .audio_match import TargetMatcher, VoiceActivity
from .native_gate import AudioEvidence, NativeVoiceGate
from .contracts import Envelope


class JapaneseASR:
    def __init__(self, model: Path, tokens: Path, *, num_threads: int = 2):
        if not model.is_file() or not tokens.is_file():
            raise FileNotFoundError('sherpa model and tokens are required')
        import sherpa_onnx  # optional dependency
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), num_threads=num_threads,
            use_itn=True, debug=False)

    def recognize(self, chunks: tuple[PCMChunk, ...]) -> str:
        import numpy as np
        if not chunks:
            return ''
        if any((c.sample_rate, c.channels, c.sample_width_bytes) != (48000, 2, 4) for c in chunks):
            raise ValueError('ASR expects 48kHz stereo float32')
        frames = np.concatenate([np.frombuffer(c.frames, dtype='<f4').reshape(-1, 2) for c in chunks])
        mono = frames.mean(axis=1).astype('float32')
        stream = self.recognizer.create_stream()
        stream.accept_waveform(48000, mono)
        self.recognizer.decode_stream(stream)
        return stream.result.text


@dataclass(frozen=True)
class TargetLine:
    session_id: str
    line_epoch: int
    cancel_token: int
    text: str
    text_first_seen_ms: int
    # Obtained from independent game-side metadata, never from ambient VAD.
    metadata_voice_id: str | None = None


class AudioRecognitionPipeline:
    def __init__(self, history: AudioHistory, asr: JapaneseASR,
                 emit: Callable[[Envelope], None],
                 activity: VoiceActivity | None = None,
                 *, interval_ms: int = 500,
                 audit: Callable[[str,dict],None] | None = None,
                 clock: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000):
        if interval_ms < 50:
            raise ValueError('inference interval too small')
        self.history, self.asr, self.emit = history, asr, emit
        self.activity = activity or VoiceActivity()
        self.interval_ms, self.clock = interval_ms, clock
        self.audit = audit or (lambda *_:None)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='autoadv-asr')
        self.lock = Lock()
        self.target: TargetLine | None = None
        self.matcher: TargetMatcher | None = None
        self.gate = NativeVoiceGate()
        self.utterance_id = ''
        self.last_sample_ms = -1
        self.inflight = False
        self.closed = False
        self.last_vad_active = False
        self.fresh_onset = False
        self.ambiguous_speech = False
        self.tail_ms = 0
        self.last_vad_rise_ms: int | None = None
        self.last_boundary_ms: int = 0

    def mark_boundary(self, advanced_at_ms: int) -> None:
        """An irreversible input establishes a lower bound for the next voice."""
        with self.lock:
            self.last_boundary_ms = max(self.last_boundary_ms, advanced_at_ms)
            # An onset AFTER the observed screen transition began belongs to
            # the new candidate window, even if that candidate became ready
            # only after the onset. Invalidate only earlier playback.
            if (self.last_vad_rise_ms is not None and
                self.last_vad_rise_ms < self.last_boundary_ms):
                self.last_vad_rise_ms = None
            self.fresh_onset = False

    def begin(self, target: TargetLine) -> None:
        with self.lock:
            self.target = target
            self.matcher = TargetMatcher(target.text)
            self.utterance_id = uuid.uuid4().hex
            self.gate.begin_line(target.session_id, target.line_epoch, target.cancel_token)
            self.last_sample_ms = -1
            # The first candidate precedes visual TEXT_READY. A rising edge
            # captured just before the candidate may belong to THIS dialogue,
            # but never across a confirmed input/transition boundary.
            self.fresh_onset = bool(
                self.last_vad_rise_ms is not None and
                self.last_vad_rise_ms >= max(self.last_boundary_ms,
                                            target.text_first_seen_ms - 2000))
            self.ambiguous_speech = False

    def retarget(self, line_epoch:int, cancel_token:int, text:str) -> None:
        """Partial line text changed without creating an audio generation."""
        with self.lock:
            if self.target is None or (self.target.line_epoch, self.target.cancel_token) != (line_epoch, cancel_token):
                return
            from dataclasses import replace
            self.target = replace(self.target,text=text)
            self.matcher = TargetMatcher(text)
            self.last_sample_ms = -1
            # In-flight results for the former target cannot update this line.

    def potentially_active(self) -> bool:
        with self.lock:
            return (self.inflight or self.ambiguous_speech or self.last_vad_active or
                    self.gate.active and not self.gate.complete)

    def can_heuristically_exclude_voice(self) -> bool:
        with self.lock:
            return not self.inflight and not self.last_vad_active and not self.ambiguous_speech and not self.gate.active

    def feed(self, chunk: PCMChunk) -> None:
        try:
            current_vad = self.activity.observe_float32_stereo(chunk.frames)
        except Exception:
            from .contracts import EventKind
            self.emit(Envelope(EventKind.CAPTURE_HEALTH,
                 self.target.session_id if self.target else '',self.clock(),capture_ok=False))
            return
        with self.lock:
            if self.closed:
                return
            if current_vad.frames_analyzed:
                if not current_vad.active:
                    self.last_vad_active = False
                elif not self.last_vad_active:
                    self.last_vad_rise_ms = chunk.at_ms
                    self.last_vad_active = True
                    if (self.target is not None and chunk.at_ms >= max(
                            self.last_boundary_ms, self.target.text_first_seen_ms-2000)):
                        self.fresh_onset = True
            self.tail_ms = current_vad.tail_silence_ms
            if self.target is None or not self.history.healthy:
                return
            if current_vad.active and not self.gate.active:
                self.ambiguous_speech = True
            if self.inflight or (self.last_sample_ms >= 0 and
                                 chunk.at_ms < self.last_sample_ms + self.interval_ms):
                return
            target = self.target
            self.last_sample_ms = chunk.at_ms
            self.inflight = True
            # Include the early onset and partial text playback in the window.
            samples = self.history.since(target.text_first_seen_ms - 2000)
        self.pool.submit(self._infer, target, samples)

    def _infer(self, target: TargetLine, samples: tuple[PCMChunk, ...]) -> None:
        try:
            hypothesis = self.asr.recognize(samples)
            with self.lock:
                if self.target != target or self.closed or not self.matcher:
                    return
                match = self.matcher.observe(hypothesis)
                # Voice-asset assignment indicates expected playback, NOT that
                # arbitrary BGM/SE belongs to that line. Require target-matched
                # acoustic evidence for the actual native onset.
                attributed = self.fresh_onset and match.matched_chars >= 2
                if attributed and match.onset_score >= self.gate.policy.onset_score:
                    self.ambiguous_speech = False
                emitted = self.gate.observe(AudioEvidence(
                    target.session_id, target.line_epoch, target.cancel_token,
                    self.clock(), self.utterance_id, self.history.healthy,
                    attributed,
                    match.onset_score, self.fresh_onset, self.tail_ms,
                    match.coverage, match.tail_confidence, match.monotonic,
                    metadata_voice_id_matches=False,
                ))
            self.audit('asr_progress',{
                'session_id':target.session_id,'line_epoch':target.line_epoch,
                'at_ms':self.clock(),'asr_coverage':match.coverage,
                'asr_tail_confidence':match.tail_confidence,
                'asr_order_consistent':match.monotonic,
                'vad_tail_silence_ms':self.tail_ms,
                'inference_samples':sum(len(c.frames) for c in samples)//8,
                'native_attributed':attributed,
            })
            for event in emitted:
                self.emit(event)
        except Exception:
            # An ASR failure is a capture-analysis failure, never ABSENT.
            # A cancelled *previous* line's decoding failure cannot poison
            # the next line's independently owned capture/ASR generation.
            from .contracts import EventKind
            with self.lock:
                current = not self.closed and self.target == target
            if current:
                self.emit(Envelope(EventKind.CAPTURE_HEALTH, target.session_id, self.clock(), capture_ok=False))
        finally:
            with self.lock:
                self.inflight = False

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.target = None
        self.pool.shutdown(wait=True, cancel_futures=True)
