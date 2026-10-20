"""Public-origin observation is not a line ID; positive screen evidence controls transitions."""
import unittest
from autoadv.application import ObservationOrchestrator
from autoadv.controller import Controller,Settings
from autoadv.contracts import ControllerState,ScreenState,EventKind,Envelope,VoicePresence,Evidence
from autoadv.luna import OriginObservation,decode_origin_text
from autoadv.screen import ScreenSample
from autoadv.runtime import Runtime,FreshSnapshot

class SilentTTS:
    def apply(self,e):raise AssertionError('should not use TTS')
class GameInput:
    def __init__(self):self.count=0
    def advance(self):self.count+=1
class Guard:
    def observe(self):return FreshSnapshot(ScreenState.DIALOGUE,True,True,True,False,False,True,True,True,False,False)

class OriginApplicationContracts(unittest.TestCase):
    def setUp(self):
        self.ctrl=Controller(Settings(autoplay=True),session_id='s')
        self.input=GameInput()
        self.now=0
        self.rt=Runtime(self.ctrl,Guard(),self.input,SilentTTS(),clock=lambda:self.now)
        self.bound=True
        self.started=[]
        self.app=ObservationOrchestrator(self.rt,on_line_started=lambda *x:self.started.append(x),binding_ok=lambda:self.bound)
    def sample(self,now,state=ScreenState.DIALOGUE,ready=True,positive=True,manual=False):
        self.now=now
        self.app.observe(ScreenSample(now,state,ready,positive,'calibrated-marker'),focused=True,capture_ok=True,manual_intervened=manual)
    def packet(self,n,t,now):
        self.now=now
        self.app.candidate(OriginObservation(n,t,t),now)
    def absent(self,now):
        self.now=now
        line=self.ctrl.line
        self.rt.post(Envelope(EventKind.VOICE_PRESENCE,'s',now,line_epoch=line.epoch,cancel_token=line.token,voice=VoicePresence.ABSENT,evidence=Evidence.METADATA))
        self.rt.step()
    def test_public_ws_text_is_not_authoritative_without_binding_and_screen(self):
        self.bound=False
        self.packet(1,'大和「おはよう」',5)
        self.assertIsNone(self.ctrl.line)
        self.bound=True
        self.packet(2,'大和「おはよう」',10)
        self.assertIsNone(self.ctrl.line)
        self.sample(20,ScreenState.UNKNOWN,False,False)
        self.assertIsNone(self.ctrl.line)
        self.sample(40)
        self.assertEqual((self.ctrl.line.text.speaker,self.ctrl.line.text.body),('大和','おはよう'))
    def test_no_same_text_replay_without_new_verified_ack(self):
        self.packet(1,'同じ',10);self.sample(20)
        self.absent(40)
        self.sample(3000)
        self.assertEqual(self.input.count,1)
        self.assertEqual(self.ctrl.state,ControllerState.WAIT_TRANSITION_ACK)
        self.packet(2,'同じ',3010)
        self.sample(3040)
        self.assertEqual(self.ctrl.line.epoch,1)
        self.sample(3050,ScreenState.TRANSITION,False,False)
        self.packet(3,'同じ',3060)
        self.sample(3080)
        self.assertEqual(self.ctrl.line.epoch,2)
        self.assertEqual(self.input.count,1)
    def test_choice_auto_return_requires_fresh_post_choice_original(self):
        self.packet(1,'選択前',10);self.sample(20)
        self.sample(30,ScreenState.MANUAL_REQUIRED,False,False)
        self.assertEqual(self.ctrl.state,ControllerState.WAIT_MANUAL_CHOICE)
        self.assertTrue(self.ctrl.autoplay_authorized)
        self.packet(2,'古い選択前',40)
        self.sample(50)
        self.assertEqual(self.ctrl.state,ControllerState.WAIT_LINE)
        self.assertIsNone(self.ctrl.line)
        self.packet(3,'選択後',60)
        self.sample(80)
        self.assertEqual(self.ctrl.line.text.body,'選択後')
        self.assertEqual(self.ctrl.line.epoch,2)
    def test_pause_and_disconnect_do_not_auto_return(self):
        self.packet(1,'本文',10);self.sample(20)
        self.app.disconnected(30)
        self.assertEqual(self.ctrl.state,ControllerState.PAUSED)
        self.assertFalse(self.ctrl.autoplay_authorized)
        self.packet(2,'新本文',40);self.sample(50)
        self.assertEqual(self.input.count,0)
        self.assertEqual(self.ctrl.state,ControllerState.PAUSED)
    def test_decoding_is_observation_only(self):
        self.assertEqual(decode_origin_text('大和「こんにちは」'),'大和「こんにちは」')
        self.assertEqual(decode_origin_text('{"text":"本文"}'),'本文')
