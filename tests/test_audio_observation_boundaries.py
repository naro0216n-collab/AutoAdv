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
from autoadv.audio_match import ASRProgress, VoiceActivity, VADDecision
from autoadv.conversation import Controller, Target, Token
from autoadv.model import AdvanceMode, HookSelection, Profile
from autoadv.native_audio import ASRResult, NativeAudio
from autoadv.process_audio import CaptureHealth, ProcessCapture, HEADER


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
             patch.object(process_audio.subprocess, 'Popen', return_value=normal), \
             patch.object(process_audio.subprocess, 'CREATE_NO_WINDOW', 0, create=True):
            capture = new_capture(normal, observed)
            try:
                capture.start()
                self.assertTrue(any(h.healthy for h in observed))
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
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *fields: reported.append(fields),
                            recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=SimpleNamespace(reset=lambda: None), clock=lambda: 1000)
        try:
            audio.health(CaptureHealth(950, False, 'WASAPI stream closed'))
            self.assertEqual(reported, [])  # no dialogue token existed yet
            token = Token(1, 1)
            audio.begin(token, '台詞')
            self.assertEqual(reported, [(token, 'error', 'WASAPI stream closed')])
            self.assertEqual(audio.capture_status()['failure'], 'WASAPI stream closed')
        finally:
            audio.close()

    def test_transport_heartbeat_is_not_pcm_or_utterance_evidence(self):
        events = []
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *args: events.append(args),
                            recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=SimpleNamespace(reset=lambda: None), clock=lambda: 1000)
        try:
            token = Token(1, 1)
            audio.begin(token, '台詞')
            audio.health(CaptureHealth(1000, True, 'helper_alive'))
            self.assertEqual(audio.capture_status()['heartbeats'], 1)
            self.assertEqual(audio.capture_status()['pcm_packets'], 0)
            self.assertFalse(audio.is_confirmed_playing(token))
            self.assertFalse(audio.completed)
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
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *_: None, recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=detector, clock=lambda: 1000)
        try:
            audio.begin(Token(1, 1), '台詞')
            audio.feed(PCMChunk(1000, stereo.tobytes(), 48000, 2, 4))
            status = audio.capture_status()
            self.assertAlmostEqual(status['vad_input_peak'], 0.25, places=5)
            self.assertEqual(status['vad_positive_packets'], 0)
            self.assertFalse(audio.completed)
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
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *_: None, activity=activity, recognizer=SimpleNamespace(recognize=lambda _: ''),
                            clock=lambda: 1000)
        try:
            token = Token(1, 1)
            audio.begin(token, 'テスト')
            audio.feed(PCMChunk(1000, bytes(960 * 8), 48000, 2, 4))
            self.assertEqual(audio.rise_ms, 1000)
            self.assertFalse(audio.is_confirmed_playing(token))
        finally:
            audio.close()

    def test_external_hook_replay_uses_only_requested_pre_hook_window(self):
        activity = SimpleNamespace(
            observe_float32_stereo=lambda _: VADDecision(False, 0),
            reset=lambda: None)
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
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

        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
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
            self.assertEqual(audio.rise_ms, -1)
            self.assertFalse(audio._last_vad)
            # The old PCM's original timestamp can never enter the new line.
            audio.feed(old_chunk)
            self.assertEqual(audio.rise_ms, -1)
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
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
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
        audio = NativeAudio(
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
            self.assertEqual(audio.rise_ms, 1100)
            self.assertEqual(audio._sample_start(), audio.window_start_ms)
        finally:
            audio.close()

    def test_corrected_native_match_reports_completion_to_existing_controller_once(self):
        sent = []
        ctrl = Controller(lambda target, key: sent.append((target.pid, key)), lambda _: True)
        profile = Profile('p', 'P', advance_mode=AdvanceMode.AUDIO,
                          hooks=[HookSelection('X', 1, 2)], body_source='X|1|2')
        ctrl.begin(profile, Target(11, 2), 'host', 1)
        source = SimpleNamespace(hook_code='X', context=1, context2=2)
        token = ctrl.observe(generation='host', connection=1, sequence=1,
                             source=source, text='えっ嘘でしょ', pid=11)
        self.assertIsNotNone(token)
        ctrl.permit(True)
        events = []
        readings = iter(['え嘘でしょ', 'え嘘でしょ'])
        vad_frames = iter([VADDecision(True, 0), VADDecision(False, 400)])
        clock = [1000]

        def notify(observed, event, detail):
            events.append((observed, event))
            if event == 'completed':
                if ctrl.presentation_event(observed, 'native_finished'):
                    ctrl.try_advance()

        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            notify, recognizer=SimpleNamespace(recognize=lambda _: next(readings)),
                            activity=SimpleNamespace(observe_float32_stereo=lambda _: next(vad_frames),
                                                     reset=lambda: None), clock=lambda: clock[0])
        try:
            audio.history.start(11)
            audio.begin(token, 'えっ嘘でしょ')
            for when in (1000, 1600):
                clock[0] = when
                chunk = PCMChunk(when, bytes(960 * 8), 48000, 2, 4)
                audio.history.push(chunk, target_pid=11)
                audio.feed(chunk)
                for _ in range(200):
                    with audio._lock:
                        busy = audio._inflight
                    if not busy:
                        break
                    time.sleep(.005)
                self.assertFalse(busy, 'product ASR observation did not finish')
            self.assertEqual(ctrl.presentation_reason, 'native')
            self.assertEqual(len(sent), 1)
            self.assertEqual(sum(event == 'completed' for _, event in events), 1)
            self.assertFalse(ctrl.presentation_event(token, 'native_finished'))
            self.assertFalse(ctrl.try_advance())
            self.assertEqual(len(sent), 1)
        finally:
            audio.close()


