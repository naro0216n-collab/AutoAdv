"""Domain invariants for independent observer clocks and terminal stopping."""
import unittest
from autoadv.contracts import Envelope, EventKind as E, ScreenState as S, ControllerState as CS, Evidence
from autoadv.controller import Controller


class ConcurrentEventContracts(unittest.TestCase):
    def setUp(self):
        self.c = Controller(session_id='s')
        self.c.accept(Envelope(E.SCREEN_CHANGED,'s',100,screen=S.DIALOGUE))
        self.c.accept(Envelope(E.FOCUS_CHANGED,'s',100,focused=True))
        self.c.accept(Envelope(E.CAPTURE_HEALTH,'s',100,capture_ok=True))
        self.c.accept(Envelope(E.LINE_CANDIDATE,'s',101,source_seq=1,source_line_id='a',text='おはよう',text_ready=True))

    def event(self, kind, at, **kwargs):
        line=self.c.line
        return self.c.accept(Envelope(kind,'s',at,line_epoch=line.epoch,cancel_token=line.token,**kwargs))

    def test_paused_is_latched_against_native_events_and_ready_timers(self):
        self.c.accept(Envelope(E.SCREEN_CHANGED,'s',102,screen=S.UNKNOWN))
        self.assertEqual(self.c.state,CS.PAUSED)
        for kind, extra in [
            (E.NATIVE_STARTED,{'native_utterance_id':'voice','evidence':Evidence.AUDIO_CONFIRMED}),
            (E.NATIVE_COMPLETED,{'native_utterance_id':'voice','evidence':Evidence.AUDIO_CONFIRMED}),
            (E.VOICE_PRESENCE,{'evidence':Evidence.METADATA}),
            (E.TIMER,{}),
            (E.LINE_READY,{})]:
            self.assertEqual(self.event(kind,103,**extra),[])
            self.assertEqual(self.c.state,CS.PAUSED)
        self.assertEqual(self.c.pause_reason,'screen_unknown')

    def test_delayed_choice_observation_not_discarded_by_newer_timer(self):
        self.event(E.TIMER,200)
        self.c.accept(Envelope(E.SCREEN_CHANGED,'s',150,screen=S.UNKNOWN))
        self.assertEqual(self.c.state,CS.PAUSED)
        self.assertEqual(self.c.screen,S.UNKNOWN)

    def test_old_screen_observation_same_source_is_not_resurrected(self):
        self.c.accept(Envelope(E.SCREEN_CHANGED,'s',200,screen=S.UNKNOWN))
        self.c.accept(Envelope(E.SCREEN_CHANGED,'s',150,screen=S.DIALOGUE))
        self.assertEqual(self.c.screen,S.UNKNOWN)
        self.assertEqual(self.c.state,CS.PAUSED)

if __name__=='__main__':unittest.main()
