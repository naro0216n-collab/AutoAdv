from unittest import TestCase
from autoadv.application import ObservationOrchestrator,MetadataVoice
from autoadv.controller import Controller
from autoadv.runtime import Runtime,FreshSnapshot
from autoadv.contracts import ScreenState as SS,ControllerState as CS
from autoadv.screen import ScreenSample
from autoadv.luna_bridge import Candidate

class Input:
    def __init__(self):self.calls=0
    def advance(self):self.calls+=1
class Guard:
    def observe(self):return FreshSnapshot(SS.DIALOGUE,True,True,True,False,False,True,True,True,False,False)
class TTS:
    def apply(self,x):raise AssertionError('not expected')

class AppContracts(TestCase):
    def test_visual_boundary_and_duplicate_without_cycle(self):
        c=Controller(session_id='s'); inp=Input()
        current=[0]
        rt=Runtime(c,Guard(),inp,TTS(),clock=lambda:current[0])
        app=ObservationOrchestrator(rt)
        def observe(now,ready=True,match=True,voice=True):
            current[0]=now
            app.observe(ScreenSample(now,SS.DIALOGUE,ready,match,'本文',True),focused=True,capture_ok=True,
                voice_metadata=MetadataVoice(False,verified=True) if voice else None)
        app.candidate(Candidate(1,1,1,'本文','HOOK'),100)
        observe(200)
        self.assertEqual(c.state,CS.WAIT_SILENT_DWELL)
        observe(4000)
        self.assertEqual(inp.calls,1)
        self.assertEqual(c.state,CS.WAIT_TRANSITION_ACK)
        app.candidate(Candidate(1,2,2,'本文','HOOK'),4100)
        observe(4300)
        self.assertEqual(c.line.epoch,1)
        self.assertEqual(inp.calls,1)
        observe(4500,ready=False)
        app.candidate(Candidate(1,3,3,'本文','HOOK'),4600)
        observe(4800)
        self.assertEqual(c.line.epoch,2)
        self.assertEqual(c.state,CS.WAIT_SILENT_DWELL)
        observe(8000)
        self.assertEqual(inp.calls,2)

    def test_no_line_when_text_unverified(self):
        c=Controller(session_id='s');inp=Input()
        app=ObservationOrchestrator(Runtime(c,Guard(),inp,TTS(),clock=lambda:1000))
        app.candidate(Candidate(1,1,1,'本文','HOOK'),100)
        app.observe(ScreenSample(200,SS.DIALOGUE,True,False,'別本文',True),focused=True,capture_ok=True)
        self.assertIsNone(c.line)
        self.assertEqual(inp.calls,0)

class CalibratedAbsence(TestCase):
    def test_unconfirmed_absence_does_not_become_metadata(self):
        from autoadv.controller import Settings
        c=Controller(Settings(allow_heuristic_absence=False),session_id='s')
        rt=Runtime(c,Guard(),Input(),TTS(),clock=lambda:5000)
        app=ObservationOrchestrator(rt,heuristic_absence_ready=lambda:True)
        app.candidate(Candidate(1,1,1,'本文','HOOK'),10)
        app.observe(ScreenSample(1000,SS.DIALOGUE,True,True,'本文',True),focused=True,capture_ok=True)
        self.assertEqual(c.line.voice.name,'UNRESOLVED')
        self.assertNotEqual(c.state,CS.WAIT_TRANSITION_ACK)

    def test_calibrated_inference_explicit_heuristic(self):
        from autoadv.controller import Settings
        c=Controller(Settings(allow_heuristic_absence=True),session_id='s')
        t=[1000];i=Input()
        rt=Runtime(c,Guard(),i,TTS(),clock=lambda:t[0])
        app=ObservationOrchestrator(rt,heuristic_absence_ready=lambda:True)
        app.candidate(Candidate(1,1,1,'本文','HOOK'),10)
        app.observe(ScreenSample(1000,SS.DIALOGUE,True,True,'本文',True),focused=True,capture_ok=True)
        self.assertEqual(c.line.evidence.name,'NONE')
        t[0]=3500
        app.observe(ScreenSample(3500,SS.DIALOGUE,True,True,'本文',True),focused=True,capture_ok=True)
        self.assertEqual(c.line.evidence.name,'HEURISTIC')
        self.assertEqual(i.calls,1)