class InferenceOwnershipTests(unittest.TestCase):
    def test_late_decode_from_old_line_cannot_release_new_line_inference_slot(self):
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()

        class Recognizer:
            def __init__(self):
                self.calls = 0

            def recognize(self, chunks):
                self.calls += 1
                if self.calls == 1:
                    first_started.set()
                    if not release_first.wait(3):
                        raise TimeoutError('first inference not released')
                else:
                    second_started.set()
                    if not release_second.wait(3):
                        raise TimeoutError('second inference not released')
                return 'テスト'

        activity = SimpleNamespace(observe_float32_stereo=lambda _: VADDecision(True, 0))
        reports = []
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
                            lambda *args: reports.append(args), recognizer=Recognizer(),
                            activity=activity, clock=lambda: 1000)
        try:
            audio.history.start(11)
            old, new = Token(1, 1), Token(1, 2)
            audio.begin(old, 'テスト')
            first = PCMChunk(1000, bytes(960 * 8), 48000, 2, 4)
            audio.history.push(first, target_pid=11)
            audio.feed(first)
            self.assertTrue(first_started.wait(2))
            audio.begin(new, 'テスト')
            second = PCMChunk(1700, bytes(960 * 8), 48000, 2, 4)
            audio.history.push(second, target_pid=11)
            audio.feed(second)
            with audio._lock:
                expected = audio._generation
                self.assertEqual(audio._inflight_generation, expected)
            release_first.set()
            self.assertTrue(second_started.wait(2))
            with audio._lock:
                self.assertEqual(audio._inflight_generation, expected)
                self.assertTrue(audio._inflight)
            release_second.set()
            for _ in range(100):
                with audio._lock:
                    if not audio._inflight:
                        break
                time.sleep(.01)
            self.assertFalse(audio._inflight)
            self.assertFalse(any(token == old for token, *_ in reports))
        finally:
            release_first.set()
            release_second.set()
            audio.close()


