"""No native ownership may be inferred from foreign sound or partial display."""
import unittest
from dataclasses import replace
from autoadv.audio_buffer import AudioHistory,PCMChunk
from autoadv.audio_recognition import AudioRecognitionPipeline,TargetLine
from autoadv.audio_match import VADDecision
from autoadv.contracts import EventKind as E


class FakeVoice:
    def __init__(self):self.active=False;self.tail=0
    def observe_float32_stereo(self,frames):return VADDecision(self.active,self.tail)


class FakeASR:
    def __init__(self,text):self.text=text
    def recognize(self,samples):return self.text


class GenerationAudioTests(unittest.TestCase):
    def make(self):
        history=AudioHistory(185000)
        history.start(1234)
        pcm=PCMChunk(2000,b'\0' * (960 * 2 * 4),48000,2,4)
        history.push(pcm,target_pid=1234)
        voice=FakeVoice()
        events=[]
        p=AudioRecognitionPipeline(history,FakeASR('おはよう'),events.append,activity=voice,clock=lambda:3000)
        self.addCleanup(p.close)
        return p,pcm,voice,events

    def test_voice_onset_during_text_animation_is_preserved(self):
        p,pcm,v,events=self.make()
        v.active=True
        p.feed(pcm)
        # Audio can start before a HOOK line is stabilized; retain its onset.
        p.begin(TargetLine('s',1,1,'おはよう',2100))
        self.assertTrue(p.fresh_onset)
        p._infer(p.target,p.history.since(0))
        self.assertIn(E.NATIVE_STARTED,[x.kind for x in events])

    def test_asset_assignment_does_not_claim_music_as_voice(self):
        p,pcm,v,events=self.make()
        p.begin(TargetLine('s',1,1,'おはよう',2100,metadata_voice_id='asset-A'))
        # No fresh native onset evidence; the metadata is not an audio match.
        p._infer(p.target,p.history.since(0))
        self.assertEqual(events,[])

    def test_previous_line_onset_does_not_appear_on_next_line(self):
        p,pcm,v,events=self.make()
        v.active=True;p.feed(pcm)
        p.mark_boundary(2100)
        p.begin(TargetLine('s',2,2,'おはよう',2200))
        self.assertFalse(p.fresh_onset)
        p._infer(p.target,p.history.since(0))
        self.assertEqual(events,[])

    def test_new_voice_onset_after_verified_line_boundary_is_retained(self):
        p,pcm,v,events=self.make()
        v.active=True;p.feed(pcm)
        # A pre-transition rise cannot be attributed to the next line.
        p.mark_boundary(2200)
        p.begin(TargetLine('s',2,2,'おはよう',2300))
        self.assertFalse(p.fresh_onset)
        # A fresh audio rise after the previous line boundary must be retained
        # until it can be attributed to the next candidate.
        v.active=False
        p.feed(PCMChunk(2300,pcm.frames,48000,2,4))
        v.active=True
        p.feed(PCMChunk(2400,pcm.frames,48000,2,4))
        p.mark_boundary(2200)
        p.begin(TargetLine('s',3,3,'おはよう',2500))
        self.assertTrue(p.fresh_onset)

    def test_partial_line_retarget_keeps_epoch_and_onset(self):
        p,pcm,v,events=self.make()
        v.active=True;p.feed(pcm)
        p.begin(TargetLine('s',1,1,'おは',2100))
        first_id=p.utterance_id
        p.retarget(1,1,'おはよう')
        self.assertEqual(p.target.text,'おはよう')
        self.assertEqual(p.utterance_id,first_id)
        self.assertTrue(p.fresh_onset)

    def test_cancelled_generation_asr_failure_cannot_pause_next_line(self):
        p,pcm,v,events=self.make()
        class BrokenASR:
            def recognize(self,samples):raise RuntimeError('stale decoder failure')
        p.asr=BrokenASR()
        p.begin(TargetLine('s',1,1,'おはよう',2000))
        old=p.target
        p.begin(TargetLine('s',2,2,'次の本文',2200))
        p._infer(old,())
        self.assertEqual(events,[])
        p._infer(p.target,())
        self.assertEqual([e.kind for e in events],[E.CAPTURE_HEALTH])
        self.assertFalse(events[0].capture_ok)

    def test_history_retains_target_start_longer_than_15_seconds(self):
        h=AudioHistory()
        h.start(42)
        for t in range(0,40001,1000):
            h.push(PCMChunk(t,b'\0'*8,48000,2,4),target_pid=42)
        self.assertEqual(h.since(0)[0].at_ms,0)
        self.assertEqual(h.since(0)[-1].at_ms,40000)

if __name__=='__main__':unittest.main()
