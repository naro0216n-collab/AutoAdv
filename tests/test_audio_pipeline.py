import io
import struct
import unittest
from autoadv.process_audio import HEADER, read_packet
from autoadv.audio_match import TargetMatcher

class CaptureContractTests(unittest.TestCase):
    def test_heartbeat_never_fabricates_absence(self):
        at,packet=read_packet(io.BytesIO(HEADER.pack(0,123,0)))
        self.assertEqual(at,123)
        self.assertIsNone(packet)

    def test_stereo_pcm_retains_format(self):
        pcm=struct.pack('<4f',0,0,.1,.1)
        at,chunk=read_packet(io.BytesIO(HEADER.pack(len(pcm),123,1)+pcm))
        self.assertEqual((chunk.sample_rate,chunk.channels,chunk.duration_ms),(48000,2,1000/48000*2))

    def test_partial_and_oversize_rejected(self):
        with self.assertRaises(EOFError):read_packet(io.BytesIO(HEADER.pack(8,123,1)))
        with self.assertRaises(ValueError):read_packet(io.BytesIO(HEADER.pack(48000*8+8,123,1)))

class ASRContractTests(unittest.TestCase):
    def test_prefix_matching_and_stable_tail(self):
        m=TargetMatcher('「おはよう。」')
        a=m.observe('おは')
        self.assertGreater(a.onset_score,.9)
        self.assertLess(a.coverage,1)
        a=m.observe('おはよう')
        self.assertEqual(a.tail_confidence,0)
        b=m.observe('おはよう')
        self.assertEqual(b.tail_confidence,1)

    def test_reversed_hypothesis_blocks_tail(self):
        m=TargetMatcher('おはよう')
        m.observe('おはよう')
        self.assertFalse(m.observe('おは').monotonic)
        self.assertEqual(m.observe('おはよう').tail_confidence,0)

    def test_unrelated_utterance_not_onset(self):
        m=TargetMatcher('おはよう')
        a=m.observe('今日はおはよう')
        self.assertEqual((a.coverage,a.onset_score),(0,0))

if __name__=='__main__':unittest.main()

class VADFramingTests(unittest.TestCase):
    @unittest.skipUnless(__import__('importlib').util.find_spec('numpy') is not None,
                         'requires the audio runtime extras')
    def test_partial_pcm_frames_are_buffered_across_callbacks(self):
        from autoadv.audio_match import VoiceActivity
        from unittest.mock import patch
        class FakeVad:
            def __init__(self,mode):self.parts=[]
            def is_speech(self,frame,rate):
                self.parts.append((len(frame),rate))
                return True
        with patch.dict('sys.modules',{'webrtcvad':type('M',(),{'Vad':FakeVad})()}):
            v=VoiceActivity()
        import struct
        p=struct.pack('<960f',*([.1]*960))  # 10ms stereo, 48kHz
        self.assertFalse(v.observe_float32_stereo(p).active)
        self.assertTrue(v.observe_float32_stereo(p).active)
        self.assertEqual(v.vad.parts,[(1920,48000)])
