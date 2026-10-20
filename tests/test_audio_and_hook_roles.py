import io
import unittest
import wave
from unittest.mock import patch
from types import SimpleNamespace
from autoadv.conversation import Controller, Target
from autoadv.model import AudioMode, HookSelection, Profile
from autoadv.voice import synthesize


class NativeAndAudioContract(unittest.TestCase):
    def test_speaker_output_requires_adjacency_and_selected_source(self):
        p = Profile('id', 'game', hooks=[HookSelection('D', 1, 2, 'text'),
                                         HookSelection('S', 4, 5, 'speaker')])
        c = Controller(lambda *_: None, lambda _: True)
        c.begin(p, Target(11, 22), 'host', 1)
        source_a = SimpleNamespace(hook_code='D', context=1, context2=2, identity='D|1|2')
        source_b = SimpleNamespace(hook_code='S', context=4, context2=5, identity='S|4|5')
        def out(seq, src, text):
            return c.observe(generation='host', connection=1, sequence=seq,
                             source=src, text=text, pid=11)
        self.assertIsNone(out(1, source_b, '花子'))
        self.assertIsNotNone(out(2, source_a, '「台詞」'))
        self.assertEqual(c.utterance.speaker, '花子')
        out(3, source_a, '別台詞')
        self.assertEqual(c.utterance.speaker, '')
        out(4, source_b, '太郎')
        out(6, source_a, '台詞')
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
        self.assertIn(b'"voice": "voice-1"', calls[0].data)
        with self.assertRaises(ValueError):
            synthesize('http://127.0.0.1:8088', 'こんにちは', '')


if __name__ == '__main__':
    unittest.main()
