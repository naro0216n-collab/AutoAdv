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
from autoadv.audio_match import VoiceActivity, VADDecision
from autoadv.conversation import Controller, Target, Token
from autoadv.model import AdvanceMode, HookSelection, Profile
from autoadv.native_audio import NativeAudio


class PacketVadBoundaryTests(unittest.TestCase):
    def test_prior_line_packet_cannot_enter_new_line_history(self):
        history = AudioHistory()
        history.start(11)
        previous = PCMChunk(980, bytes(40 * 48 * 8), 48000, 2, 4)
        current = PCMChunk(1020, bytes(20 * 48 * 8), 48000, 2, 4)
        history.push(previous, target_pid=11)
        history.push(current, target_pid=11)
        self.assertEqual(history.since(1000), (current,))

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
                            lambda *_: None, activity=activity, vad_only=True,
                            clock=lambda: 1000)
        try:
            token = Token(1, 1)
            audio.begin(token, 'テスト')
            audio.feed(PCMChunk(1000, bytes(960 * 8), 48000, 2, 4))
            self.assertEqual(audio.rise_ms, 1000)
            self.assertFalse(audio.is_confirmed_playing(token))
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
                            vad_only=True, clock=lambda: times[0])
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

    def test_delayed_voice_uses_its_own_acoustic_window(self):
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
            self.assertEqual(heard[0], (1500,))  # rise - 200ms = 1400ms
        finally:
            audio.close()

    def test_corrected_native_match_reports_completion_to_existing_controller_once(self):
        sent = []
        ctrl = Controller(lambda target, key: sent.append((target.pid, key)), lambda _: True)
        profile = Profile('p', 'P', advance_mode=AdvanceMode.AUDIO,
                          hooks=[HookSelection('X', 1, 2)])
        ctrl.begin(profile, Target(11, 2), 'host', 1)
        source = SimpleNamespace(hook_code='X', context=1, context2=2)
        token = ctrl.observe(generation='host', connection=1, sequence=1,
                             source=source, text='えっ嘘でしょ', pid=11)
        self.assertIsNotNone(token)
        ctrl.permit(True)
        events = []
        readings = iter(['え嘘でしょ', 'え嘘でしょ'])
        vad_frames = iter([VADDecision(True, 0), VADDecision(False, 340)])
        clock = [1000]

        def notify(observed, event, detail):
            events.append((observed, event))
            if event in ('started', 'completed'):
                accepted = ctrl.presentation_event(
                    observed, 'native_finished' if event == 'completed' else 'native_started')
                if accepted:
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
                          hooks=[HookSelection('X', 1, 2)])
        controller = Controller(lambda *_: sent.append('input'), lambda *_: True)
        controller.begin(profile, Target(11, 22), 'host', 1)
        controller.permit(True)
        source = SimpleNamespace(hook_code='X', context=1, context2=2)
        token = controller.observe(generation='host', connection=1,
            sequence=1, source=source, text='えっ嘘でしょ。', pid=11)
        self.assertIsNotNone(token)
        def notice(t, event, detail):
            if event == 'started':
                controller.presentation_event(t, 'native_started')
            elif event == 'completed':
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

    def test_real_worker_only_finishes_on_silence_present_in_decoded_pcm(self):
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
            audio.boundary()  # prior SendInput, long before Luna's flush
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
                            lambda *_: None, vad_only=True,
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
                            lambda *_: None, vad_only=True,
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
