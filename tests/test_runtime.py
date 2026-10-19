import unittest
from autoadv.runtime import Runtime, FreshSnapshot
from autoadv.contracts import Envelope, EventKind as E, Evidence, VoicePresence as V, ScreenState as SS
from autoadv.controller import Controller


class MockTTS:
    def __init__(self): self.effects=[]
    def apply(self,e): self.effects.append(e)


class MockInput:
    def __init__(self): self.calls=0
    def advance(self): self.calls+=1


class MockGuard:
    def __init__(self, screen=SS.DIALOGUE): self.screen=screen
    def observe(self): return FreshSnapshot(self.screen,True,True,True,False,False,True,True,True,False,False)


class RuntimeTests(unittest.TestCase):
    def test_guard_prevents_input(self):
        ctrl=Controller(session_id="s")
        guard=MockGuard(SS.CHOICE); inp=MockInput(); clock=iter(range(10000,20000))
        runner=Runtime(ctrl,guard,inp,MockTTS(),clock=lambda:next(clock))
        def post(k,at,**kwargs):
            l=ctrl.line
            runner.post(Envelope(k,"s",at,line_epoch=l.epoch if l else None,
                        cancel_token=l.token if l else None,**kwargs));runner.step()
        post(E.SCREEN_CHANGED,100,screen=SS.DIALOGUE)
        post(E.FOCUS_CHANGED,101,focused=True)
        post(E.CAPTURE_HEALTH,102,capture_ok=True)
        post(E.LINE_CANDIDATE,103,source_seq=1,text="あ",text_ready=True,source_line_id="a")
        post(E.VOICE_PRESENCE,104,voice=V.ABSENT,evidence=Evidence.METADATA)
        post(E.TIMER,5000)
        self.assertEqual(inp.calls,0)
        self.assertEqual(ctrl.pause_reason,"advance_preflight_rejected")

    def test_input_only_once(self):
        ctrl=Controller(session_id="s")
        inp=MockInput();runner=Runtime(ctrl,MockGuard(),inp,MockTTS(),clock=lambda:9000)
        seq=[(E.SCREEN_CHANGED,100,{"screen":SS.DIALOGUE}),
             (E.FOCUS_CHANGED,101,{"focused":True}),
             (E.CAPTURE_HEALTH,102,{"capture_ok":True}),
             (E.LINE_CANDIDATE,103,{"source_seq":1,"text":"あ","text_ready":True,"source_line_id":"a"}),
             (E.VOICE_PRESENCE,104,{"voice":V.ABSENT,"evidence":Evidence.METADATA}),
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