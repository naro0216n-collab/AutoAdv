import unittest
from autoadv.runtime import Runtime, FreshSnapshot
from autoadv.contracts import Envelope, EventKind as E, Evidence, VoicePresence as V
from autoadv.controller import Controller,Settings


class MockTTS:
    def __init__(self): self.effects=[]
    def apply(self,e): self.effects.append(e)


class MockInput:
    def __init__(self): self.calls=0
    def advance(self): self.calls+=1


class MockGuard:
    def __init__(self, allow=True): self.allow=allow
    def observe(self): return FreshSnapshot(focused=True,capture_ok=True,gate_allowed=self.allow,
        gate_proof_id='verified-gate' if self.allow else None,native_voice_active=False,tts_playing=False,
        target_window_matches=True,input_available=True,manual_override=False,transition_pending=False)


class RuntimeTests(unittest.TestCase):
    def test_guard_prevents_input(self):
        ctrl=Controller(Settings(autoplay=True),session_id="s")
        guard=MockGuard(False); inp=MockInput(); clock=iter(range(10000,20000))
        runner=Runtime(ctrl,guard,inp,MockTTS(),clock=lambda:next(clock))
        def post(k,at,**kwargs):
            l=ctrl.line
            runner.post(Envelope(k,"s",at,line_epoch=l.epoch if l else None,
                        cancel_token=l.token if l else None,**kwargs));runner.step()
        post(E.FOCUS_CHANGED,101,focused=True)
        post(E.CAPTURE_HEALTH,102,capture_ok=True)
        post(E.LINE_CANDIDATE,103,source_seq=1,text="あ",text_ready=True,source_line_id="a")
        post(E.ADVANCE_GATE,104,gate_allowed=True,gate_proof_id="verified-gate",gate_source="game-state")
        post(E.VOICE_PRESENCE,105,voice=V.ABSENT,evidence=Evidence.METADATA)
        post(E.TIMER,5000)
        self.assertEqual(inp.calls,0)
        self.assertEqual(ctrl.pause_reason,"advance_preflight_rejected")

    def test_input_only_once(self):
        ctrl=Controller(Settings(autoplay=True),session_id="s")
        inp=MockInput();runner=Runtime(ctrl,MockGuard(),inp,MockTTS(),clock=lambda:9000)
        seq=[(E.FOCUS_CHANGED,101,{"focused":True}),
             (E.CAPTURE_HEALTH,102,{"capture_ok":True}),
             (E.LINE_CANDIDATE,103,{"source_seq":1,"text":"あ","text_ready":True,"source_line_id":"a"}),
             (E.ADVANCE_GATE,104,{"gate_allowed":True,"gate_proof_id":"verified-gate","gate_source":"game-state"}),
             (E.VOICE_PRESENCE,105,{"voice":V.ABSENT,"evidence":Evidence.METADATA}),
             (E.TIMER,5000,{})]
        for k,at,details in seq:
            l=ctrl.line
            runner.post(Envelope(k,"s",at,line_epoch=l.epoch if l else None,
                                 cancel_token=l.token if l else None,**details));runner.step()
        self.assertEqual(inp.calls,1)
        l=ctrl.line
        runner.post(Envelope(E.TIMER,"s",6000,line_epoch=l.epoch,cancel_token=l.token));runner.step()
        self.assertEqual(inp.calls,1)

if __name__ == "__main__":unittest.main()