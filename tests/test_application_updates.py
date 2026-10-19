import unittest
from autoadv.application import ObservationOrchestrator
from autoadv.controller import Controller
from autoadv.contracts import ScreenState as S,ControllerState as CS
from autoadv.screen import ScreenSample
from autoadv.luna_bridge import Candidate
from autoadv.runtime import Runtime,FreshSnapshot

class NeverInput:
    def advance(self):raise AssertionError('input before verified presentation')
class Closed:
    def observe(self):return FreshSnapshot(S.UNKNOWN,False,False,False)
class NoTTS:
    def apply(self,e):raise AssertionError('no TTS')

class ApplicationUpdates(unittest.TestCase):
    def test_partial_visual_recognition_starts_audio_before_ready(self):
        ctrl=Controller(session_id='s')
        started=[];updated=[]
        app=ObservationOrchestrator(Runtime(ctrl,Closed(),NeverInput(),NoTTS(),clock=lambda:500),
            on_line_started=lambda *args:started.append(args),
            on_line_updated=lambda *args:updated.append(args))
        app.candidate(Candidate(1,1,1,'おは','HOOK'),100)
        app.observe(ScreenSample(200,S.DIALOGUE,False,True,'お',True),focused=True,capture_ok=True)
        self.assertEqual(ctrl.state,CS.WAIT_TEXT_READY)
        self.assertIsNone(ctrl.line.ready_ms)
        self.assertEqual(started[0][3],100)
        epoch=ctrl.line.epoch
        app.candidate(Candidate(1,2,2,'おはよう','HOOK'),220)
        self.assertEqual(ctrl.line.epoch,epoch)
        self.assertEqual(ctrl.line.text.raw,'おはよう')
        self.assertEqual(updated[0][:2],(epoch,ctrl.line.token))
        app.observe(ScreenSample(500,S.DIALOGUE,True,True,'おはよう',True),focused=True,capture_ok=True)
        self.assertEqual(ctrl.line.epoch,epoch)
        self.assertEqual(ctrl.line.ready_ms,500)

if __name__=='__main__':unittest.main()

class PartialSpeakerAndEarlyNative(unittest.TestCase):
    def test_native_event_precedes_readiness_without_illegal_tts_configuration(self):
        from autoadv.contracts import Envelope,EventKind as E,Evidence,TTSPolicy
        from autoadv.speaker import SpeakerRouter,VoiceRoute
        c=Controller(session_id='s')
        app=ObservationOrchestrator(Runtime(c,Closed(),NeverInput(),NoTTS(),clock=lambda:300),
            speaker_router=SpeakerRouter({'大和':VoiceRoute(TTSPolicy.SKIP)},
                                         VoiceRoute(TTSPolicy.SELECTED,'fallback')))
        app.candidate(Candidate(1,1,1,'大','HOOK'),100)
        app.observe(ScreenSample(200,S.DIALOGUE,False,True,'大',True),focused=True,capture_ok=True)
        l=c.line
        c.accept(Envelope(E.NATIVE_STARTED,'s',220,line_epoch=l.epoch,cancel_token=l.token,
                          evidence=Evidence.AUDIO_CONFIRMED,native_utterance_id='native'))
        app.candidate(Candidate(1,2,2,'大和「こんにちは」','HOOK'),240)
        app.observe(ScreenSample(300,S.DIALOGUE,True,True,'こんにちは',True),focused=True,capture_ok=True)
        self.assertEqual(c.line.epoch,1)
        self.assertEqual(c.tts_policy,TTSPolicy.SKIP)
        self.assertEqual(c.line.ready_ms,300)
