import unittest
from autoadv.speaker import split_speaker,VoiceRoute,SpeakerRouter
from autoadv.text import normalize
from autoadv.contracts import TTSPolicy
class SpeakerTests(unittest.TestCase):
    def test_dialogue_body_only_and_standalone_name(self):
        self.assertEqual(split_speaker('大和「おはようございます」'),('大和','おはようございます'))
        line=normalize('大和「おはようございます」')
        self.assertEqual(line.raw,'大和「おはようございます」')
        self.assertEqual(line.spoken,'おはようございます')
        self.assertEqual(normalize('京(みやこ)は東京へ').spoken,'みやこは東京へ')
        self.assertEqual(normalize('京は東京へ',names={'京':'みやこ'}).spoken,'みやこは東京へ')
    def test_no_tts_for_native_speaker(self):
        route=SpeakerRouter({'大和':VoiceRoute(TTSPolicy.SKIP)},VoiceRoute(TTSPolicy.DEFAULT))
        self.assertEqual(route.resolve('大和「はい」').policy,TTSPolicy.SKIP)
        self.assertEqual(route.resolve('地の文').policy,TTSPolicy.DEFAULT)
if __name__=='__main__':unittest.main()