class RealControllerAudioDeadlineTests(unittest.TestCase):
    def test_pending_native_analysis_defers_fallback_but_unrelated_speech_cannot_hold_forever(self):
        from threading import Event
        entered, release = Event(), Event()
        now = [1000]
        sent = []
        class SlowUnrelatedASR:
            def recognize(self, chunks):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('deliberately blocked recognition')
                return '別の話題です'
        class ActiveVoice:
            def observe_float32_stereo(self, frames):
                return VADDecision(True, 0, contains_speech=True)
            def reset(self):
                pass
        profile = Profile('p', 'P', advance_mode=AdvanceMode.AUDIO,
                          uncertain_text_fallback=True,
                          hooks=[HookSelection('X', 1, 2)], body_source='X|1|2')
        controller = Controller(lambda *_: sent.append('input'), lambda *_: True)
        controller.begin(profile, Target(11, 22), 'host', 1)
        controller.permit(True)
        source = SimpleNamespace(hook_code='X', context=1, context2=2)
        token = controller.observe(generation='host', connection=1,
            sequence=1, source=source, text='えっ嘘でしょ。', pid=11)
        self.assertIsNotNone(token)
        def notice(t, event, detail):
            if event == 'completed':
                controller.presentation_event(t, 'native_finished')
        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            notice, recognizer=SlowUnrelatedASR(), activity=ActiveVoice(),
            clock=lambda: now[0])
        try:
            native.history.start(11)
            native.boundary()
            native.begin(token, 'えっ嘘でしょ。')
            chunk = PCMChunk(1000, bytes(960 * 8), 48000, 2, 4)
            native.history.push(chunk, target_pid=11)
            native.feed(chunk)
            self.assertTrue(entered.wait(1))
            # Product audio says hold; production Controller has neither
            # finished nor authorized a send at the fallback deadline.
            now[0] = 1700
            self.assertTrue(native.holds_deadline(token))
            self.assertFalse(controller.completed)
            self.assertFalse(controller.try_advance())
            self.assertEqual(sent, [])
            # After the bounded grace period, the product still labels an
            # optional send as uncertain, NEVER as native completion.
            now[0] = 3101
            self.assertFalse(native.holds_deadline(token))
            controller.presentation_event(token, 'fallback_elapsed')
            self.assertEqual(controller.presentation_reason, 'uncertain_text_fallback')
            self.assertTrue(controller.try_advance())
            self.assertEqual(sent, ['input'])
        finally:
            native.cancel()
            release.set()
            native.close()
        self.assertEqual(sent, ['input'])

    def test_pending_real_pcm_asr_gets_bounded_fallback_grace_without_vad(self):
        entered = threading.Event()
        release = threading.Event()
        now = [1000]

        class SlowRecognizer:
            def recognize(self, chunks):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('test ASR was not released')
                return ASRResult('別の話題')

        class NeverActive:
            def observe_float32_stereo(self, frames):
                return VADDecision(False, 0, contains_speech=False)
            def reset(self):
                pass

        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            lambda *_: None, recognizer=SlowRecognizer(), activity=NeverActive(),
            clock=lambda: now[0])
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, 'えっ嘘でしょ')
            now[0] = 1400
            pcm = PCMChunk(1400, bytes(960 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            self.assertTrue(entered.wait(1))
            now[0] = 1700
            self.assertTrue(native.holds_deadline(token))
            now[0] = 3401
            self.assertFalse(native.holds_deadline(token))
        finally:
            release.set()
            native.close()

    def test_partial_asr_can_complete_with_independent_vad_endpoint_evidence(self):
        events = []
        metrics = []
        clock = [1000]
        decisions = iter((
            VADDecision(True, 0, contains_speech=True),
            VADDecision(False, 400, contains_speech=False),
        ))

        class Recognizer:
            def recognize(self, chunks):
                # Four of six normalized target characters are recognized.
                # This is intentionally not a matcher terminal candidate.
                return ASRResult('えっ嘘で')

        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event), recognizer=Recognizer(),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _frames: next(decisions),
                reset=lambda: None),
            clock=lambda: clock[0], on_inference=lambda _t, m: metrics.append(m))
        try:
            native.history.start(11)
            native.begin(Token(1, 1), 'えっ嘘でしょ')
            for when in (1100, 1600):
                clock[0] = when
                pcm = PCMChunk(when, bytes(4800 * 8), 48000, 2, 4)
                native.history.push(pcm, target_pid=11)
                native.feed(pcm)
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    with native._lock:
                        busy = native._inflight
                    if not busy:
                        break
                    time.sleep(.005)
                self.assertFalse(busy, 'partial ASR inference did not finish')
            self.assertIn('started', events)
            self.assertIn('completed', events)
            self.assertTrue(native.completed)
            self.assertFalse(metrics[-1]['terminal_candidate'])
            self.assertAlmostEqual(metrics[-1]['phonetic_coverage'], 4 / 6, places=3)
            self.assertLess(metrics[-1]['length_score'], .85)
            self.assertGreaterEqual(metrics[-1]['endpoint_score'], .70)
            self.assertGreaterEqual(
                metrics[-1]['evidence_score'], NativeAudio.COMPLETION_EVIDENCE_THRESHOLD)
        finally:
            native.close()

    def test_current_combined_evidence_completes_without_prior_attribution_latch(self):
        events = []
        metrics = []
        clock = [1000]
        decisions = iter((
            VADDecision(True, 0, contains_speech=True),
            VADDecision(False, 400, contains_speech=False),
        ))

        progress = ASRProgress(
            coverage=.6667, onset_score=.75, tail_confidence=0.0, monotonic=True,
            matched_chars=4, observed='え嘘でし', edit_count=2, length_ratio=.6667,
            terminal_candidate=False, phonetic_coverage=.7778, matched_phonemes=7,
            phonetic_edit_count=2, attribution_score=.7529, completion_score=.72,
            contradiction_score=.1444, reading_rank=0, target_units=9,
            pronunciation_length_ratio=.7778, observed_token_count=4)

        native = NativeAudio(
            11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event),
            recognizer=SimpleNamespace(recognize=lambda _: ASRResult('え嘘でし')),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _frames: next(decisions),
                reset=lambda: None),
            clock=lambda: clock[0], on_inference=lambda _t, m: metrics.append(m))
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, 'えっ嘘でしょ')
            native.matcher = SimpleNamespace(observe=lambda *_args, **_kwargs: progress)
            for when in (1100, 1600):
                clock[0] = when
                pcm = PCMChunk(when, bytes(4800 * 8), 48000, 2, 4)
                native.history.push(pcm, target_pid=11)
                native.feed(pcm)
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    with native._lock:
                        busy = native._inflight
                    if not busy:
                        break
                    time.sleep(.005)
                self.assertFalse(busy, 'combined-evidence inference did not finish')
            self.assertIn('started', events)
            self.assertIn('completed', events)
            self.assertTrue(native.completed)
            self.assertAlmostEqual(metrics[-1]['attribution_score'], .7529, places=4)
            self.assertAlmostEqual(metrics[-1]['length_score'], .7778, places=4)
            self.assertLess(metrics[-1]['length_score'], .85)
            self.assertGreaterEqual(metrics[-1]['vad_tail_ms'], 400)
            self.assertGreaterEqual(metrics[-1]['endpoint_score'], .45)
            self.assertGreaterEqual(
                metrics[-1]['evidence_score'], NativeAudio.COMPLETION_EVIDENCE_THRESHOLD)
        finally:
            native.close()


    def test_whisper_like_partial_alignment_completes_with_strong_vad_endpoint(self):
        events = []
        metrics = []
        clock = [1000]
        progress = ASRProgress(
            coverage=.3636, onset_score=.61, tail_confidence=0.0, monotonic=True,
            matched_chars=4, observed='whisper-like', edit_count=7, length_ratio=.9091,
            terminal_candidate=False, phonetic_coverage=.6667, matched_phonemes=12,
            phonetic_edit_count=6, attribution_score=.6130, completion_score=.5691,
            contradiction_score=.2167, reading_rank=0, target_units=18,
            pronunciation_length_ratio=.9091, observed_token_count=10)

        native = NativeAudio(
            11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event),
            recognizer=SimpleNamespace(recognize=lambda _: ASRResult('whisper-like')),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _frames: VADDecision(
                    False, 300, contains_speech=False),
                reset=lambda: None),
            clock=lambda: clock[0], on_inference=lambda _t, m: metrics.append(m))
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, '囁き声の対象台詞')
            native.matcher = SimpleNamespace(observe=lambda *_args, **_kwargs: progress)
            clock[0] = 1500
            pcm = PCMChunk(1500, bytes(24000 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            end = time.monotonic() + 2
            while time.monotonic() < end:
                with native._lock:
                    busy = native._inflight
                if not busy:
                    break
                time.sleep(.005)
            self.assertFalse(busy, 'whisper-like inference did not finish')
            self.assertIn('completed', events)
            self.assertTrue(native.completed)
            self.assertFalse(metrics[-1]['terminal_candidate'])
            self.assertGreaterEqual(metrics[-1]['length_score'], .85)
            self.assertGreaterEqual(metrics[-1]['endpoint_score'], .55)
            self.assertGreaterEqual(
                metrics[-1]['evidence_score'], NativeAudio.COMPLETION_EVIDENCE_THRESHOLD)
        finally:
            native.close()

    def test_stable_terminal_asr_combines_with_short_timestamp_tail_under_bgm(self):
        events = []
        metrics = []
        end_observations = []
        clock = [1000]

        class Recognizer:
            def recognize(self, chunks):
                total_ms = sum(chunk.duration_ms for chunk in chunks)
                next_token = max(.42, total_ms / 1000.0 - .08)
                return ASRResult(
                    'えっ嘘でしょ背', tuple('えっ嘘でしょ背'),
                    (0.05, 0.12, 0.20, 0.28, 0.34, next_token - .07, next_token))

        native = NativeAudio(
            11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event), recognizer=Recognizer(),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _frames: VADDecision(
                    True, 0, contains_speech=True),
                reset=lambda: None),
            clock=lambda: clock[0], on_inference=lambda _t, m: metrics.append(m),
            on_voice_end=lambda _t, m: end_observations.append(m))
        try:
            native.history.start(11)
            native.begin(Token(1, 1), 'えっ嘘でしょ')
            for when in (1500, 2000):
                clock[0] = when
                pcm = PCMChunk(when, bytes(24000 * 8), 48000, 2, 4)
                native.history.push(pcm, target_pid=11)
                native.feed(pcm)
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    with native._lock:
                        busy = native._inflight
                    if not busy:
                        break
                    time.sleep(.005)
                self.assertFalse(busy, 'stable terminal inference did not finish')
            self.assertEqual(len(metrics), 2)
            self.assertLess(metrics[0]['endpoint_score'], NativeAudio.ENDPOINT_EVIDENCE_THRESHOLD)
            self.assertEqual(metrics[0]['tail_confidence'], 0.0)
            self.assertEqual(metrics[1]['tail_confidence'], 1.0)
            self.assertAlmostEqual(metrics[1]['timestamp_endpoint_score'], .25, places=2)
            self.assertGreaterEqual(
                metrics[1]['endpoint_score'], NativeAudio.ENDPOINT_EVIDENCE_THRESHOLD)
            self.assertIn('completed', events)
            self.assertTrue(native.completed)
            self.assertEqual(end_observations[-1]['source'], 'asr_timestamp')
        finally:
            native.close()

    def test_timestamp_endpoint_can_complete_with_post_target_pcm_while_vad_stays_active(self):
        events = []
        metrics = []
        end_observations = []
        clock = [1000]

        class Recognizer:
            def recognize(self, chunks):
                # The selected target prefix is followed by process-mix speech.
                # The following token bounds the target prefix in the decode
                # waveform; 300ms of captured PCM follows that boundary.
                return ASRResult(
                    'えっ嘘でしょ背景', tuple('えっ嘘でしょ背景'),
                    (0.05, 0.12, 0.20, 0.30, 0.38, 0.46, 0.70, 0.82))

        class AlwaysActive:
            def observe_float32_stereo(self, frames):
                return VADDecision(True, 0, contains_speech=True)
            def reset(self):
                pass

        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event), recognizer=Recognizer(),
            activity=AlwaysActive(), clock=lambda: clock[0],
            on_inference=lambda _t, m: metrics.append(m),
            on_voice_end=lambda _t, m: end_observations.append(m))
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, 'えっ嘘でしょ')
            clock[0] = 2000
            pcm = PCMChunk(2000, bytes(48000 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            end = time.monotonic() + 2
            while time.monotonic() < end:
                with native._lock:
                    busy = native._inflight
                if not busy:
                    break
                time.sleep(.005)
            self.assertIn('started', events)
            self.assertIn('completed', events)
            self.assertTrue(native.completed)
            self.assertEqual(metrics[-1]['vad_tail_ms'], 0.0)
            self.assertGreaterEqual(metrics[-1]['timestamp_endpoint_score'], .45)
            self.assertEqual(metrics[-1]['target_token_count'], 6)
            self.assertEqual(end_observations[-1]['source'], 'asr_timestamp')
        finally:
            native.close()

    def test_short_post_target_timestamp_tail_does_not_complete_while_vad_stays_active(self):
        events = []
        metrics = []
        clock = [1000]

        class Recognizer:
            def recognize(self, chunks):
                # Only 80ms of captured PCM follows the next-token boundary.
                # This reproduces the premature first-terminal inference seen in
                # RealAudio without forbidding timestamp evidence altogether.
                return ASRResult(
                    'えっ嘘でしょ背', tuple('えっ嘘でしょ背'),
                    (0.05, 0.12, 0.20, 0.30, 0.38, 0.46, 0.92))

        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event), recognizer=Recognizer(),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _frames: VADDecision(
                    True, 0, contains_speech=True),
                reset=lambda: None),
            clock=lambda: clock[0], on_inference=lambda _t, m: metrics.append(m))
        try:
            native.history.start(11)
            native.begin(Token(1, 1), 'えっ嘘でしょ')
            clock[0] = 2000
            pcm = PCMChunk(2000, bytes(48000 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            end = time.monotonic() + 2
            while time.monotonic() < end:
                with native._lock:
                    busy = native._inflight
                if not busy:
                    break
                time.sleep(.005)
            self.assertFalse(busy, 'short-tail timestamp inference did not finish')
            self.assertIn('started', events)
            self.assertNotIn('completed', events)
            self.assertFalse(native.completed)
            self.assertAlmostEqual(metrics[-1]['trailing_after_target_ms'], 80.0, places=3)
            self.assertLess(metrics[-1]['timestamp_endpoint_score'], .45)
        finally:
            native.close()

    def test_partial_match_token_timestamps_do_not_turn_unrecognized_suffix_into_trailing_audio(self):
        events = []
        metrics = []
        clock = [1000]
        progress = ASRProgress(
            coverage=.6957, onset_score=.78, tail_confidence=0.0, monotonic=True,
            matched_chars=8, observed='途中まで', edit_count=2, length_ratio=.70,
            terminal_candidate=False, phonetic_coverage=.7442, matched_phonemes=8,
            phonetic_edit_count=2, attribution_score=.80, completion_score=.7403,
            contradiction_score=.10, reading_rank=0, target_units=11,
            pronunciation_length_ratio=.74, observed_token_count=4)

        native = NativeAudio(
            11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event),
            recognizer=SimpleNamespace(recognize=lambda _: ASRResult(
                '途中まで背景', ('途', '中', 'ま', 'で', '背', '景'),
                (0.05, 0.15, 0.25, 0.35, 0.55, 0.70))),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _frames: VADDecision(
                    True, 0, contains_speech=True),
                reset=lambda: None),
            clock=lambda: clock[0], on_inference=lambda _t, m: metrics.append(m))
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, '途中まで続く対象台詞')
            native.matcher = SimpleNamespace(observe=lambda *_args, **_kwargs: progress)
            clock[0] = 2000
            pcm = PCMChunk(2000, bytes(48000 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            end = time.monotonic() + 2
            while time.monotonic() < end:
                with native._lock:
                    busy = native._inflight
                if not busy:
                    break
                time.sleep(.005)
            self.assertFalse(busy, 'partial timestamp inference did not finish')
            self.assertIn('started', events)
            self.assertNotIn('completed', events)
            self.assertFalse(native.completed)
            self.assertEqual(metrics[-1]['endpoint_score'], 0.0)
            self.assertEqual(metrics[-1]['vad_tail_ms'], 0.0)
        finally:
            native.close()

    def test_final_token_start_without_following_token_is_not_endpoint(self):
        events = []
        metrics = []
        end_observations = []
        clock = [1000]

        class Recognizer:
            def recognize(self, chunks):
                return ASRResult(
                    'えっ嘘でしょ', tuple('えっ嘘でしょ'),
                    (0.05, 0.12, 0.20, 0.30, 0.38, 0.46))

        class NeverActive:
            def observe_float32_stereo(self, frames):
                return VADDecision(False, 0, contains_speech=False)
            def reset(self):
                pass

        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event), recognizer=Recognizer(),
            activity=NeverActive(), clock=lambda: clock[0],
            on_inference=lambda _t, m: metrics.append(m),
            on_voice_end=lambda _t, m: end_observations.append(m))
        try:
            native.history.start(11)
            native.begin(Token(1, 1), 'えっ嘘でしょ')
            # ASR still runs without VAD-positive frames and can attribute
            # the line, but token starts alone cannot certify acoustic end.
            clock[0] = 2000
            pcm = PCMChunk(2000, bytes(48000 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            end = time.monotonic() + 2
            while time.monotonic() < end:
                with native._lock:
                    busy = native._inflight
                if not busy:
                    break
                time.sleep(.005)
            self.assertIn('started', events)
            self.assertNotIn('completed', events)
            self.assertFalse(native.completed)
            self.assertEqual(native.capture_status()['vad_positive_packets'], 0)
            self.assertEqual(metrics[-1]['vad_tail_ms'], 0.0)
            self.assertEqual(metrics[-1]['endpoint_score'], 0.0)
            self.assertEqual(end_observations, [])
        finally:
            native.close()

    def test_vad_tail_not_token_timestamp_establishes_acoustic_endpoint(self):
        events = []
        metrics = []
        end_observations = []
        clock = [1000]

        class Recognizer:
            def recognize(self, chunks):
                return ASRResult(
                    'えっ嘘でしょ', tuple('えっ嘘でしょ'),
                    (0.05, 0.12, 0.20, 0.30, 0.38, 0.46))

        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event), recognizer=Recognizer(),
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _frames: VADDecision(
                    False, 400, contains_speech=False),
                reset=lambda: None),
            clock=lambda: clock[0], on_inference=lambda _t, m: metrics.append(m),
            on_voice_end=lambda _t, m: end_observations.append(m))
        try:
            native.history.start(11)
            native.rise_ms = 1000
            native.begin(Token(1, 1), 'えっ嘘でしょ')
            native.rise_ms = 1000
            native.voice_end_estimate_ms = 1200
            clock[0] = 1600
            pcm = PCMChunk(1600, bytes(4800 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            end = time.monotonic() + 2
            while time.monotonic() < end:
                with native._lock:
                    busy = native._inflight
                if not busy:
                    break
                time.sleep(.005)
            self.assertFalse(busy, 'VAD endpoint inference did not finish')
            self.assertIn('completed', events)
            self.assertTrue(native.completed)
            self.assertEqual(metrics[-1]['vad_tail_ms'], 400.0)
            self.assertEqual(end_observations[-1]['source'], 'asr_vad')
        finally:
            native.close()

    def test_asr_timestamp_uses_pcm_duration_not_wall_clock_gap(self):
        first = PCMChunk(1000, bytes(4800 * 8), 48000, 2, 4)
        second = PCMChunk(2000, bytes(4800 * 8), 48000, 2, 4)
        result = ASRResult('対象後', ('対象', '後'), (0.05, 0.18))
        endpoint, trailing, score = NativeAudio._timestamp_endpoint(
            (first, second), result, observed_token_count=1, target_reached=True)
        self.assertEqual(endpoint, 1980)
        self.assertAlmostEqual(trailing, 20.0)
        self.assertLess(score, .45)

        endpoint, trailing, score = NativeAudio._timestamp_endpoint(
            (first, second), result, observed_token_count=1, target_reached=False)
        self.assertEqual((endpoint, trailing, score), (-1, 0.0, 0.0))

        # A final token start has no duration in SenseVoice and cannot be
        # converted into an endpoint by adding a guessed token length.
        endpoint, trailing, score = NativeAudio._timestamp_endpoint(
            (first, second), ASRResult('対象', ('対象',), (0.05,)),
            observed_token_count=1, target_reached=True)
        self.assertEqual((endpoint, trailing, score), (-1, 0.0, 0.0))

    def test_later_vad_state_cannot_retroactively_complete_decode_snapshot(self):
        from threading import Event
        gate, release = Event(), Event()
        events = []
        class SlowRecognizer:
            def recognize(self, chunks):
                gate.set()
                release.wait(2)
                return 'えっ嘘でしょ。'
        class Activity:
            def observe_float32_stereo(self, frames):
                return VADDecision(True, 0, contains_speech=True)
        native = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
            lambda _t, event, _detail: events.append(event),
            recognizer=SlowRecognizer(), activity=Activity())
        try:
            native.history.start(11)
            native.begin(Token(1, 1), 'えっ嘘でしょ。')
            pcm = PCMChunk(native.clock(), bytes(960 * 8), 48000, 2, 4)
            native.history.push(pcm, target_pid=11)
            native.feed(pcm)
            self.assertTrue(gate.wait(1))
            # Even if later captured audio seems quiet, this inference's
            # snapshot has no endpoint. It must not complete a dialogue.
            with native._lock:
                native.tail_ms = 400
            release.set()
            end = time.monotonic() + 2
            while time.monotonic() < end:
                with native._lock:
                    busy = native._inflight
                if not busy:
                    break
                time.sleep(.005)
            self.assertNotIn('completed', events)
        finally:
            release.set()
            native.close()


if __name__ == '__main__':
    unittest.main()

class AdvanceOriginAudioWindowTests(unittest.TestCase):
    """Real NativeAudio state ownership with controlled PCM/VAD/ASR timestamps."""

    def test_voice_ending_before_hook_is_replayed_and_matched_to_next_token(self):
        moments = [1000]
        activity = iter((VADDecision(True, 0), VADDecision(False, 200),
                         VADDecision(False, 400)))
        recognition = []
        end_observations = []
        events = []
        ready = threading.Event()

        class Recognizer:
            def recognize(self, chunks):
                recognition.append(tuple(c.at_ms for c in chunks))
                return '合図です'

        def report(token, status, detail):
            events.append((token, status))
            if status == 'completed':
                ready.set()

        audio = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
                            report, recognizer=Recognizer(),
                            activity=SimpleNamespace(observe_float32_stereo=lambda _: next(activity),
                                                     reset=lambda: None),
                            clock=lambda: moments[0],
                            on_voice_end=lambda t, m: end_observations.append((t, m)))
        try:
            audio.history.start(11)
            audio.boundary()  # prior advance input, long before Luna's flush
            for when in (1100, 1300, 1500):
                packet = PCMChunk(when, bytes(960 * 8), 48000, 2, 4)
                audio.history.push(packet, target_pid=11)
                audio.feed(packet)  # unbound audio cannot issue a completion
            self.assertEqual(events, [])
            moments[0] = 1800
            token = Token(5, 2)
            audio.begin(token, '合図です')
            self.assertTrue(ready.wait(2), events)
            self.assertEqual(recognition, [(1100, 1300, 1500)])
            self.assertEqual([v for v in events if v[1] == 'completed'], [(token, 'completed')])
            self.assertEqual(end_observations[0][1]['estimated_end_ms'], 1100)
            self.assertLess(audio.rise_ms, audio.start_ms)
            self.assertEqual(end_observations[0][1]['source'], 'asr_vad')
        finally:
            audio.close()

    def test_previous_interval_audio_is_not_replayed_after_next_send(self):
        times = [1000]
        viewed = []
        audio = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
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
            self.assertEqual(audio.rise_ms, 1100)
        finally:
            audio.close()

    def test_no_send_does_not_claim_pre_hook_voice_from_prior_line(self):
        times = [1600]
        audio = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
                            lambda *_: None, recognizer=SimpleNamespace(recognize=lambda _: ''),
                            activity=SimpleNamespace(observe_float32_stereo=lambda _: VADDecision(True, 0),
                                                     reset=lambda: None),
                            clock=lambda: times[0])
        try:
            audio.history.start(11)
            audio.history.push(PCMChunk(1200, bytes(960 * 8), 48000, 2, 4), target_pid=11)
            audio.begin(Token(2, 1), '新しい本文')  # no observed advance
            self.assertEqual(audio.rise_ms, -1)
        finally:
            audio.close()
