import io
import unittest
import wave
from unittest.mock import patch
from types import SimpleNamespace
from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import HookSelection, Profile, SPEAKER_ONLY_RULE
from autoadv.voice import synthesize


class NativeAndAudioContract(unittest.TestCase):
    def test_speaker_is_never_carried_to_a_different_flush_batch(self):
        p = Profile('id', 'game', hooks=[HookSelection('D', 1, 2),
                                         HookSelection('S', 4, 5)],
                    body_sources=['D|1|2'], speaker_sources=['S|4|5'],
                    source_rules={'S|4|5': [SPEAKER_ONLY_RULE]})
        c = Controller(lambda *_: None, lambda _: True)
        c.begin(p, Target(11, 22), 'host', 1)
        body = ThreadSource(11, 10, 1, 2, 'D', 'body')
        speaker = ThreadSource(11, 20, 4, 5, 'S', 'speaker')
        def e(src, seq, text):
            return HookEvent('output', 'host', 1, seq, seq * 1_000_000, 11, src, text)
        first = c.observe_group([e(body, 1, '「台詞」'), e(speaker, 2, '花子')])
        self.assertIsNotNone(first)
        self.assertEqual(c.utterance.speaker, '花子')
        c.permit(True)
        self.assertIsNone(c.observe_group([e(speaker, 3, '太郎')]))
        self.assertTrue(c.authorized)
        second = c.observe_group([e(body, 4, '次の台詞')])
        self.assertIsNotNone(second)
        self.assertEqual(c.utterance.speaker, '')

    def test_voice_request_checks_wav_and_real_voice(self):
        sound = io.BytesIO()
        with wave.open(sound, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\x00\x00')
        payload = sound.getvalue()
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return None
            def read(self): return payload
        calls = []
        def opener(req, timeout):
            calls.append(req)
            return Response()
        with patch('autoadv.voice.request.urlopen', opener):
            self.assertEqual(synthesize('http://127.0.0.1:8088', 'こんにちは', 'voice-1'), payload)
            self.assertEqual(synthesize('http://127.0.0.1:8088', 'こんにちは', 'voice-2'), payload)
        self.assertIn(b'"voice": "voice-1"', calls[0].data)
        self.assertIn(b'"voice": "voice-2"', calls[1].data)
        with self.assertRaises(ValueError):
            synthesize('http://127.0.0.1:8088', 'こんにちは', '')


if __name__ == '__main__':
    unittest.main()
