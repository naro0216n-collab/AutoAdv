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
    def observe(self): return FreshSnapshot(focused=True,capture_ok=True,native_voice_active=False,tts_playing=False,
        target_window_matches=True,input_available=True,manual_override=not self.allow)


class RuntimeTests(unittest.TestCase):
    def test_tts_only_runtime_never_requires_capture_but_guards_actual_output(self):
        from autoadv.text import GameTextConfig,TextInterpreter
        from autoadv.contracts import TTSPolicy,EffectKind
        class NoAudioGuard:
            def __init__(self):self.tts_playing=True
            def observe(self):
                return FreshSnapshot(focused=True,capture_ok=False,native_voice_active=False,
                    tts_playing=self.tts_playing,target_window_matches=True,
                    input_available=True,manual_override=False)
        ctrl=Controller(Settings(autoplay=True),session_id='s',interpreter=TextInterpreter(
            GameTextConfig(unknown_route=TTSPolicy.SELECTED,unknown_voice='voice')))
        inp=MockInput();guard=NoAudioGuard();tts=MockTTS()
        rt=Runtime(ctrl,guard,inp,tts,clock=lambda:9000)
        def step(kind,at,**data):
            l=ctrl.line
            rt.post(Envelope(kind,'s',at,line_epoch=l.epoch if l else None,
                cancel_token=l.token if l else None,**data));rt.step()
        step(E.FOCUS_CHANGED,100,focused=True)
        step(E.LINE_CANDIDATE,110,source_seq=1,text='本文',text_ready=True,source_line_id='a')
        self.assertEqual([x.kind for x in tts.effects],[EffectKind.START_TTS])
        step(E.TIMER,120)
        self.assertEqual(inp.calls,0)
        # The worker only posts TTS_FINISHED once the sound device has drained.
        guard.tts_playing=False
        step(E.TTS_FINISHED,130,tts_success=True)
        self.assertEqual(inp.calls,1)
        step(E.TIMER,5000)
        self.assertEqual(inp.calls,1)

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

        post(E.VOICE_PRESENCE,105,voice=V.ABSENT,evidence=Evidence.METADATA)
        post(E.TIMER,5000)
        self.assertEqual(inp.calls,0)
        self.assertEqual(ctrl.pause_reason,"advance_preflight_rejected")

    def test_revocation_during_pending_effect_does_not_send_or_latch_failure(self):
        ctrl=Controller(Settings(autoplay=True),session_id='s')
        inp=MockInput()
        def audit(kind, data):
            if kind=='effect' and data['kind']=='REQUEST_ADVANCE':
                ctrl.pause()
        runner=Runtime(ctrl,MockGuard(),inp,MockTTS(),clock=lambda:9000,audit=audit)
        seq=[(E.FOCUS_CHANGED,101,{'focused':True}),
             (E.CAPTURE_HEALTH,102,{'capture_ok':True}),
             (E.LINE_CANDIDATE,103,{'source_seq':1,'text':'あ',
                                     'text_ready':True,'source_line_id':'a'}),
             (E.VOICE_PRESENCE,105,{'voice':V.ABSENT,'evidence':Evidence.METADATA}),
             (E.TIMER,5000,{})]
        for kind,at,detail in seq:
            line=ctrl.line
            runner.post(Envelope(kind,'s',at,
                line_epoch=line.epoch if line else None,
                cancel_token=line.token if line else None,**detail))
            runner.step()
        self.assertEqual(inp.calls,0)
        self.assertFalse(ctrl.autoplay_authorized)
        self.assertNotEqual(ctrl.state.name,'PAUSED')
        ctrl.authorize()
        line=ctrl.line
        runner.post(Envelope(E.TIMER,'s',6000,line_epoch=line.epoch,cancel_token=line.token))
        runner.step()
        # Explicit pause can be undone, but this audit callback pauses again.
        self.assertEqual(inp.calls,0)

    def test_input_only_once(self):
        ctrl=Controller(Settings(autoplay=True),session_id="s")
        inp=MockInput();runner=Runtime(ctrl,MockGuard(),inp,MockTTS(),clock=lambda:9000)
        seq=[(E.FOCUS_CHANGED,101,{"focused":True}),
             (E.CAPTURE_HEALTH,102,{"capture_ok":True}),
             (E.LINE_CANDIDATE,103,{"source_seq":1,"text":"あ","text_ready":True,"source_line_id":"a"}),

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