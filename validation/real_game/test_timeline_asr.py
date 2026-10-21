"""Timing suite registration and evidence shape, not fake ASR quality claims."""
import unittest

from validation.real_game.timeline_asr import recorded_cases, replay
from autoadv.native_audio import NativeAudio
from pathlib import Path


class TimelineSources(unittest.TestCase):
    def test_timeline_covers_pinned_normal_and_whisper_recordings(self):
        rows = list(recorded_cases('corpus'))
        self.assertEqual(len(rows), 14)
        self.assertEqual({row[3] for row in rows}, {'recorded_voice'})
        self.assertEqual({row[0].split('-')[0] for row in rows}, {'normal', 'whisper'})
        self.assertTrue(all(len(row[2]) > 0 and row[1].strip() for row in rows))

    def test_timeline_stress_is_explicitly_observational(self):
        rows = list(recorded_cases('stress'))
        self.assertEqual(len(rows), 8)
        self.assertIn('nonlexical', {row[3] for row in rows})
        self.assertIn('unverified_same_text', {row[3] for row in rows})
        self.assertTrue(all(len(row[2]) > 0 for row in rows))


class RealPathTimedPlayback(unittest.TestCase):
    def test_voice_finishing_before_hook_uses_native_audio_and_controller_to_send(self):
        name, text, pcm, kind = next(iter(recorded_cases('corpus')))
        class FixedTranscript:
            # Only the ASR observation is fixed. Actual completion and
            # input authority belong to product NativeAudio and Controller.
            def recognize(self, chunks):
                return text
        native = NativeAudio(42, Path('unused.dll'), Path('unused.onnx'),
                             Path('unused.tokens'), lambda *_: None,
                             recognizer=FixedTranscript())
        try:
            native.history.start(native.pid)
            result = replay(native, index=1, name=name, display=text,
                            mono=pcm, kind=kind, chunk_ms=40, tail_ms=600,
                            hook_delay_ms=2600)
        finally:
            native.close()
        self.assertEqual(result['native_completions'], 1)
        self.assertEqual(result['presentation_reason'], 'native')
        self.assertEqual(len(result['input_attempts']), 1)
        self.assertGreaterEqual(result['input_attempts'][0]['at_ms'], 2600)
        self.assertGreater(result['recording_end_to_input_ms'], 0)


if __name__ == '__main__':
    unittest.main()
