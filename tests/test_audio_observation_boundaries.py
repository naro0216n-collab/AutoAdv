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
from autoadv.audio_match import TargetMatcher, VoiceActivity, VADDecision
from autoadv.audio_evidence import VoiceEvidence, VoiceState, assess_voice
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
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
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

    def test_native_audio_keeps_full_active_line_in_every_decode_then_returns_to_rolling_history(self):
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
        native = NativeAudio(
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
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
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
        audio = NativeAudio(11, Path('unused.dll'), Path('unused.onnx'), Path('unused.tokens'),
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
            self.assertEqual(audio.vad_onset_ms, 1100)
            self.assertEqual(audio._sample_start(), audio.window_start_ms)
        finally:
            audio.close()

class VoiceEvidenceArchitectureTests(unittest.TestCase):
    def _evidence(self, alignment, *, capture_end=2000, target_last=None,
                  boundary=None, post=0.0, vad_tail=0, vad_end=None,
                  stable=1, progress_delta=None):
        return VoiceEvidence(
            alignment=alignment, audio_ms=2000.0, capture_end_ms=capture_end,
            observation_ms=capture_end, target_last_token_ms=target_last, target_boundary_ms=boundary,
            post_target_pcm_ms=post, vad_active=False, vad_contains_speech=False,
            vad_onset_ms=None, vad_tail_ms=vad_tail, vad_end_ms=vad_end,
            stable_alignment_observations=stable,
            phonetic_progress_delta=progress_delta)

    def test_recent_target_position_is_playing_without_decode_wall_clock_freshness(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようござ')
        assessed = assess_voice(self._evidence(
            alignment, capture_end=2000, target_last=1900))
        self.assertEqual(assessed.state, VoiceState.PLAYING)

    def test_late_redecode_of_old_target_does_not_mean_currently_playing(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようござ')
        assessed = assess_voice(self._evidence(
            alignment, capture_end=5000, target_last=1200))
        self.assertEqual(assessed.state, VoiceState.INCONCLUSIVE)

    def test_following_token_boundary_plus_captured_tail_can_end_without_vad_silence(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようございます')
        assessed = assess_voice(self._evidence(
            alignment, capture_end=2000, target_last=1200, boundary=1400, post=600))
        self.assertEqual(assessed.state, VoiceState.ENDED)
        self.assertEqual(assessed.estimated_end_ms, 1400)
        self.assertEqual(assessed.endpoint_source, 'asr_timestamp')

    def test_alignment_stability_is_supporting_evidence_not_an_endpoint_by_itself(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようございます')
        assessed = assess_voice(self._evidence(
            alignment, capture_end=5000, target_last=1000, stable=5))
        self.assertEqual(assessed.state, VoiceState.INCONCLUSIVE)
        self.assertLess(assessed.endpoint_score, .45)

    def test_process_vad_endpoint_cannot_attribute_unrelated_speech(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('別の台詞です')
        assessed = assess_voice(self._evidence(
            alignment, capture_end=2000, vad_tail=500, vad_end=1500))
        self.assertNotEqual(assessed.state, VoiceState.ENDED)

    def test_token_timing_observations_are_audio_timeline_measurements(self):
        samples = (
            PCMChunk(1000, bytes(48000 // 10 * 8), 48000, 2, 4),
            PCMChunk(1600, bytes(48000 // 10 * 8), 48000, 2, 4),
        )
        # 100 ms of PCM, then a 500 ms wall-clock gap, then 100 ms of PCM.
        result = ASRResult('AB', ('A', 'B'), (0.05, 0.15))
        last, boundary, post = NativeAudio._token_timing_observations(samples, result, 1)
        self.assertEqual(last, 950)
        self.assertEqual(boundary, 1550)
        self.assertAlmostEqual(post, 50.0)

    def test_healthy_heartbeat_ages_playing_evidence_without_fabricating_endpoint(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようござ')
        initial = self._evidence(
            alignment, capture_end=1000, target_last=950)
        self.assertEqual(assess_voice(initial).state, VoiceState.PLAYING)

        observed = []
        native = NativeAudio(
            11, Path('unused'), Path('unused'), Path('unused'), lambda *_: None,
            recognizer=SimpleNamespace(recognize=lambda _: ''), g2p=None,
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _: VADDecision(False, 0), reset=lambda: None),
            clock=lambda: 1000, on_evidence=lambda token, evidence: observed.append((token, evidence)))
        try:
            token = Token(1, 1)
            native.token = token
            native._latest_evidence = initial
            native._last_heartbeat_evidence_ms = 1000
            native.health(CaptureHealth(1800, True, 'helper_alive'))

            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][0], token)
            refreshed = observed[0][1]
            self.assertEqual(refreshed.capture_end_ms, initial.capture_end_ms)
            self.assertEqual(refreshed.target_last_token_ms, initial.target_last_token_ms)
            self.assertEqual(refreshed.observation_ms, 1800)
            self.assertEqual(assess_voice(refreshed).state, VoiceState.INCONCLUSIVE)
            self.assertNotEqual(assess_voice(refreshed).state, VoiceState.ENDED)
        finally:
            native.close()

    def test_native_audio_reports_evidence_not_completion_state(self):
        assessments = []
        metrics = []
        ready = threading.Event()

        class Recognizer:
            def recognize(self, _chunks):
                return ASRResult('おはようございます背景', tuple('おはようございます背景'),
                                 tuple(i * .1 for i in range(len('おはようございます背景'))))

        native = NativeAudio(
            11, Path('unused'), Path('unused'), Path('unused'), lambda *_: None,
            recognizer=Recognizer(), g2p=None,
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _: VADDecision(False, 0), reset=lambda: None),
            clock=lambda: 2000,
            on_inference=lambda _t, m: metrics.append(m),
            on_evidence=lambda t, e: (assessments.append((t, assess_voice(e))), ready.set()))
        try:
            native.history.start(11)
            token = Token(1, 1)
            native.begin(token, 'おはようございます')
            # 2 seconds of one cumulative audio window makes the following-token
            # boundary leave enough captured post-target PCM for ENDED.
            chunk = PCMChunk(2400, bytes(48000 * 2 * 8), 48000, 2, 4)
            native.history.push(chunk, target_pid=11)
            native.feed(chunk)
            self.assertTrue(ready.wait(2))
            self.assertEqual(assessments[-1][0], token)
            self.assertEqual(assessments[-1][1].state, VoiceState.ENDED)
            self.assertFalse(hasattr(native, 'completed'))
            self.assertIn('target_last_token_age_ms', metrics[-1])
            self.assertNotIn('voice_state', metrics[-1])
            self.assertNotIn('assessment_endpoint', metrics[-1])
        finally:
            native.close()

    def test_old_generation_inference_cannot_assess_new_line(self):
        entered = threading.Event()
        release = threading.Event()
        assessments = []

        class Recognizer:
            def recognize(self, _chunks):
                entered.set()
                release.wait(2)
                return ASRResult('古い台詞')

        native = NativeAudio(
            11, Path('unused'), Path('unused'), Path('unused'), lambda *_: None,
            recognizer=Recognizer(), g2p=None,
            activity=SimpleNamespace(
                observe_float32_stereo=lambda _: VADDecision(True, 0), reset=lambda: None),
            clock=lambda: 1000, on_evidence=lambda t, e: assessments.append((t, assess_voice(e).state)))
        try:
            native.history.start(11)
            old = Token(1, 1)
            native.begin(old, '古い台詞')
            chunk = PCMChunk(1400, bytes(48000 * 8), 48000, 2, 4)
            native.history.push(chunk, target_pid=11)
            native.feed(chunk)
            self.assertTrue(entered.wait(1))
            native.begin(Token(1, 2), '新しい台詞')
            release.set()
            time.sleep(.05)
            self.assertFalse(any(t == old for t, _ in assessments))
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

        def report(token, evidence):
            assessment = assess_voice(evidence)
            events.append((token, assessment.state))
            if assessment.state is VoiceState.ENDED:
                end_observations.append((token, assessment, evidence))
                ready.set()

        audio = NativeAudio(11, Path('unused'), Path('unused'), Path('unused'),
                            lambda *_: None, recognizer=Recognizer(),
                            activity=SimpleNamespace(observe_float32_stereo=lambda _: next(activity),
                                                     reset=lambda: None),
                            clock=lambda: moments[0], on_evidence=report)
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
            self.assertEqual([v for v in events if v[1] is VoiceState.ENDED], [(token, VoiceState.ENDED)])
            self.assertEqual(end_observations[0][1].estimated_end_ms, 1100)
            self.assertLess(audio.vad_onset_ms, audio.start_ms)
            self.assertEqual(end_observations[0][1].endpoint_source, 'asr_vad')
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
            self.assertEqual(audio.vad_onset_ms, 1100)
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
            self.assertEqual(audio.vad_onset_ms, -1)
        finally:
            audio.close()
