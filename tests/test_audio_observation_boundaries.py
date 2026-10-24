"""Packet boundaries and overlapping inference must not fabricate speech evidence."""
from __future__ import annotations

import threading
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_buffer import AudioHistory
from autoadv.audio_match import TargetAlignment, TargetMatcher, VoiceActivity, VADDecision, surface_key
from autoadv.g2p import PronunciationCandidate
from autoadv.audio_knowledge import (
    NativeAudioObservation, NativeVoiceKnowledge, reduce_native_voice_knowledge,
)
from autoadv.conversation import Controller, Target, Token
from autoadv.model import AdvanceMode, HookSelection, Profile
from autoadv.native_audio import ASRResult, NativeAudio
from autoadv.process_audio import CaptureHealth, ProcessCapture, HEADER


class IdentityG2P:
    def candidates(self, text, *, max_candidates=4):
        units = tuple(surface_key(text))
        return (PronunciationCandidate(units),) if units and max_candidates else ()


TEST_G2P = IdentityG2P()

def _native_audio(*args, **kwargs):
    kwargs.setdefault('g2p', TEST_G2P)
    return NativeAudio(*args, **kwargs)


class ProcessCaptureOwnershipTests(unittest.TestCase):
    def test_helper_without_initial_heartbeat_does_not_claim_readiness(self):
        from unittest.mock import patch
        from queue import Queue
        from threading import Event
        import autoadv.process_audio as process_audio

        class Stream:
            def __init__(self, initial):
                self.q = Queue()
                if initial is not None:
                    self.q.put(initial)
            def read(self, _size):
                return self.q.get(timeout=1)

        class Proc:
            def __init__(self, initial):
                self.stdout = Stream(initial)
                self.terminated = False
            def poll(self):
                return 0 if self.terminated else None
            def terminate(self):
                self.terminated = True
                self.stdout.q.put(b'')

        def new_capture(proc, observed):
            return ProcessCapture(11, Path('helper.dll'), AudioHistory(),
                                  lambda _: None, observed.append, clock=lambda: 1000)

        # A Popen success is insufficient: the WASAPI helper must send its
        # first actual transport packet after StartRecording succeeds.
        normal = Proc(HEADER.pack(0, 1000, 0))
        observed = []
        with patch.object(process_audio.sys, 'platform', 'win32'), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(process_audio.subprocess, 'Popen', return_value=normal) as popen, \
             patch.object(process_audio.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
            capture = new_capture(normal, observed)
            try:
                capture.start()
                self.assertTrue(any(h.healthy for h in observed))
                self.assertEqual(popen.call_args.args[0], ['dotnet', 'helper.dll', '--pid', '11'])
            finally:
                capture.close()

        no_packets = Proc(None)
        with patch.object(process_audio.sys, 'platform', 'win32'), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(process_audio.subprocess, 'Popen', return_value=no_packets), \
             patch.object(process_audio, 'CAPTURE_STARTUP_TIMEOUT_SECONDS', 0.03), \
             patch.object(process_audio.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
            capture = new_capture(no_packets, [])
            with self.assertRaisesRegex(TimeoutError, 'did not acknowledge'):
                capture.start()
            self.assertTrue(no_packets.terminated)

        eof = Proc(b'')
        with patch.object(process_audio.sys, 'platform', 'win32'), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(process_audio.subprocess, 'Popen', return_value=eof), \
             patch.object(process_audio.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
            capture = new_capture(eof, [])
            with self.assertRaisesRegex(OSError, 'failed to initialize'):
                capture.start()

    def test_early_helper_failure_is_delivered_to_first_real_utterance(self):
        reported = []
        audio = _native_audio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *fields: reported.append(fields),
                            recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=SimpleNamespace(reset=lambda: None), clock=lambda: 1000)
        try:
            audio.health(CaptureHealth(950, False, 'WASAPI stream closed'))
            self.assertEqual(reported, [])  # no dialogue token existed yet
            token = Token(1, 1)
            audio.begin(token, '台詞')
            self.assertEqual(reported, [(token, 'WASAPI stream closed')])
            self.assertEqual(audio.capture_status()['failure'], 'WASAPI stream closed')
        finally:
            audio.close()

    def test_transport_heartbeat_is_not_pcm_or_utterance_evidence(self):
        events = []
        audio = _native_audio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *args: events.append(args),
                            recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=SimpleNamespace(reset=lambda: None), clock=lambda: 1000)
        try:
            token = Token(1, 1)
            audio.begin(token, '台詞')
            audio.health(CaptureHealth(1000, True, 'helper_alive'))
            self.assertEqual(audio.capture_status()['heartbeats'], 1)
            self.assertEqual(audio.capture_status()['pcm_packets'], 0)
            self.assertEqual(events, [])
            audio.health(CaptureHealth(1010, True, 'helper_alive', pcm_frames=960))
            self.assertEqual(audio.capture_status()['pcm_packets'], 1)
            self.assertEqual(audio.capture_status()['pcm_frames'], 960)
            self.assertEqual(events, [])  # decoded voice never inferred from packet count
        finally:
            audio.close()


class PacketVadBoundaryTests(unittest.TestCase):
    def test_prior_line_packet_cannot_enter_new_line_history(self):
        history = AudioHistory()
        history.start(11)
        previous = PCMChunk(980, bytes(40 * 48 * 8), 48000, 2, 4)
        current = PCMChunk(1020, bytes(20 * 48 * 8), 48000, 2, 4)
        history.push(previous, target_pid=11)
        history.push(current, target_pid=11)
        self.assertEqual(history.since(1000), (current,))

    def test_active_utterance_retention_outlives_rolling_replay_window(self):
        history = AudioHistory(horizon_ms=1000)
        history.start(11)
        history.retain_from(1000)
        for when in (1000, 1500, 2000, 2500, 3000):
            history.push(PCMChunk(when, bytes(8), 48000, 2, 4), target_pid=11)
        self.assertEqual([chunk.at_ms for chunk in history.since(0)],
                         [1000, 1500, 2000, 2500, 3000])

        history.release_retention()
        self.assertEqual([chunk.at_ms for chunk in history.since(0)],
                         [2000, 2500, 3000])

    def _native_audio_keeps_full_active_line_in_every_decode_then_returns_to_rolling_history(self):
        decoded_windows = []

        class Recognizer:
            def recognize(self, chunks):
                decoded_windows.append((chunks[0].at_ms, chunks[-1].at_ms))
                return ASRResult('')

        class Quiet:
            def reset(self):
                pass
            def observe_float32_stereo(self, _frames):
                return VADDecision(False, 0, frames_analyzed=True)

        clock = [1000]
        native = _native_audio(
            11, Path('unused'), Path('unused'), Path('unused'), lambda *_: None,
            recognizer=Recognizer(), activity=Quiet(), clock=lambda: clock[0],
            replay_lookback_ms=0)
        # Make the distinction visible without waiting ten real seconds. The
        # retention contract is independent of the rolling horizon size.
        native.history.horizon_ms = 1000
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, '長い台詞')
            for when in (1000, 1500, 2000, 2500, 3000):
                clock[0] = when
                chunk = PCMChunk(when, bytes(8), 48000, 2, 4)
                native.history.push(chunk, target_pid=11)
                native.feed(chunk)
                end = time.monotonic() + 1
                while time.monotonic() < end:
                    with native._lock:
                        busy = native._inflight
                    if not busy:
                        break
                    time.sleep(.001)
                self.assertFalse(busy)
            self.assertTrue(decoded_windows)
            self.assertEqual(decoded_windows[-1], (1000, 3000))

        finally:
            native.close()

    def test_vad_input_amplitude_is_observation_not_speech_attribution(self):
        import numpy as np
        vad = SimpleNamespace(is_speech=lambda *_: False)
        with patch.dict(sys.modules, {'webrtcvad': SimpleNamespace(Vad=lambda _: vad)}):
            detector = VoiceActivity()
        stereo = np.full((960, 2), 0.25, dtype='<f4')
        result = detector.observe_float32_stereo(stereo.tobytes())
        self.assertTrue(result.frames_analyzed)
        self.assertFalse(result.active)
        self.assertAlmostEqual(result.mono_peak, 0.25, places=5)
        self.assertAlmostEqual(result.stereo_peak, 0.25, places=5)
        # Stereo antiphase would vanish in the VAD/ASR mono downmix; do
        # not label zero mono amplitude as proof of zero captured PCM.
        opposite = stereo.copy()
        opposite[:, 1] *= -1
        antiphase = detector.observe_float32_stereo(opposite.tobytes())
        self.assertEqual(antiphase.mono_peak, 0.0)
        self.assertAlmostEqual(antiphase.stereo_peak, 0.25, places=5)
        # Audio amplitude alone may be music or SFX: no ASR attribution or
        # completion may be inferred from this observation.
        audio = _native_audio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *_: None, recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=detector, clock=lambda: 1000)
        try:
            audio.begin(Token(1, 1), '台詞')
            audio.feed(PCMChunk(1000, stereo.tobytes(), 48000, 2, 4))
            status = audio.capture_status()
            self.assertAlmostEqual(status['vad_input_peak'], 0.25, places=5)
            self.assertEqual(status['vad_positive_packets'], 0)
        finally:
            audio.close()

    def test_packet_with_speech_then_silence_is_not_currently_playing(self):
        speech_by_frame = iter([True, True, False, False, False, False])
        # Fast CI does not install native VAD. Exercise the *production* frame
        # aggregation with deterministic VAD verdicts, not fake recognizer
        # output or an optional native dependency in every unit job.
        module = SimpleNamespace(Vad=lambda _: SimpleNamespace(
            is_speech=lambda *_: next(speech_by_frame)))
        with patch.dict(sys.modules, {'webrtcvad': module}):
            activity = VoiceActivity()
        # Six 20ms frames within one transport packet. The VAD previously
        # reported active=True for the entire packet when any frame had voice.
        packet = bytes(6 * 960 * 8)
        outcome = activity.observe_float32_stereo(packet)
        self.assertTrue(outcome.frames_analyzed)
        self.assertTrue(outcome.contains_speech)
        self.assertFalse(outcome.active)
        self.assertEqual(outcome.tail_silence_ms, 80)

    def test_trailing_quiet_after_speech_preserves_prior_voice_cue(self):
        outcomes = iter([VADDecision(False, 400, contains_speech=True)])
        activity = SimpleNamespace(observe_float32_stereo=lambda _: next(outcomes))
        audio = _native_audio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *_: None, activity=activity, recognizer=SimpleNamespace(recognize=lambda _: ''),
                            clock=lambda: 1000)
        try:
            token = Token(1, 1)
            audio.begin(token, 'テスト')
            audio.feed(PCMChunk(1000, bytes(960 * 8), 48000, 2, 4))
            self.assertEqual(audio.vad_onset_ms, 1000)
            self.assertEqual(audio.vad_tail_ms, 400)
        finally:
            audio.close()

    def test_external_hook_replay_uses_only_requested_pre_hook_window(self):
        activity = SimpleNamespace(
            observe_float32_stereo=lambda _: VADDecision(False, 0),
            reset=lambda: None)
        audio = _native_audio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *_: None, activity=activity,
                            recognizer=SimpleNamespace(recognize=lambda _: ''), clock=lambda: 2000)
        try:
            audio.history.start(11)
            for when in (1000, 1400, 1800):
                audio.history.push(PCMChunk(when, bytes(960 * 8), 48000, 2, 4), target_pid=11)
            token = Token(1, 2)
            audio.begin(token, '新しい台詞', replay_from_ms=1300)
            self.assertEqual(audio.window_start_ms, 1300)
            self.assertEqual(audio._last_processed_ms, 1800)
            self.assertEqual(audio.token, token)
        finally:
            audio.close()

    def test_delayed_old_pcm_cannot_mutate_new_line_vad(self):
        entered = threading.Event()
        release = threading.Event()
        times = [1000]
        observed = []

        class BlockingActivity:
            def observe_float32_stereo(self, frames):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test VAD blocked')
                return VADDecision(True, 0)

            def reset(self):
                pass

        audio = _native_audio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *args: observed.append(args), activity=BlockingActivity(),
                            recognizer=SimpleNamespace(recognize=lambda _: ''), clock=lambda: times[0])
        try:
            old, new = Token(1, 1), Token(1, 2)
            audio.begin(old, '古い台詞')
            old_chunk = PCMChunk(1000, bytes(960 * 8), 48000, 2, 4)
            t_feed = threading.Thread(target=audio.feed, args=(old_chunk,))
            t_feed.start()
            self.assertTrue(entered.wait(2))
            times[0] = 1100
            t_begin = threading.Thread(target=audio.begin, args=(new, '新しい台詞'))
            t_begin.start()
            release.set()
            t_feed.join(3)
            t_begin.join(3)
            self.assertFalse(t_feed.is_alive() or t_begin.is_alive())
            self.assertEqual(audio.token, new)
            self.assertEqual(audio.vad_onset_ms, -1)
            self.assertFalse(audio._vad_active)
            # The old PCM's original timestamp can never enter the new line.
            audio.feed(old_chunk)
            self.assertEqual(audio.vad_onset_ms, -1)
        finally:
            release.set()
            audio.close()

    def test_later_vad_rise_does_not_discard_earlier_utterance_audio(self):
        heard = []
        signal = threading.Event()
        activity = SimpleNamespace(observe_float32_stereo=lambda _: VADDecision(True, 0),
                                   reset=lambda: None)
        class Recognizer:
            def recognize(self, chunks):
                heard.append(tuple(chunk.at_ms for chunk in chunks))
                signal.set()
                return ''
        audio = _native_audio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *_: None, recognizer=Recognizer(), activity=activity,
                            clock=lambda: 1000)
        try:
            audio.history.start(11)
            audio.begin(Token(1, 1), '台詞')
            for at_ms in (900, 1100, 1390, 1500):
                audio.history.push(PCMChunk(at_ms, bytes(960 * 8), 48000, 2, 4), target_pid=11)
            audio.feed(PCMChunk(1600, bytes(960 * 8), 48000, 2, 4))
            self.assertTrue(signal.wait(2))
            # The Utterance began at 1000ms. A later VAD rise at 1600ms is only
            # acoustic evidence inside that line and must not redefine the ASR
            # window or throw away the earlier 1100/1390ms speech.
            self.assertEqual(heard[0], (1100, 1390, 1500))
            self.assertEqual(audio._sample_start(), audio.window_start_ms)
        finally:
            audio.close()

    def test_vad_reactivation_keeps_first_utterance_rise(self):
        clock = [1000]
        decisions = iter((
            VADDecision(True, 0, contains_speech=True),
            VADDecision(False, 300, contains_speech=False),
            VADDecision(True, 0, contains_speech=True),
        ))
        audio = _native_audio(
            11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
            lambda *_: None,
            recognizer=SimpleNamespace(recognize=lambda _: ''),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _: next(decisions), reset=lambda: None),
            clock=lambda: clock[0])
        try:
            audio.history.start(11)
            audio.begin(Token(1, 1), '長い台詞')
            for when in (1100, 1500, 1900):
                clock[0] = when
                packet = PCMChunk(when, bytes(960 * 8), 48000, 2, 4)
                audio.history.push(packet, target_pid=11)
                audio.feed(packet)
                for _ in range(200):
                    with audio._lock:
                        busy = audio._inflight
                    if not busy:
                        break
                    time.sleep(.005)
            self.assertEqual(audio.vad_onset_ms, 1100)
            self.assertEqual(audio._sample_start(), audio.window_start_ms)
        finally:
            audio.close()

