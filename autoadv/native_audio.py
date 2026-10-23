"""Process-scoped native audio recognition for non-TTS audio-judgment utterances.

Actual audio is captured by Windows Process Loopback, then converted into
immutable alignment/timing/acoustic evidence. NativeAudio never owns dialogue
completion or a fallback deadline; the pure evidence model assigns the current
ENDED / PLAYING / INCONCLUSIVE assessment and Controller owns progression.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Callable
import time
from .audio_buffer import AudioHistory, PCMChunk
from .audio_match import TargetMatcher, VoiceActivity
from .audio_evidence import VoiceEvidence
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
    DEFAULT_REPLAY_HISTORY_MS = 10000
    CAPTURE_HISTORY_MARGIN_MS = 1000
    def __init__(self, pid: int, helper: Path, model: Path, tokens: Path,
                 on_error: Callable[[Token | None, str], None],
                 *, recognizer=None, activity=None, g2p=None, clock=None,
                 replay_lookback_ms: int = DEFAULT_REPLAY_HISTORY_MS,
                 on_pcm: Callable[[Token, int], None] | None = None,
                 on_inference: Callable[[Token, dict], None] | None = None,
                 on_evidence: Callable[[Token, VoiceEvidence], None] | None = None):
        self.pid = pid
        self.on_error = on_error
        self.on_pcm = on_pcm
        self.on_inference = on_inference
        self.on_evidence = on_evidence
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        if replay_lookback_ms < 0:
            raise ValueError('native replay lookback must be nonnegative')
        # Rolling history exists only to recover bounded audio immediately
        # before an externally observed HOOK. The active Utterance interval is
        # retained separately below and therefore is never truncated to this
        # replay window.
        self.history = AudioHistory(horizon_ms=max(
            self.DEFAULT_REPLAY_HISTORY_MS,
            int(replay_lookback_ms) + self.CAPTURE_HISTORY_MARGIN_MS))
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
        self.vad_end_ms = -1
        self.vad_onset_ms = -1
        self.vad_tail_ms = 0
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
        self._vad_active = False
        self._vad_contains_speech = False
        self._pcm_reported = False
        self._latest_evidence: VoiceEvidence | None = None
        self._last_target_prefix_units = 0
        self._last_target_matches = 0
        self._last_target_token_ms = -1

    def start(self):
        # Capture starts before the first HOOK: retain the first voice's PCM.
        # ProcessCapture.start() initializes AudioHistory, so restore the
        # already-established observation boundary immediately afterwards.
        self.boundary()
        self._capture.start()
        self.history.retain_from(self.window_start_ms)

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
            self.history.retain_from(self.window_start_ms)
            self._reset_observation()

    def _reset_observation(self):
        self._inference_at = -1
        self._pcm_reported = False
        self._vad_active = False
        self._vad_contains_speech = False
        self.vad_onset_ms = -1
        self.vad_end_ms = -1
        self.vad_tail_ms = 0
        self._latest_evidence = None
        self._last_target_prefix_units = 0
        self._last_target_matches = 0
        self._last_target_token_ms = -1
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
            # The rolling pre-HOOK history is deliberately short, but once a
            # line is admitted its ASR window owns the complete interval. This
            # prevents a long voiced line from silently losing its target
            # prefix after the replay horizon expires.
            self.history.retain_from(self.window_start_ms)
            self._reset_observation()
            # Pre-HOOK replay may help END attribution, but it must not claim
            # that the target is currently progressing.  Only target tokens
            # positioned after the accepted HOOK can establish PLAYING.
            self._last_target_token_ms = self.start_ms
            # ProcessCapture pushes a packet before calling feed(). Replaying
            # under this lock makes an in-flight packet idempotent.
            buffered = self.history.since(self.window_start_ms)
            pending = [self._consume_locked(c, replay=True) for c in buffered]
            if buffered:
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
                    self._inference_at = buffered[-1].at_ms
                    pending.append((token, generation, '', None, samples,
                                    self.vad_tail_ms, self.vad_end_ms,
                                    self._vad_active, self._vad_contains_speech))
        for item in pending:
            self._dispatch(item)
        # A capture failure before HOOK reception must not disappear merely
        # because the earlier health event had no active utterance token.
        with self._lock:
            failure = self._capture_error if self.token == token else None
        if failure:
            self.on_error(token, failure)

    def finish_line(self, token: Token) -> bool:
        """Detach a Controller-completed line while keeping capture continuous.

        This is lifecycle ownership only: it does not claim that audio ended.
        """
        with self._lock:
            if token != self.token:
                return False
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.matcher = None
            self.history.release_retention()
            self._reset_observation()
            return True

    def cancel(self):
        with self._lock:
            self._generation += 1
            self._inflight = False
            self._inflight_generation = None
            self.token = None
            self.matcher = None
            self.history.release_retention()

    def health(self, status: CaptureHealth):
        """Record transport health without turning heartbeat into audio evidence.

        A healthy heartbeat after PCM stops can clear a transient PLAYING
        observation, but it never fabricates an endpoint or an absent voice.
        """
        refreshed = None
        token = None
        with self._lock:
            if status.healthy:
                if status.pcm_frames:
                    self._capture_pcm_packets += 1
                    self._capture_pcm_frames += status.pcm_frames
                else:
                    self._capture_heartbeats += 1
                    latest = self._latest_evidence
                    if (not self._inflight and self.token is not None and latest is not None
                            and latest.target_progressed):
                        refreshed = replace(latest, target_progressed=False)
                        self._latest_evidence = refreshed
                        token = self.token
            else:
                self._capture_error = status.reason
                token = self.token
        if refreshed is not None and token is not None and self.on_evidence is not None:
            self.on_evidence(token, refreshed)
            return
        if not status.healthy and token is not None:
            # Before the first line there is no token to notify. begin() replays
            # the failure to the actual line; no silence/voice conclusion is made.
            self.on_error(token, status.reason)

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
        # The Utterance boundary owns the ASR window. VAD may rise more than
        # once inside one voiced line (pauses, whispering, BGM interaction), so
        # a later rise must never redefine the line and discard earlier speech.
        # Bounded pre-HOOK replay for external progression is already expressed
        # by ``window_start_ms`` in begin().
        return self.window_start_ms

    def _consume_locked(self, chunk: PCMChunk, *, replay: bool = False):
        """Record one PCM packet and optionally schedule one evidence snapshot.

        VAD and capture are observations only. They never decide that the target
        voice started, ended, or should keep a text deadline open.
        """
        if (self._closed or self.token is None or
                chunk.at_ms < self.window_start_ms or
                chunk.at_ms < self._last_processed_ms or
                (chunk.at_ms == self._last_processed_ms and
                 chunk is self._last_processed_chunk)):
            return None
        self._last_processed_ms = chunk.at_ms
        self._last_processed_chunk = chunk
        samples = None
        pcm_frames = None
        token, generation = self.token, self._generation
        try:
            activity = self.activity.observe_float32_stereo(chunk.frames)
        except Exception as exc:
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
                target_acoustic_activity = bool(activity.active or activity.contains_speech)
                if target_acoustic_activity and self.vad_onset_ms < 0:
                    self.vad_onset_ms = chunk.at_ms
                self._vad_active = bool(activity.active)
                self._vad_contains_speech = bool(activity.contains_speech)
                if target_acoustic_activity:
                                self.vad_end_ms = -1
                elif self.vad_onset_ms >= 0 and activity.tail_silence_ms > 0:
                    # This is a process-mix VAD observation, not a target-voice
                    # endpoint assertion.
                    self.vad_end_ms = chunk.at_ms - activity.tail_silence_ms
            self.vad_tail_ms = int(activity.tail_silence_ms)

            if activity.frames_analyzed and not replay:
                # Cumulative ASR runs on one fixed cadence. VAD influences only
                # endpoint evidence, never when recognition is allowed to run.
                enough_pcm = chunk.at_ms - self.window_start_ms >= 300
                decode_due = (self._inference_at < 0 or
                              chunk.at_ms - self._inference_at >= 500)
                if not self._inflight and enough_pcm and decode_due:
                    candidate_samples = self.history.since(self._sample_start())
                    if candidate_samples:
                        self._inflight = True
                        self._inflight_generation = generation
                        self._inference_at = chunk.at_ms
                        samples = candidate_samples
        return (token, generation, error, pcm_frames, samples,
                self.vad_tail_ms, self.vad_end_ms,
                self._vad_active, self._vad_contains_speech)

    def _dispatch(self, item):
        if item is None:
            return
        (token, generation, error, pcm_frames, samples,
         sampled_vad_tail_ms, sampled_end_ms,
         sampled_vad_active, sampled_contains_speech) = item
        # All callbacks are outside the lock; a callback may synchronously
        # complete the line and invalidate this generation.
        with self._lock:
            if generation != self._generation or token != self.token:
                return
        if pcm_frames is not None and self.on_pcm is not None:
            self.on_pcm(token, pcm_frames)
        if error:
            self.on_error(token, error)
        if samples:
            with self._lock:
                if generation != self._generation or token != self.token:
                    return
            submitted_ns = time.perf_counter_ns() if self.on_inference else 0
            try:
                self._pool.submit(
                    self._infer, token, generation, samples, submitted_ns,
                    sampled_vad_tail_ms, sampled_end_ms,
                    sampled_vad_active, sampled_contains_speech)
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
        """Map an ASR-waveform offset onto captured packet time.

        Packet gaps remain gaps; they are never counted as target-relative
        acoustic time merely because the recognizer received concatenated PCM.
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

    @classmethod
    def _token_timing_observations(cls, samples, result: ASRResult,
                                   observed_token_count: int
                                   ) -> tuple[int | None, int | None]:
        """Return raw target-token timing observations on the capture timeline.

        SenseVoice timestamps are token starts. A following token therefore
        bounds the selected target prefix but is not itself declared to be the
        acoustic end of the target voice.
        """
        if not samples or observed_token_count <= 0 or not result.timestamps:
            return None, None
        timestamps = result.timestamps
        if (observed_token_count > len(timestamps)
                or any(t < 0 for t in timestamps)
                or any(a > b for a, b in zip(timestamps, timestamps[1:]))):
            return None, None
        total_audio_ms = sum(chunk.duration_ms for chunk in samples)
        last_offset_ms = timestamps[observed_token_count - 1] * 1000.0
        if last_offset_ms > total_audio_ms + 1e-6:
            return None, None
        last_wall = cls._audio_offset_to_wall_ms(samples, last_offset_ms)
        target_last_ms = int(round(last_wall)) if last_wall is not None else None
        if observed_token_count >= len(timestamps):
            return target_last_ms, None
        boundary_offset_ms = timestamps[observed_token_count] * 1000.0
        if boundary_offset_ms < last_offset_ms or boundary_offset_ms > total_audio_ms + 1e-6:
            return target_last_ms, None
        boundary_wall = cls._audio_offset_to_wall_ms(samples, boundary_offset_ms)
        if boundary_wall is None:
            return target_last_ms, None
        return target_last_ms, int(round(boundary_wall))

    def _infer(self, token, generation, samples, submitted_ns=0,
               submitted_vad_tail_ms=None, submitted_end_ms=None,
               submitted_vad_active=False, submitted_contains_speech=False):
        started_ns = time.perf_counter_ns() if self.on_inference else 0
        if not submitted_ns:
            submitted_ns = started_ns
        try:
            result = _coerce_asr_result(self.asr.recognize(samples))
            decoded_ns = time.perf_counter_ns() if self.on_inference else 0
            evidence = None
            with self._lock:
                if (self._closed or self._generation != generation or token != self.token
                        or not self.matcher or not self.history.healthy):
                    return
                alignment = self.matcher.align(result.text, tokens=result.tokens)
                target_last_ms, target_boundary_ms = self._token_timing_observations(
                    samples, result, alignment.observed_token_count)
                audio_ms = sum(chunk.duration_ms for chunk in samples)
                capture_end_ms = int(samples[-1].at_ms) if samples else None
                tail_at_decode = max(0, int(self.vad_tail_ms if submitted_vad_tail_ms is None
                                             else submitted_vad_tail_ms))
                vad_end = (self.vad_end_ms if submitted_end_ms is None
                           else submitted_end_ms)
                target_progressed = bool(
                    target_last_ms is not None
                    and target_last_ms > self._last_target_token_ms
                    and (alignment.phonetic_prefix_units > self._last_target_prefix_units
                         or alignment.phonetic_matches > self._last_target_matches))
                if target_last_ms is not None:
                    self._last_target_token_ms = max(self._last_target_token_ms, target_last_ms)
                self._last_target_prefix_units = max(
                    self._last_target_prefix_units, alignment.phonetic_prefix_units)
                self._last_target_matches = max(
                    self._last_target_matches, alignment.phonetic_matches)
                evidence = VoiceEvidence(
                    alignment=alignment,
                    target_last_token_ms=target_last_ms,
                    target_boundary_ms=target_boundary_ms,
                    target_progressed=target_progressed,
                    vad_onset_ms=(self.vad_onset_ms if self.vad_onset_ms >= 0 else None),
                    vad_tail_ms=tail_at_decode,
                    vad_end_ms=(int(vad_end) if vad_end is not None and vad_end >= 0 else None),
                )
                self._latest_evidence = evidence

            if self.on_inference and evidence is not None:
                a = evidence.alignment
                measurements = {
                    'queue_ms': round(max(0, started_ns - submitted_ns) / 1_000_000, 3),
                    'decode_ms': round(max(0, decoded_ns - started_ns) / 1_000_000, 3),
                    'audio_ms': round(audio_ms, 3),
                    'coverage': round(a.coverage, 4),
                    'phonetic_coverage': round(a.phonetic_coverage, 4),
                    'matched_chars': a.matched_chars,
                    'matched_phonemes': a.matched_phonemes,
                    'edit_count': a.edit_count,
                    'phonetic_edit_count': a.phonetic_edit_count,
                    'reading_rank': a.reading_rank,
                    'asr_token_timestamp_count': len(result.timestamps),
                    'target_token_count': a.observed_token_count,
                    'target_last_token_ms': evidence.target_last_token_ms,
                    'target_boundary_ms': evidence.target_boundary_ms,
                    'target_progressed': evidence.target_progressed,
                    'vad_active': bool(submitted_vad_active),
                    'vad_contains_speech': bool(submitted_contains_speech),
                    'vad_onset_ms': evidence.vad_onset_ms,
                    'vad_tail_ms': evidence.vad_tail_ms,
                    'vad_end_ms': evidence.vad_end_ms,
                }
                try:
                    self.on_inference(token, measurements)
                except Exception:
                    pass

            if self.on_evidence is not None and evidence is not None:
                self.on_evidence(token, evidence)
        except Exception as exc:
            self.on_error(token, 'ASR failure: ' + str(exc))
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
