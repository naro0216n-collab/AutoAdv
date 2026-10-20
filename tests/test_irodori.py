import io
import json
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from autoadv.irodori import IrodoriClient, IrodoriConfig, TTSWorker
from autoadv.contracts import Effect, EffectKind, EventKind


def make_wav():
    mem=io.BytesIO()
    with wave.open(mem,"wb") as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000)
        w.writeframes(b"\x00\x00"*160)
    return mem.getvalue()


class Handler(BaseHTTPRequestHandler):
    received=None
    def do_POST(self):
        body=self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received=json.loads(body)
        response=make_wav()
        self.send_response(200)
        self.send_header("Content-Type","audio/wav")
        self.send_header("Content-Length",str(len(response)))
        self.end_headers();self.wfile.write(response)
    def log_message(self,*args):pass


class IrodoriTests(unittest.TestCase):
    def test_http_request_contract_and_wav(self):
        server=HTTPServer(("127.0.0.1",0),Handler)
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        try:
            cfg=IrodoriConfig(url=f"http://127.0.0.1:{server.server_port}/v1/audio/speech")
            data=IrodoriClient(cfg).synthesize("おはよう","v1")
            self.assertEqual(data,make_wav())
            self.assertEqual(Handler.received["voice"],"v1")
            self.assertEqual(Handler.received["model"],"irodori-tts")
            self.assertEqual(Handler.received["response_format"],"wav")
        finally:
            server.shutdown();server.server_close();t.join()

    def test_no_tts_success_before_device_drain(self):
        entered=threading.Event();release=threading.Event();done=threading.Event()
        received=[]
        class Client:
            def synthesize(self, text, voice):return make_wav()
        def player(audio,cancel):
            entered.set();release.wait(5)
            return not cancel.is_set()
        def emit(event):
            received.append(event);done.set()
        w=TTSWorker(Client(),emit,player=player,monotonic_ms=lambda:1000)
        w.apply(Effect(EffectKind.START_TTS,"s",1,1,text="x",voice_id="v1"))
        self.assertTrue(entered.wait(2))
        self.assertEqual(received,[])
        release.set();self.assertTrue(done.wait(2))
        self.assertEqual(received[0].kind,EventKind.TTS_FINISHED)
        self.assertTrue(received[0].tts_success)

    def test_cancelled_http_generation_does_not_block_native_end(self):
        started=threading.Event();release=threading.Event();done=threading.Event()
        played=[];out=[]
        class WaitingHTTP:
            def synthesize(self,text,voice):
                started.set();release.wait(5);done.set();return make_wav()
        w=TTSWorker(WaitingHTTP(),out.append,player=lambda *_:played.append(True))
        w.apply(Effect(EffectKind.START_TTS,'s',1,1,text='x',voice_id='voice'))
        self.assertTrue(started.wait(2))
        self.assertTrue(w.is_active())
        w.apply(Effect(EffectKind.CANCEL_TTS,'s',1,1))
        self.assertFalse(w.is_active())
        release.set();self.assertTrue(done.wait(2))
        self.assertEqual(played,[])
        self.assertEqual(out,[])

    def test_cancelled_playback_blocks_until_device_aborted(self):
        started=threading.Event();release=threading.Event();done=threading.Event()
        events=[]
        class Client:
            def synthesize(self,text,voice):return make_wav()
        def player(audio,cancel):
            started.set();release.wait(5);done.set();return not cancel.is_set()
        w=TTSWorker(Client(),events.append,player=player)
        w.apply(Effect(EffectKind.START_TTS,'s',2,2,text='y',voice_id='voice'))
        self.assertTrue(started.wait(2))
        w.apply(Effect(EffectKind.CANCEL_TTS,'s',2,2))
        self.assertTrue(w.is_active())
        release.set();self.assertTrue(done.wait(2))
        # Observe removal via worker lock so no timing sleep guesses.
        for _ in range(10000):
            with w._lock:
                if not w._jobs:break
            threading.Event().wait(.0002)
        self.assertFalse(w.is_active())
        self.assertEqual(events,[])

    def test_cancelled_synthesis_allows_verified_native_end_in_runtime(self):
        from autoadv.controller import Controller,Settings
        from autoadv.text import TextInterpreter,GameTextConfig
        from autoadv.contracts import Envelope, Evidence, TTSPolicy, VoicePresence
        from autoadv.runtime import Runtime, FreshSnapshot
        started=threading.Event();release=threading.Event();finished=threading.Event()
        played=[]
        class BlockingClient:
            def synthesize(self,text,voice):
                started.set();release.wait(5);finished.set();return make_wav()
        class Input:
            def __init__(self):self.calls=0
            def advance(self):self.calls+=1
        class Guard:
            def __init__(self,worker):self.worker=worker
            def observe(self):
                return FreshSnapshot(focused=True,capture_ok=True,native_voice_active=False,tts_playing=self.worker.is_active(),target_window_matches=True,input_available=True,manual_override=False)
        ctrl=Controller(Settings(autoplay=True),session_id='s',interpreter=TextInterpreter(
            GameTextConfig(unknown_route=TTSPolicy.AUTO,unknown_voice='voice')))
        input_driver=Input()
        # The actual TTS worker is used, not a fake cancellation protocol.
        worker=TTSWorker(BlockingClient(),lambda e:None,
                         player=lambda *_:played.append(True))
        rt=Runtime(ctrl,Guard(worker),input_driver,worker,clock=lambda:4000)
        def step(kind,at,**fields):
            line=ctrl.line
            rt.post(Envelope(kind,'s',at,line_epoch=line.epoch if line else None,
                             cancel_token=line.token if line else None,**fields))
            rt.step()
        step(EventKind.FOCUS_CHANGED,101,focused=True)
        step(EventKind.CAPTURE_HEALTH,102,capture_ok=True)
        step(EventKind.LINE_CANDIDATE,103,source_seq=1,source_line_id='a',
             text='おはよう',text_ready=True)

        step(EventKind.VOICE_PRESENCE,105,voice=VoicePresence.ABSENT,
             evidence=Evidence.METADATA)
        self.assertTrue(started.wait(2))
        self.assertTrue(worker.is_active())
        step(EventKind.NATIVE_STARTED,200,evidence=Evidence.AUDIO_CONFIRMED,
             native_utterance_id='native-1')
        self.assertFalse(worker.is_active())
        step(EventKind.NATIVE_COMPLETED,300,evidence=Evidence.AUDIO_CONFIRMED,
             native_utterance_id='native-1')
        self.assertEqual(input_driver.calls,1)
        release.set();self.assertTrue(finished.wait(2))
        self.assertEqual(played,[])

    def test_native_interrupt_discards_tts_done(self):
        entered=threading.Event();release=threading.Event();exited=threading.Event()
        received=[]
        class Client:
            def synthesize(self,text,voice):return make_wav()
        def player(audio,cancel):
            entered.set();release.wait(5)
            return not cancel.is_set()
        w=TTSWorker(Client(),received.append,player=player)
        effect=Effect(EffectKind.START_TTS,"s",1,1,text="x",voice_id="v1")
        w.apply(effect)
        self.assertTrue(entered.wait(2))
        w.apply(Effect(EffectKind.CANCEL_TTS,"s",1,1))
        release.set()
        # Avoid arbitrary sleep: observe worker cleanup via the worker lock.
        for _ in range(10000):
            with w._lock:
                if not w._jobs:break
            threading.Event().wait(.0002)
        self.assertEqual(received,[])

if __name__=="__main__":unittest.main()