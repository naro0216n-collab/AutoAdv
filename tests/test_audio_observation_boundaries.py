"""Packet boundaries and overlapping inference must not fabricate speech evidence."""
from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_match import VoiceActivity, VADDecision
from autoadv.conversation import Token
from autoadv.native_audio import NativeAudio


class PacketVadBoundaryTests(unittest.TestCase):
    def test_packet_with_speech_then_silence_is_not_currently_playing(self):
        speech_by_frame = iter([True, True, False, False, False, False])
        activity = VoiceActivity()
        activity.vad = SimpleNamespace(is_speech=lambda *_: next(speech_by_frame))
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


if __name__ == '__main__':
    unittest.main()
