import unittest
from autoadv.native_gate import AudioEvidence, NativeVoiceGate
from autoadv.audio_buffer import AudioHistory, PCMChunk
from autoadv.contracts import EventKind as E


class NativeGateTests(unittest.TestCase):
    def setUp(self):
        self.g=NativeVoiceGate();self.g.begin_line("s",1,1)
        self.base=dict(session_id="s",line_epoch=1,cancel_token=1,at_ms=1000,
            utterance_id="u1",capture_ok=True,source_line_matches=True,target_score=.97,
            vad_speech=True,vad_tail_silence_ms=0,asr_coverage=.0,
            asr_tail_confidence=0,asr_order_consistent=True)

    def event(self,**kwargs):
        return AudioEvidence(**(self.base | kwargs))

    def test_onset_cannot_be_vad_only(self):
        self.assertEqual(self.g.observe(self.event(target_score=0.1)),[])
        self.assertEqual(self.g.observe(self.event(source_line_matches=False)),[])
        self.assertEqual([x.kind for x in self.g.observe(self.event())],[E.NATIVE_STARTED])

    def test_tail_requires_both_asr_and_vad(self):
        self.g.observe(self.event())
        self.assertEqual(self.g.observe(self.event(at_ms=1100,asr_coverage=1,asr_tail_confidence=1)),[])
        self.assertEqual(self.g.observe(self.event(at_ms=1200,vad_tail_silence_ms=800)),[])
        self.assertEqual([x.kind for x in self.g.observe(self.event(at_ms=1300,vad_tail_silence_ms=800,
                        asr_coverage=1,asr_tail_confidence=1))],[E.NATIVE_COMPLETED])

    def test_reject_old_epoch_and_previous_voice(self):
        self.assertEqual(self.g.observe(self.event(line_epoch=2)),[])
        self.g.observe(self.event())
        self.assertEqual(self.g.observe(self.event(at_ms=1400,utterance_id="u2",vad_tail_silence_ms=1000,
                                                 asr_coverage=1,asr_tail_confidence=1)),[])

    def test_reset_on_new_line(self):
        self.g.observe(self.event())
        self.g.begin_line("s",2,2)
        self.assertEqual(self.g.observe(self.event()),[])

    def test_metadata_identity_still_requires_vad_for_start(self):
        self.assertEqual(self.g.observe(self.event(metadata_voice_id_matches=True, vad_speech=False)),[])
        self.assertEqual([x.kind for x in self.g.observe(self.event(metadata_voice_id_matches=True,target_score=0))],[E.NATIVE_STARTED])


class AudioHistoryTests(unittest.TestCase):
    def test_ring_buffer_and_pid_change(self):
        buf=AudioHistory(100);buf.start(100)
        for at in (0,60,120,180):
            buf.push(PCMChunk(at,b"\x00\x00"*480,48000,1,2),target_pid=100)
        self.assertEqual([x.at_ms for x in buf.since(0)],[120,180])
        self.assertTrue(buf.healthy)
        buf.push(PCMChunk(200,b"\x00\x00",48000,1,2),target_pid=101)
        self.assertFalse(buf.healthy)
        self.assertEqual(buf.fail_reason,"target_pid_changed")

if __name__=="__main__":unittest.main()