class NativeKnowledgeArchitectureTests(unittest.TestCase):
    def test_token_timing_observations_are_audio_timeline_measurements(self):
        samples = (
            PCMChunk(1000, bytes(48000 // 10 * 8), 48000, 2, 4),
            PCMChunk(1600, bytes(48000 // 10 * 8), 48000, 2, 4),
        )
        result = ASRResult('AB', ('A', 'B'), (0.05, 0.15))
        last, successor, end_ms = NativeAudio._token_timing_observations(samples, result, 1)
        self.assertEqual(last, 950)
        self.assertEqual(successor, 1550)
        self.assertIsNone(end_ms)

    def test_token_duration_is_distinct_from_successor_start(self):
        samples = (PCMChunk(2000, bytes(48000 * 8), 48000, 2, 4),)
        result = ASRResult('AB', ('A', 'B'), (0.20, 0.70), (0.30, 0.10))
        last, successor, end_ms = NativeAudio._token_timing_observations(samples, result, 1)
        self.assertEqual(last, 1200)
        self.assertEqual(successor, 1700)
        self.assertEqual(end_ms, 1500)

    def test_heartbeat_is_transport_observation_only(self):
        observed = []
        native = _native_audio(
            11, Path('unused'), Path('unused'), Path('unused'), lambda *_: None,
            recognizer=SimpleNamespace(recognize=lambda _: ''), g2p=TEST_G2P,
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _: VADDecision(False, 0), reset=lambda: None),
            clock=lambda: 1000,
            on_observation=lambda token, item: observed.append((token, item)))
        try:
            token = Token(1, 1)
            native.token = token
            native._last_pcm_end_ms = 1000
            native.health(CaptureHealth(1100, True, 'helper_alive'))
            self.assertEqual(len(observed), 1)
            item = observed[0][1]
            self.assertEqual(item.kind, 'transport')
            self.assertEqual(item.pcm_last_frame_ms, 1000)
            self.assertEqual(item.heartbeat_at_ms, 1100)
            knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), item)
            self.assertFalse(knowledge.target_associated)
            self.assertFalse(knowledge.target_end_confirmed)
        finally:
            native.close()

    def test_native_audio_reports_observations_not_completion_state(self):
        observed = []
        metrics = []
        ready = threading.Event()

        class Recognizer:
            def recognize(self, _chunks):
                return ASRResult('おはようございます背景', tuple('おはようございます背景'),
                                 tuple(i * .1 for i in range(len('おはようございます背景'))))

        native = _native_audio(
            11, Path('unused'), Path('unused'), Path('unused'), lambda *_: None,
            recognizer=Recognizer(), g2p=TEST_G2P,
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _: VADDecision(False, 0), reset=lambda: None),
            clock=lambda: 2000,
            on_inference=lambda _t, m: metrics.append(m),
            on_observation=lambda t, item: (observed.append((t, item)), ready.set()))
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, 'おはようございます')
            chunk = PCMChunk(2400, bytes(48000 * 2 * 8), 48000, 2, 4)
            native.history.push(chunk, target_pid=11)
            native.feed(chunk)
            self.assertTrue(ready.wait(2))
            item = observed[-1][1]
            self.assertEqual(item.kind, 'asr')
            self.assertIsNotNone(item.target_successor_token_start_ms)
            self.assertIsNone(item.target_acoustic_end_ms)
            knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), item)
            self.assertTrue(knowledge.target_associated)
            self.assertFalse(knowledge.target_end_confirmed)
            self.assertFalse(hasattr(native, 'completed'))
            self.assertIn('matched_phonemes', metrics[-1])
            self.assertIn('target_successor_token_start_ms', metrics[-1])
            self.assertNotIn('target_progressed', metrics[-1])
            self.assertNotIn('voice_state', metrics[-1])
            self.assertNotIn('evidence_score', metrics[-1])
        finally:
            native.close()

    def test_duration_backed_tail_observation_can_form_end_knowledge(self):
        matcher = TargetMatcher('AB', g2p=TEST_G2P)
        alignment = matcher.align('AB', tokens=('A', 'B'))
        item = NativeAudioObservation(
            kind='asr', alignment=alignment,
            target_last_token_start_ms=1200,
            target_acoustic_end_ms=1500,
            target_acoustic_end_source='asr_token_duration')
        knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), item)
        self.assertTrue(knowledge.target_associated)
        self.assertTrue(knowledge.target_end_confirmed)
        self.assertEqual(knowledge.end.estimated_end_ms, 1500)

