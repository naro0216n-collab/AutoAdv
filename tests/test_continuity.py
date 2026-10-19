"""Line-ownership and late-callback invariants across the application boundary."""
import unittest
from autoadv.controller import Controller
from autoadv.contracts import Envelope,EventKind as E,Evidence as V,ScreenState as S,VoicePresence as P,ControllerState as CS

class Continuity(unittest.TestCase):
    def test_native_late_after_irreversible_input_is_explicit_incident(self):
        c=Controller(session_id='s')
        def send(k,at,**kwargs):
            l=c.line
            return c.accept(Envelope(k,'s',at,line_epoch=l.epoch if l else None,cancel_token=l.token if l else None,**kwargs))
        send(E.SCREEN_CHANGED,1,screen=S.DIALOGUE)
        send(E.FOCUS_CHANGED,2,focused=True)
        send(E.CAPTURE_HEALTH,3,capture_ok=True)
        send(E.LINE_CANDIDATE,4,text='あ',source_seq=1,source_line_id='A',text_ready=True)
        send(E.VOICE_PRESENCE,5,voice=P.ABSENT,evidence=V.METADATA)
        pending=send(E.TIMER,3000)[0]
        self.assertTrue(c.confirm_advance(line_epoch=pending.line_epoch,cancel_token=pending.cancel_token,
                          fresh_screen=S.DIALOGUE,fresh_focused=True,fresh_text_ready=True,fresh_capture_ok=True,
                          fresh_native_voice_active=False,fresh_tts_playing=False,fresh_line_matches=True,
                          fresh_target_window_matches=True,fresh_input_available=True,
                          fresh_manual_override=False,fresh_transition_pending=False,at_ms=3001))
        send(E.NATIVE_STARTED,3100,native_utterance_id='voice',evidence=V.AUDIO_CONFIRMED)
        self.assertEqual(c.state,CS.PAUSED)
        self.assertEqual(c.pause_reason,'native_started_after_advance')

    def test_transition_screen_does_not_resend(self):
        c=Controller(session_id='s')
        c.state=CS.WAIT_TRANSITION_ACK
        c.line=__import__('autoadv.controller',fromlist=['Line']).Line(1,1,__import__('autoadv.text',fromlist=['normalize']).normalize('あ'),'A',0,1)
        e=Envelope(E.SCREEN_CHANGED,'s',1,screen=S.TRANSITION)
        self.assertEqual(c.accept(e),[])
        self.assertEqual(c.state,CS.WAIT_TRANSITION_ACK)

if __name__=='__main__':unittest.main()
