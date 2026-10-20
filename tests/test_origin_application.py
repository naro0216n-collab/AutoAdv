"""HOOK/OCR output may drive Utterance/audio; never infer permission or ACK."""
import unittest
from autoadv.application import ObservationOrchestrator
from autoadv.controller import Controller,Settings
from autoadv.contracts import ControllerState as S,EventKind as E,Envelope,VoicePresence,Evidence
from autoadv.observation import OriginObservation
from autoadv.progression import GateObservation,GateState
from autoadv.runtime import Runtime,FreshSnapshot

class NullTTS:
    def apply(self,e):pass
class Input:
    def __init__(self):self.count=0
    def advance(self):self.count+=1
class GateGuard:
    def __init__(self):self.allow=False
    def observe(self):return FreshSnapshot(focused=True,capture_ok=True,
        gate_allowed=self.allow,gate_proof_id='gate-A' if self.allow else None,
        native_voice_active=False,tts_playing=False,target_window_matches=True,
        input_available=True,manual_override=False,transition_pending=False)

class OriginApplicationContracts(unittest.TestCase):
    def setUp(self):
        self.now=0;self.ctrl=Controller(Settings(autoplay=True),session_id='s')
        self.input=Input();self.guard=GateGuard()
        self.rt=Runtime(self.ctrl,self.guard,self.input,NullTTS(),clock=lambda:self.now)
        self.bound=True
        self.started=[]
        self.app=ObservationOrchestrator(self.rt,binding_ok=lambda:self.bound,
            on_line_started=lambda *x:self.started.append(x),stable_ms=200)
    def packet(self,n,text,now):
        self.now=now;self.app.candidate(OriginObservation(n,text,text,kind='hook',origin='p:1'),now)
    def tick(self,now,gate=None):
        self.now=now;self.app.tick(now,focused=True,capture_ok=True,gate=gate)
    def absent(self,now):
        self.now=now;line=self.ctrl.line
        self.rt.post(Envelope(E.VOICE_PRESENCE,'s',now,line_epoch=line.epoch,
                             cancel_token=line.token,voice=VoicePresence.ABSENT,evidence=Evidence.METADATA));self.rt.step()
    def test_hook_without_image_calibration_produces_presentation_line(self):
        self.packet(1,'大和「おはよう」',10)
        self.tick(50)
        self.assertEqual(self.ctrl.line.text.body,'おはよう')
        self.assertIsNone(self.ctrl.line.ready_ms)
        self.tick(250)
        self.assertIsNotNone(self.ctrl.line.ready_ms)
        self.absent(260);self.tick(3000)
        self.assertEqual(self.input.count,0)
        self.assertFalse(self.ctrl.gate_allowed)
    def test_cannot_bind_unselected_source(self):
        self.bound=False;self.packet(1,'本文',10);self.tick(50)
        self.assertIsNone(self.ctrl.line)
    def test_explicit_gate_is_separate_from_text_and_input_is_once(self):
        self.packet(1,'本文',10);self.tick(240);self.absent(250)
        self.guard.allow=True
        proof=GateObservation(3000,GateState.ALLOW,'gate-A','verified-game-provider')
        self.tick(3000,proof)
        self.assertEqual(self.input.count,1)
        self.assertEqual(self.ctrl.state,S.WAIT_TRANSITION_ACK)
        self.packet(2,'新しい本文',3010);self.tick(4000,proof)
        self.assertEqual(self.input.count,1)
        self.assertEqual(self.ctrl.line.epoch,1)
    def test_only_independent_ack_releases_line(self):
        self.packet(1,'本文',10);self.tick(240);self.absent(250)
        self.guard.allow=True
        proof=GateObservation(3000,GateState.ALLOW,'gate-A','verified-game-provider')
        self.tick(3000,proof)
        self.packet(2,'新しい本文',3010);self.tick(3200,proof)
        self.assertEqual(self.ctrl.state,S.WAIT_TRANSITION_ACK)
        self.app.acknowledge_transition(3210,independent_proof_id='scene-2',source='game-native-event')
        self.assertEqual(self.ctrl.state,S.WAIT_LINE)
        self.packet(3,'新しい本文',3220);self.tick(3240)
        self.assertEqual(self.ctrl.line.epoch,2)
    def test_choice_latches_without_new_positive_resolution(self):
        self.packet(1,'本文',10);self.tick(20)
        self.tick(30,GateObservation(30,GateState.MANUAL_REQUIRED,'choice','game-hook'))
        self.assertEqual(self.ctrl.state,S.WAIT_MANUAL_CHOICE)
        self.packet(2,'メニューの文字',40);self.tick(200)
        self.assertIsNone(self.ctrl.line)
        self.app.choice_resolved(220,proof_id='choice-exited',source='native-state',manual_intervened=False)
        self.assertEqual(self.ctrl.state,S.WAIT_MANUAL_CHOICE)
        self.app.choice_resolved(230,proof_id='choice-exited',source='native-state',manual_intervened=True)
        self.assertEqual(self.ctrl.state,S.WAIT_LINE)
    def test_disconnect_invalidates_and_cannot_resume(self):
        self.packet(1,'本文',10);self.tick(20);self.app.disconnected(30)
        self.assertEqual(self.ctrl.state,S.PAUSED)
        self.packet(2,'本文2',40);self.tick(240)
        self.assertEqual(self.ctrl.state,S.PAUSED)
    def test_same_partial_after_ready_is_not_reinterpreted(self):
        self.packet(1,'こん',10);self.tick(20)
        self.packet(2,'こんにちは',30);self.tick(250)
        self.assertEqual(self.ctrl.line.text.body,'こんにちは')
        self.packet(3,'こんばんは',270)
        self.assertEqual(self.ctrl.state,S.PAUSED)
