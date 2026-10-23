"""Process-scoped native audio recognition for non-TTS audio-judgment utterances.

Actual audio is captured by Windows Process Loopback, then analyzed in one
worker. Pronunciation alignment, ASR token timing/stability, and acoustic cues
are combined for the active Utterance. Global mix silence is not required;
neither capture silence nor a timer asserts voice absence.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Callable
import time
from .audio_buffer import AudioHistory, PCMChunk
from .audio_match import TargetMatcher, VoiceActivity
from .conversation import Token
from .process_audio import ProcessCapture, CaptureHealth


@dataclass(frozen=True)
class ASRResult:
    text: str
    tokens: tuple[str, ...] = ()
    timestamps: tuple[float, ...] = ()


def _coerce_asr_result(value) -> ASRResult:
    if isinstance(value, ASRResult):
        return value
    if isinstance(value, str):
        return ASRResult(value)
    return ASRResult(
        str(getattr(value, 'text', '')),
        tuple(str(x) for x in (getattr(value, 'tokens', ()) or ())),
        tuple(float(x) for x in (getattr(value, 'timestamps', ()) or ())),
    )


class JapaneseASR:
    def __init__(self, model: Path, tokens: Path):
        if not model.is_file() or not tokens.is_file():
            raise FileNotFoundError('configured ASR model and tokens required')
        import sherpa_onnx
        self.engine = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model), tokens=str(tokens), num_threads=2, use_itn=True, debug=False)

    def recognize(self, chunks: tuple[PCMChunk, ...]) -> ASRResult:
        import numpy as np
        if not chunks:
            return ASRResult('')
        if any((c.sample_rate, c.channels, c.sample_width_bytes) != (48000, 2, 4) for c in chunks):
            raise ValueError('ASR requires 48 kHz stereo float32 PCM')
        stereo = np.concatenate([np.frombuffer(c.frames, dtype='<f4').reshape(-1, 2) for c in chunks])
        mono = stereo.mean(axis=1).astype('float32')
        stream = self.engine.create_stream()
        stream.accept_waveform(48000, mono)
        self.engine.decode_stream(stream)
        result = stream.result
        return ASRResult(
            str(result.text),
            tuple(str(x) for x in (getattr(result, 'tokens', ()) or ())),
            tuple(float(x) for x in (getattr(result, 'timestamps', ()) or ())),
        )


class NativeAudio:
    """Owns one process's capture and one active line; never injects game input."""
    def __init__(self, pid: int, helper: Path, model: Path, tokens: Path,
                 notify: Callable[[Token | None, str, str], None],
                 *, recognizer=None, activity=None, g2p=None, clock=None,
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
        if g2p is not None:
            self.g2p = g2p
        elif recognizer is None:
            # Product native-audio readiness includes the initial Japanese G2P
            # backend. A missing native dependency must not silently change the
            # matching contract to surface-only text.
            from .g2p import HaqumeiG2P
            self.g2p = HaqumeiG2P()
        else:
            # Unit/investigation recognizers can explicitly exercise the
            # surface-only fallback without installing native extras.
            self.g2p = None
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
        self._capture_pcm_packets = 0
        self._capture_pcm_frames = 0
        self._capture_heartbeats = 0
        self._capture_error: str | None = None
        self._vad_positive_packets = 0
        self._vad_input_peak = 0.0
        self._capture_stereo_peak = 0.0
        self._last_vad = False
        self._pcm_reported = False
        self._last_active_ms = -1
        self._inference_submitted_ms = -1
        self._deadline_grace_from_ms = -1
        self._quiet_notified = False
        self._tail_decoded = False
        self._best_match_coverage = 0.0
        self._last_matching_asr_ms = -1

    def start(self):
        # Capture starts before the first HOOK: retain the first voice's PCM.
        self.boundary()
        self._capture.start()

    def boundary(self, at_ms: int | None = None):
        """An observed advance opens the next interval without restarting capture.

        ``at_ms`` can preserve an already-known AutoAdv input boundary. The old
        token and pending ASR results lose ownership immediately; retained PCM
        is replayed only after a later HOOK establishes the new text.
        """
        boundary_ms = self.clock() if at_ms is None else int(at_ms)
        if boundary_ms <= 0:
            raise ValueError('audio boundary must be a positive monotonic timestamp')
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.matcher = None
            self.boundary_ms = boundary_ms
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
        self._inference_submitted_ms = -1
        self._deadline_grace_from_ms = -1
        self._quiet_notified = False
        self._tail_decoded = False
        self._best_match_coverage = 0.0
        self._last_matching_asr_ms = -1
        self._vad_positive_packets = 0
        self._vad_input_peak = 0.0
        self._capture_stereo_peak = 0.0
        if hasattr(self.activity, 'reset'):
            self.activity.reset()

    def begin(self, token: Token, match_text: str, *, replay_from_ms: int | None = None):
        """Bind retained PCM to one HOOK-confirmed utterance.

        AutoAdv input establishes an exact boundary through ``boundary()``.
        When the game advances externally, ``replay_from_ms`` may request a
        bounded pre-HOOK replay from the continuous process history instead.
        The ASR matcher still has to associate that audio with ``match_text``;
        the replay window itself is not evidence that all PCM belongs to it.
        """
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = token
            self.matcher = TargetMatcher(match_text, g2p=self.g2p)
            self.start_ms = self.clock()
            if replay_from_ms is not None:
                self.window_start_ms = max(0, int(replay_from_ms))
            else:
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
            if buffered and not self.completed:
                # Decode the *whole* replayed window once, rather than running
                # a misleading inference on its first short packet. VAD does
                # not gate ASR: whisper/mixtures may be lexically decodable even
                # when process-wide VAD never produces a clean speech onset.
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
        # A capture failure before HOOK reception must not disappear merely
        # because the earlier health event had no active utterance token.
        with self._lock:
            failure = self._capture_error if self.token == token else None
        if failure:
            self.notify(token, 'error', failure)

    def is_confirmed_playing(self, token: Token) -> bool:
        """Recent VAD activity that still has current-target lexical support.

        BGM/ambient speech may keep process-wide VAD active forever. It can
        defer a fallback only while target-aligned ASR progress is also recent.
        These windows are freshness bounds, not end-of-speech thresholds.
        """
        with self._lock:
            now = self.clock()
            if (token != self.token or not self.attributed or self._last_matching_asr_ms < 0
                    or now - self._last_matching_asr_ms > 1200):
                return False
            recent_vad = (self._last_vad and self._last_active_ms >= 0
                          and now - self._last_active_ms <= 1000)
            # Newly increasing target-aligned ASR is itself positive evidence
            # that the target voice is still unfolding, even if VAD misses it.
            recent_progress = now - self._last_matching_asr_ms <= 700
            return bool(recent_vad or recent_progress)

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
            # A real-PCM ASR job already in flight gets one bounded chance
            # to resolve before the optional uncertain-text fallback. VAD is
            # not a prerequisite: quiet/mixture speech can be lexically useful
            # even when process-wide VAD never marks it active. Pending analysis
            # is still not native completion and may only defer for this bound.
            candidate_pending = bool(self._inflight and self._pcm_reported
                        and self._inference_submitted_ms >= 0
                        and now - self._inference_submitted_ms <= 2000)
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
        with self._lock:
            if status.healthy:
                if status.pcm_frames:
                    self._capture_pcm_packets += 1
                    self._capture_pcm_frames += status.pcm_frames
                else:
                    self._capture_heartbeats += 1
                return
            self._capture_error = status.reason
            token = self.token
        # Before the first line there is no token to notify. begin() replays
        # the failure to the actual line; no silence/voice conclusion is made.
        if token is not None:
            self.notify(token, 'error', status.reason)

    def capture_status(self) -> dict:
        """Minimal product-boundary diagnosis when a line must hand off.

        Helper heartbeats confirm only transport responsiveness; PCM frame
        counts do not assert foreground speech or a matching ASR segment.
        """
        with self._lock:
            return dict(scope='capture_since_start',
                        pcm_packets=self._capture_pcm_packets,
                        pcm_frames=self._capture_pcm_frames,
                        heartbeats=self._capture_heartbeats,
                        vad_positive_packets=self._vad_positive_packets,
                        vad_input_peak=round(self._vad_input_peak, 6),
                        capture_stereo_peak=round(self._capture_stereo_peak, 6),
                        failure=self._capture_error)

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
            self._vad_input_peak = max(self._vad_input_peak, activity.mono_peak)
            self._capture_stereo_peak = max(self._capture_stereo_peak, activity.stereo_peak)
            if activity.frames_analyzed and (activity.active or activity.contains_speech):
                self._vad_positive_packets += 1
            if activity.frames_analyzed and not self._pcm_reported:
                self._pcm_reported = True
                pcm_frames = len(chunk.frames) // 8
            if activity.frames_analyzed:
                if (activity.active or activity.contains_speech) and not self._last_vad:
                    self.rise_ms = chunk.at_ms
                self._last_vad = activity.active
                if activity.active or activity.contains_speech:
                    self._tail_decoded = False
                    self.voice_end_estimate_ms = -1
                elif self.rise_ms >= 0 and activity.tail_silence_ms > 0:
                    # The interval endpoint is estimated from VAD frames and
                    # capture-receipt clock, *not* a device playback timestamp.
                    self.voice_end_estimate_ms = chunk.at_ms - activity.tail_silence_ms
            self.tail_ms = activity.tail_silence_ms
            if activity.frames_analyzed and activity.active and self.attributed:
                self._last_active_ms = self.clock()
                self._quiet_notified = False
            elif (activity.frames_analyzed and not activity.active and self.attributed
                  and activity.tail_silence_ms >= 400 and not self._quiet_notified):
                self._quiet_notified = True
                events.append('quiet')

            if activity.frames_analyzed and not self.completed and not replay:
                tail_due = (activity.tail_silence_ms >= 300 and
                            self.rise_ms >= 0 and not self._tail_decoded)
                # Periodic ASR is independent of VAD. A first decode waits for
                # a small PCM window to avoid spending work on a single packet.
                enough_pcm = (self.rise_ms >= self.window_start_ms or
                              chunk.at_ms - self.window_start_ms >= 300)
                periodic_due = (self._inference_at < 0 or
                                chunk.at_ms - self._inference_at >= 500)
                if not self._inflight and enough_pcm and (tail_due or periodic_due):
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

    @staticmethod
    def _audio_offset_to_wall_ms(samples, offset_ms: float) -> float | None:
        """Map concatenated ASR-audio offset back onto captured packet time.

        SenseVoice timestamps address the waveform passed to the recognizer.
        Process Loopback packets need not be wall-clock contiguous, so a gap
        between packets must not be counted as captured trailing audio.
        """
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

    @staticmethod
    def _length_evidence(progress) -> float:
        """Score target-relative ASR length without requiring exact wording.

        Surface length is useful when orthography is preserved; pronunciation
        length is more reliable across kanji/kana spelling changes.  Length is
        supporting evidence only and can never attribute unrelated speech.
        """
        ratios = (progress.length_ratio, progress.pronunciation_length_ratio)
        return max((max(0.0, min(1.0, 1.0 - abs(ratio - 1.0)))
                    for ratio in ratios if ratio > 0), default=0.0)

    @classmethod
    def _timestamp_endpoint(cls, samples, result: ASRResult,
                            observed_token_count: int) -> tuple[int, float, float]:
        """Return target-local endpoint estimate, trailing PCM, and confidence.

        SenseVoice exposes per-token timestamps in seconds. They are positions
        in the concatenated decode waveform, not device playback timestamps.
        A following token bounds the target locally. If the target is last, a
        conservative token-duration guard must itself be present in captured
        PCM before it counts as endpoint evidence. Packet gaps never strengthen
        this score.
        """
        if (not samples or observed_token_count <= 0 or not result.timestamps
                or observed_token_count > len(result.timestamps)):
            return -1, 0.0, 0.0
        usable = result.timestamps[:observed_token_count]
        if (not usable or any(t < 0 for t in usable)
                or any(a > b for a, b in zip(result.timestamps, result.timestamps[1:]))):
            return -1, 0.0, 0.0
        total_audio_ms = sum(chunk.duration_ms for chunk in samples)
        target_last_start_ms = usable[-1] * 1000.0
        if observed_token_count < len(result.timestamps):
            endpoint_offset_ms = result.timestamps[observed_token_count] * 1000.0
            if endpoint_offset_ms < target_last_start_ms:
                return -1, 0.0, 0.0
            trailing_ms = max(0.0, total_audio_ms - endpoint_offset_ms)
            score = .55 + .45 * min(1.0, trailing_ms / 220.0)
        else:
            endpoint_offset_ms = target_last_start_ms + 220.0
            if endpoint_offset_ms > total_audio_ms + 1e-6:
                return -1, 0.0, 0.0
            trailing_ms = max(0.0, total_audio_ms - endpoint_offset_ms)
            score = min(1.0, trailing_ms / 320.0)
        endpoint_ms = cls._audio_offset_to_wall_ms(samples, endpoint_offset_ms)
        if endpoint_ms is None:
            return -1, 0.0, 0.0
        return int(round(endpoint_ms)), trailing_ms, score

    def _infer(self, token, generation, samples, submitted_ns=0,
               submitted_tail_ms=None, submitted_end_ms=None):
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        try:
            result = _coerce_asr_result(self.asr.recognize(samples))
            decoded_ns = time.perf_counter_ns() if self.on_inference else 0
            events = []
            endpoint_ms = -1
            endpoint_source = ''
            trailing_after_target_ms = 0.0
            timestamp_score = 0.0
            with self._lock:
                if (self._closed or self._generation != generation or token != self.token
                        or not self.matcher or not self.history.healthy):
                    return
                progress = self.matcher.observe(result.text, tokens=result.tokens)
                target_progress = progress.attribution_score >= .72
                if (target_progress
                        and progress.phonetic_coverage > self._best_match_coverage + .02):
                    self._best_match_coverage = progress.phonetic_coverage
                    self._last_matching_asr_ms = self.clock()

                vad_seen = self.rise_ms >= self.window_start_ms
                attribution_evidence = min(1.0,
                    .94 * progress.attribution_score + (.06 if vad_seen else 0.0)
                    - .08 * progress.contradiction_score)
                if (not self.attributed
                        and progress.matched_phonemes >= min(4, max(1, progress.target_units))
                        and attribution_evidence >= .78
                        and progress.contradiction_score <= .30):
                    self.attributed = True
                    if vad_seen:
                        self._last_active_ms = self.clock()
                    self._last_matching_asr_ms = self.clock()
                    events.append('started')

                endpoint_ms, trailing_after_target_ms, timestamp_score = (
                    self._timestamp_endpoint(samples, result, progress.observed_token_count))
                tail_at_decode = self.tail_ms if submitted_tail_ms is None else submitted_tail_ms
                vad_score = min(1.0, max(0, tail_at_decode) / 400.0)
                stability_score = min(1.0, progress.tail_confidence)
                # Silence is a useful bonus, never a global-mix requirement.
                endpoint_score = max(timestamp_score, .75 * vad_score, .65 * stability_score)
                length_score = self._length_evidence(progress)
                # ASR is one source of evidence, not a transcription acceptance
                # test.  Target-relative progress/length, token timing or VAD,
                # and recognition consistency can jointly establish completion.
                # ``terminal_candidate`` remains diagnostic and deliberately is
                # not a hard gate: a bounded partial transcript may be enough
                # when independent endpoint evidence is strong.
                evidence_score = max(0.0, min(1.0,
                    .50 * progress.completion_score
                    + .18 * progress.attribution_score
                    + .16 * endpoint_score
                    + .10 * length_score
                    + .06 * float(progress.monotonic)
                    - .14 * progress.contradiction_score))
                sufficient_progress = (
                    max(progress.phonetic_coverage, progress.coverage) >= .55
                    and progress.matched_phonemes >= min(4, max(1, progress.target_units))
                )
                if (self.attributed and sufficient_progress
                        and endpoint_score >= .45 and evidence_score >= .72):
                    self.completed = True
                    events.append('completed')
                    if endpoint_ms >= 0:
                        voice_end_estimate = endpoint_ms
                        endpoint_source = 'asr_timestamp'
                    else:
                        voice_end_estimate = (self.voice_end_estimate_ms if submitted_end_ms is None
                                              else submitted_end_ms)
                        endpoint_source = ('asr_vad' if voice_end_estimate >= 0
                                           else 'asr_stability')
                else:
                    voice_end_estimate = -1
            if self.on_voice_end and 'completed' in events and voice_end_estimate >= 0:
                try:
                    self.on_voice_end(token, {'estimated_end_ms': voice_end_estimate,
                                              'estimated_onset_ms': (self.rise_ms
                                                                     if self.rise_ms >= 0 else None),
                                              'source': endpoint_source})
                except Exception:
                    pass
            if self.on_inference:
                audio_ms = sum(chunk.duration_ms for chunk in samples)
                measurements = {
                    'queue_ms': round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    'decode_ms': round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    'audio_ms': round(audio_ms, 3),
                    'coverage': round(progress.coverage, 4),
                    'phonetic_coverage': round(progress.phonetic_coverage, 4),
                    'attribution_score': round(progress.attribution_score, 4),
                    'completion_score': round(progress.completion_score, 4),
                    'contradiction_score': round(progress.contradiction_score, 4),
                    'endpoint_score': round(endpoint_score, 4),
                    'length_score': round(length_score, 4),
                    'evidence_score': round(evidence_score, 4),
                    'asr_token_timestamp_count': len(result.timestamps),
                    'target_token_count': progress.observed_token_count,
                    'trailing_after_target_ms': round(trailing_after_target_ms, 3),
                    'monotonic': progress.monotonic,
                    'tail_confidence': progress.tail_confidence,
                    'matched_chars': progress.matched_chars,
                    'matched_phonemes': progress.matched_phonemes,
                    'edit_count': progress.edit_count,
                    'phonetic_edit_count': progress.phonetic_edit_count,
                    'length_ratio': round(progress.length_ratio, 4),
                    'reading_rank': progress.reading_rank,
                    'terminal_candidate': progress.terminal_candidate,
                }
                try:
                    self.on_inference(token, measurements)
                except Exception:
                    pass
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
