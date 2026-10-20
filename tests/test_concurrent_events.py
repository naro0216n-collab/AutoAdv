"""Independent evidence order, latched faults and stale generation rejection."""
import unittest
from autoadv.contracts import Envelope,EventKind as E,ControllerState as S,Evidence
from autoadv.controller import Controller

class ConcurrentEventContracts(unittest.TestCase):
    def setUp(self):
        self.c=Controller(session_id='s')
        self.c.accept(Envelope(E.FOCUS_CHANGED,'s',100,focused=True))
        self.c.accept(Envelope(E.CAPTURE_HEALTH,'s',100,capture_ok=True))
        self.c.accept(Envelope(E.LINE_CANDIDATE,'s',101,source_seq=1,text='本文',text_ready=True))
    def send(self,kind,at,**kwargs):
        l=self.c.line
        return self.c.accept(Envelope(kind,'s',at,line_epoch=l.epoch if l else None,
            cancel_token=l.token if l else None,**kwargs))
    def test_latched_pause_rejects_delayed_gate_and_audio(self):
        self.send(E.MANUAL_OVERRIDE,102)
        self.send(E.ADVANCE_GATE,103,gate_allowed=True,gate_proof_id='x',gate_source='game')
        self.send(E.NATIVE_STARTED,104,native_utterance_id='voice',evidence=Evidence.AUDIO_CONFIRMED)
        self.send(E.TIMER,105)
        self.assertEqual(self.c.state,S.PAUSED)
        self.assertFalse(self.c.gate_allowed)
    def test_old_gate_event_does_not_overwrite_fresher_revocation(self):
        self.send(E.ADVANCE_GATE,200,gate_allowed=False)
        self.send(E.ADVANCE_GATE,150,gate_allowed=True,gate_proof_id='old',gate_source='game')
        self.assertFalse(self.c.gate_allowed)
    def test_cross_session_grant_cannot_unlock(self):
        l=self.c.line
        self.c.accept(Envelope(E.ADVANCE_GATE,'other',200,line_epoch=l.epoch,cancel_token=l.token,
                               gate_allowed=True,gate_proof_id='x',gate_source='game'))
        self.assertFalse(self.c.gate_allowed)
    def test_choice_required_not_suppressed_by_newer_audio_timer(self):
        self.send(E.TIMER,200)
        self.send(E.CHOICE_REQUIRED,150)
        self.assertEqual(self.c.state,S.WAIT_MANUAL_CHOICE)