class AdvanceOriginAudioWindowTests(unittest.TestCase):
    """Real NativeAudio state ownership with controlled PCM/VAD/ASR timestamps."""

    def test_voice_ending_before_hook_is_replayed_without_fabricating_endpoint(self):
        moments = [1000]
        activity = iter((VADDecision(True, 0), VADDecision(False, 200),
                         VADDecision(False, 400)))
        recognition = []
        observations = []
        ready = threading.Event()

        class Recognizer:
            def recognize(self, chunks):
                recognition.append(tuple(c.at_ms for c in chunks))
                return '合図です'

        def report(token, item):
            observations.append((token, item))
            if item.kind == 'asr':
                ready.set()

        audio = _native_audio(11, Path('unused'), Path('unused'), Path('unused'),
                            lambda *_: None, recognizer=Recognizer(),
                            activity=SimpleNamespace(observe_float32_stereo=lambda _: next(activity),
                                                     reset=lambda: None),
                            clock=lambda: moments[0], on_observation=report)
        try:
            audio.history.start(11)
            audio.boundary()  # prior advance input, long before Luna's flush
            for when in (1100, 1300, 1500):
                packet = PCMChunk(when, bytes(960 * 8), 48000, 2, 4)
                audio.history.push(packet, target_pid=11)
                audio.feed(packet)
            self.assertEqual(observations, [])
            moments[0] = 1800
            token = Token(5, 2)
            audio.begin(token, '合図です')
            self.assertTrue(ready.wait(2), observations)
            self.assertEqual(recognition, [(1100, 1300, 1500)])
            knowledge = NativeVoiceKnowledge()
            for observed_token, item in observations:
                self.assertEqual(observed_token, token)
                knowledge = reduce_native_voice_knowledge(knowledge, item)
            self.assertTrue(knowledge.target_associated)
            self.assertFalse(knowledge.target_end_confirmed)
            self.assertLess(audio.vad_onset_ms, audio.start_ms)
        finally:
            audio.close()

    def test_previous_interval_audio_is_not_replayed_after_next_send(self):
        times = [1000]
        viewed = []
        audio = _native_audio(11, Path('unused'), Path('unused'), Path('unused'),
                            lambda *_: None, recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=SimpleNamespace(observe_float32_stereo=lambda _: (
                                viewed.append('frame') or VADDecision(True, 0)), reset=lambda: None),
                            clock=lambda: times[0])
        try:
            audio.history.start(11)
            old = PCMChunk(900, bytes(960 * 8), 48000, 2, 4)
            audio.history.push(old, target_pid=11)
            audio.boundary()
            current = PCMChunk(1100, bytes(960 * 8), 48000, 2, 4)
            audio.history.push(current, target_pid=11)
            times[0] = 1600
            audio.begin(Token(2, 1), 'テスト')
            self.assertEqual(viewed, ['frame'])
            audio.feed(current)  # previously captured packet, no double VAD
            audio.feed(old)  # old packet, cannot change new token
            self.assertEqual(viewed, ['frame'])
            self.assertEqual(audio.vad_onset_ms, 1100)
        finally:
            audio.close()

    def test_no_send_does_not_claim_pre_hook_voice_from_prior_line(self):
        times = [1600]
        audio = _native_audio(11, Path('unused'), Path('unused'), Path('unused'),
                            lambda *_: None, recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=SimpleNamespace(observe_float32_stereo=lambda _: VADDecision(True, 0),
                                                     reset=lambda: None),
                            clock=lambda: times[0])
        try:
            audio.history.start(11)
            audio.history.push(PCMChunk(1200, bytes(960 * 8), 48000, 2, 4), target_pid=11)
            audio.begin(Token(2, 1), '新しい本文')  # no observed advance
            self.assertEqual(audio.vad_onset_ms, -1)
        finally:
            audio.close()
