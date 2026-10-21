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
                 *, recognizer=None, activity=None, clock=None,
                 on_pcm: Callable[[Token, int], None] | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None,
                 on_voice_end: Callable[[Token, dict], None] | None = None):
        self.pid = pid
        self.notify = notify
        self.on_pcm = on_pcm
        self.on_inference = on_inference
        self.on_voice_end = on_voice_end
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.history = AudioHistory(horizon_ms=10000)
        self.asr = recognizer if recognizer is not None else JapaneseASR(model, tokens)
        self.activity = activity or VoiceActivity()
        self._lock = RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='autoadv-asr')
        self._capture = ProcessCapture(pid, helper, self.history, self.feed, self.health, self.clock)
        self.token: Token | None = None
        self.matcher: TargetMatcher | None = None
        self.start_ms = 0
        self.boundary_ms = 0
        self.window_start_ms = 0
        self._awaiting_hook = False
        self._last_processed_ms = -1
        self._last_processed_chunk: PCMChunk | None = None
        self.voice_end_estimate_ms = -1
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
        self._pcm_reported = False
        self._last_active_ms = -1
        self._last_unassigned_voice_ms = -1
        self._unattributed_since_ms = -1
        self._inference_submitted_ms = -1
        self._deadline_grace_from_ms = -1
        self._quiet_notified = False
        self._tail_decoded = False

    def start(self):
        # Capture starts before the first HOOK: retain the first voice's PCM.
        self.boundary()
        self._capture.start()

    def boundary(self):
        """An advance opens the *next* observation interval, not a new recorder.

        The old token and all pending ASR results lose ownership immediately.
        PCM already retained in history is not destroyed, and may be matched
        only after a subsequent HOOK establishes the new text.
        """
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.matcher = None
            self.boundary_ms = self.clock()
            self.window_start_ms = self.boundary_ms
            self._awaiting_hook = True
            self._last_processed_ms = -1
            self._last_processed_chunk = None
            self._reset_observation()

    def _reset_observation(self):
        self.active = self.attributed = self.completed = False
        self._inference_at = -1
        self._pcm_reported = False
        self._last_vad = False
        self.rise_ms = -1
        self.voice_end_estimate_ms = -1
        self.tail_ms = 0
        self._last_active_ms = -1
        self._last_unassigned_voice_ms = -1
        self._unattributed_since_ms = -1
        self._inference_submitted_ms = -1
        self._deadline_grace_from_ms = -1
        self._quiet_notified = False
        self._tail_decoded = False
        if hasattr(self.activity, 'reset'):
            self.activity.reset()

    def begin(self, token: Token, match_text: str):
        """Bind captured post-input PCM to a HOOK text, including early voice.

        Replay and live feeding are serialized with the VAD lock. An unbound
        interval is never labelled as this dialogue until text is available.
        If there was no preceding observed input, do not claim old PCM.
        """
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = token
            self.matcher = TargetMatcher(match_text)
            self.start_ms = self.clock()
            self.window_start_ms = (self.boundary_ms if self._awaiting_hook
                                    else self.start_ms)
            self._awaiting_hook = False
            self._last_processed_ms = -1
            self._last_processed_chunk = None
            self._reset_observation()
            # ProcessCapture pushes a packet before calling feed(). Replaying
            # under this lock makes an in-flight packet idempotent.
            buffered = self.history.since(self.window_start_ms)
            pending = [self._consume_locked(c, replay=True) for c in buffered]
            if buffered and self.rise_ms >= 0 and not self.completed:
                # Decode the *whole* replayed window once, rather than running
                # a misleading inference on its first short packet.
                since = self._sample_start()
                samples = self.history.since(since)
                if samples:
                    generation = self._generation
                    self._inflight = True
                    self._inflight_generation = generation
                    self._inference_submitted_ms = self.clock()
                    self._inference_at = buffered[-1].at_ms
                    self._tail_decoded = self.tail_ms >= 300
                    pending.append((token, generation, [], '', None, samples,
                                    self.tail_ms, self.voice_end_estimate_ms))
        for item in pending:
            self._dispatch(item)

    def is_confirmed_playing(self, token: Token) -> bool:
        """Positive *recent* correlated VAD activity; never proof of voice absence.

        Old PCM or a heartbeat cannot keep a line active indefinitely. The
        1000ms window is a freshness bound only, not an end-of-speech threshold.
        """
        with self._lock:
            return bool(token == self.token and self.attributed and self._last_vad
                        and self._last_active_ms >= 0
                        and self.clock() - self._last_active_ms <= 1000)

    def holds_deadline(self, token: Token) -> bool:
        """Only positive correlated activity or a bounded pending ASR analysis
        can defer the text deadline. An unrelated endless voice loop cannot
        keep the dialogue alive indefinitely; pending is not native completion.
        """
        with self._lock:
            if token != self.token or self.completed:
                return False
            now = self.clock()
            if self.is_confirmed_playing(token):
                return True
            # A captured speech candidate must get a chance to be analyzed
            # before the optional uncertain-text fallback. Pending alone does
            # not identify the foreground or prove it is still playing.
            candidate_pending = bool(self._inflight
                        and self._inference_submitted_ms >= 0
                        and now - self._inference_submitted_ms <= 2000
                        and self._last_unassigned_voice_ms >= 0
                        and now - self._last_unassigned_voice_ms <= 2000
                        and self._unattributed_since_ms >= 0
                        and now - self._unattributed_since_ms <= 2000)
            if not candidate_pending:
                return False
            if self._deadline_grace_from_ms < 0:
                self._deadline_grace_from_ms = now
            return now - self._deadline_grace_from_ms <= 2000

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

    def _sample_start(self) -> int:
        # The sender's interval is the earliest allowed observation. A voice
        # starting late should not be decoded together with earlier chatter.
        since = self.window_start_ms
        if self.rise_ms >= self.start_ms + 500:
            since = max(since, self.rise_ms - 200)
        return since

    def _consume_locked(self, chunk: PCMChunk, *, replay: bool = False):
        # Caller owns the VAD/utterance lock. Only the active interval's PCM
        # can change this token, including packets received before its HOOK.
        if (self._closed or self.token is None or
                chunk.at_ms < self.window_start_ms or
                chunk.at_ms < self._last_processed_ms or
                (chunk.at_ms == self._last_processed_ms and
                 chunk is self._last_processed_chunk)):
            return None
        self._last_processed_ms = chunk.at_ms
        self._last_processed_chunk = chunk
        events: list[str] = []
        samples = None
        pcm_frames = None
        token, generation = self.token, self._generation
        try:
            activity = self.activity.observe_float32_stereo(chunk.frames)
        except Exception as exc:
            events.append('error')
            error = 'VAD failure: ' + str(exc)
        else:
            error = ''
            if activity.frames_analyzed and not self._pcm_reported:
                self._pcm_reported = True
                pcm_frames = len(chunk.frames) // 8
            if activity.frames_analyzed:
                if (activity.active or activity.contains_speech) and not self._last_vad:
                    self.rise_ms = chunk.at_ms
                self._last_vad = activity.active
                if activity.active or activity.contains_speech:
                    self._last_unassigned_voice_ms = self.clock()
                    if not self.attributed and self._unattributed_since_ms < 0:
                        self._unattributed_since_ms = self.clock()
                    self._tail_decoded = False
                    self.voice_end_estimate_ms = -1
                elif self.rise_ms >= 0 and activity.tail_silence_ms > 0:
                    # The interval endpoint is estimated from VAD frames and
                    # capture-receipt clock, *not* a device playback timestamp.
                    self.voice_end_estimate_ms = chunk.at_ms - activity.tail_silence_ms
            self.tail_ms = activity.tail_silence_ms
            if (not self.attributed and not self._inflight
                    and activity.tail_silence_ms >= 500):
                # A later independent voice candidate earns its own bounded
                # ASR opportunity after a meaningful silence interval.
                self._unattributed_since_ms = -1
            if activity.frames_analyzed and activity.active and self.attributed:
                self._last_active_ms = self.clock()
                self._quiet_notified = False
            elif (activity.frames_analyzed and not activity.active and self.attributed
                  and activity.tail_silence_ms >= 400 and not self._quiet_notified):
                self._quiet_notified = True
                events.append('quiet')

            if activity.frames_analyzed and self.rise_ms >= 0 and not self.completed and not replay:
                tail_due = (activity.tail_silence_ms >= 300 and
                            self.rise_ms >= 0 and not self._tail_decoded)
                if not self._inflight and (tail_due or self._inference_at < 0 or
                                           chunk.at_ms - self._inference_at >= 500):
                    self._inflight = True
                    self._inflight_generation = generation
                    self._inference_submitted_ms = self.clock()
                    self._inference_at = chunk.at_ms
                    if tail_due:
                        self._tail_decoded = True
                    samples = self.history.since(self._sample_start())
        return (token, generation, events, error, pcm_frames, samples,
                self.tail_ms, self.voice_end_estimate_ms)

    def _dispatch(self, item):
        if item is None:
            return
        (token, generation, events, error, pcm_frames, samples,
         sampled_tail_ms, sampled_end_ms) = item
        # All callbacks are outside the lock; UI callbacks may synchronously
        # accept an advance and invalidate this very generation.
        with self._lock:
            if generation != self._generation or token != self.token:
                return
        if pcm_frames is not None and self.on_pcm is not None:
            self.on_pcm(token, pcm_frames)
        for event in events:
            self.notify(token, event, error if event == 'error' else '')
        if samples:
            with self._lock:
                if generation != self._generation or token != self.token:
                    return
            submitted_ns = time.perf_counter_ns() if self.on_inference else 0
            try:
                self._pool.submit(self._infer, token, generation, samples, submitted_ns,
                                  sampled_tail_ms, sampled_end_ms)
            except RuntimeError:
                with self._lock:
                    if self._inflight_generation == generation:
                        self._inflight = False
                        self._inflight_generation = None

    def feed(self, chunk: PCMChunk):
        with self._lock:
            item = self._consume_locked(chunk)
        self._dispatch(item)

    def _infer(self, token, generation, samples, submitted_ns=0,
               submitted_tail_ms=None, submitted_end_ms=None):
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
                recent = self.rise_ms >= self.window_start_ms
                if (not self.attributed and recent and
                        progress.matched_chars >= min(3, len(self.matcher.target)) and
                        progress.onset_score >= .80 and
                        progress.edit_count <= max(1, progress.matched_chars // 6)):
                    self.attributed = True
                    self._last_active_ms = self.clock()
                    self._unattributed_since_ms = -1
                    events.append('started')
                # A complete lexical match plus observed trailing silence is
                # sufficient. Requiring a second identical ASR decode wastes
                # latency and can turn a valid first final recognition into a
                # hold when background speech contaminates the next window.
                # Never apply *later* VAD silence to an earlier ASR sample.
                # This snapshot belongs to exactly the PCM supplied for decode.
                tail_at_decode = self.tail_ms if submitted_tail_ms is None else submitted_tail_ms
                if (self.attributed and progress.terminal_candidate
                        and tail_at_decode >= 300):
                    self.completed = True
                    events.append('completed')
                    voice_end_estimate = (self.voice_end_estimate_ms if submitted_end_ms is None
                                          else submitted_end_ms)
                else:
                    voice_end_estimate = -1
            if self.on_voice_end and 'completed' in events and voice_end_estimate >= 0:
                try:
                    self.on_voice_end(token, {'estimated_end_ms': voice_end_estimate, 'estimated_onset_ms': self.rise_ms, 'source': 'asr_vad'})
                except Exception:
                    pass  # Optional metrics cannot change the product decision.
            if self.on_inference:
                audio_ms = sum(chunk.duration_ms for chunk in samples)
                measurements = {
                    'queue_ms': round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    'decode_ms': round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    'audio_ms': round(audio_ms, 3),
                    'coverage': round(progress.coverage, 4),
                    'monotonic': progress.monotonic,
                    'tail_confidence': progress.tail_confidence,
                    'matched_chars': progress.matched_chars,
                    'edit_count': progress.edit_count,
                    'length_ratio': round(progress.length_ratio, 4),
                    'terminal_candidate': progress.terminal_candidate,
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
