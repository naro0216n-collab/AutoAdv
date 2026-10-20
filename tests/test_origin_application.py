"""Real Application candidate sequencing, audio presentation and post-input HOOK."""
import unittest
from autoadv.application import ObservationOrchestrator
from autoadv.controller import Controller,Settings
from autoadv.contracts import ControllerState as S,EventKind as E,Envelope,VoicePresence,Evidence
from autoadv.observation import OriginObservation
from autoadv.runtime import Runtime,FreshSnapshot

class NullTTS:
    def apply(self,e):pass
class Input:
    def __init__(self):self.count=0
    def advance(self):self.count+=1
class Guard:
    def __init__(self):self.manual=False
    def observe(self):return FreshSnapshot(focused=True,capture_ok=True,
        native_voice_active=False,tts_playing=False,target_window_matches=True,
        input_available=True,manual_override=self.manual)
class OriginApplicationContracts(unittest.TestCase):
    def setUp(self):
        self.now=0;self.ctrl=Controller(Settings(autoplay=True),session_id='s')
        self.input=Input();self.guard=Guard()
        self.rt=Runtime(self.ctrl,self.guard,self.input,NullTTS(),clock=lambda:self.now)
        self.bound=True;self.started=[]
        self.app=ObservationOrchestrator(self.rt,binding_ok=lambda:self.bound,
            on_line_started=lambda *x:self.started.append(x),stable_ms=200)
    def packet(self,n,text,now,*,kind='hook',origin='p:1',generation=1):
        self.now=now;self.app.candidate(OriginObservation(n,text,text,kind=kind,
            source_generation=generation,origin=origin),now)
    def tick(self,now):
        self.now=now;self.app.tick(now,focused=True,capture_ok=True)
    def absent(self,now):
        self.now=now;line=self.ctrl.line
        self.rt.post(Envelope(E.VOICE_PRESENCE,'s',now,line_epoch=line.epoch,
                     cancel_token=line.token,voice=VoicePresence.ABSENT,evidence=Evidence.METADATA));self.rt.step()
    def test_hook_without_image_calibration_drives_audio_and_one_input(self):
        self.packet(1,'大和「おはよう」',10)
        self.tick(250);self.absent(260);self.tick(3000)
        self.assertEqual(self.ctrl.line.text.body,'おはよう')
        self.assertEqual(self.input.count,1)
        self.assertEqual(self.ctrl.state,S.WAIT_NEXT_HOOK)
    def test_new_hook_after_input_drives_next_line_without_scene_scanner(self):
        self.packet(1,'本文',10);self.tick(250);self.absent(260);self.tick(3000)
        self.packet(2,'本文',3010);self.tick(4000)
        self.assertEqual(self.input.count,1)
        self.packet(3,'次の本文',4010);self.tick(4250)
        self.assertEqual(self.ctrl.line.epoch,2)
        self.assertEqual(self.ctrl.line.text.body,'次の本文')
        self.absent(4300);self.tick(7000)
        self.assertEqual(self.input.count,2)
    def test_foreign_hook_invalidates_after_input(self):
        self.packet(1,'本文',10);self.tick(250);self.absent(260);self.tick(3000)
        self.packet(2,'異なる本文',3200,origin='other')
        # Foreign source after our input invalidates the old line and revokes
        # authority; monitoring remains available for a fresh selected source.
        self.assertEqual(self.ctrl.state,S.WAIT_LINE)
        self.assertIsNone(self.ctrl.line)
        self.assertFalse(self.ctrl.autoplay_authorized)
        self.assertEqual(self.input.count,1)
    def test_input_manual_preflight_latches_pause(self):
        self.packet(1,'本文',10);self.tick(250);self.absent(260)
        self.guard.manual=True;self.tick(3000)
        self.assertEqual(self.input.count,0)
        self.assertEqual(self.ctrl.state,S.PAUSED)
    def test_source_unstable_invalidates_line(self):
        self.packet(1,'本文',10);self.tick(40)
        self.app.unstable(50)
        self.assertEqual(self.ctrl.state,S.PAUSED)
