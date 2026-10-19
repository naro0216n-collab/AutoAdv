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