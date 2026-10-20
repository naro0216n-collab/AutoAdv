"""Line summaries must be derivable without saving voice or screenshots."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from autoadv.live import JsonAudit,LiveConfig
from autoadv.contracts import Envelope,EventKind as E,ScreenState as SS,Evidence,VoicePresence
from autoadv.controller import Controller,Settings
from autoadv.runtime import Runtime,FreshSnapshot

class FakeInput:
    def __init__(self): self.count=0
    def advance(self): self.count+=1
class FakeGuard:
    def observe(self):return FreshSnapshot(focused=True,capture_ok=True,gate_allowed=True,gate_proof_id='gate-A',native_voice_active=False,tts_playing=False,target_window_matches=True,input_available=True,manual_override=False,transition_pending=False)
class FakeTTS:
    def apply(self,x):raise AssertionError('no TTS expected')

class TelemetryContracts(unittest.TestCase):
    def test_summary_has_hash_timestamps_and_asr_metrics(self):
        with TemporaryDirectory() as td:
            sink=JsonAudit(Path(td)/'result.jsonl',context={'asr_model_id':'model.int8.onnx'})
            ctrl=Controller(Settings(autoplay=True),session_id='s')
            clock=[1]
            driver=FakeInput()
            rt=Runtime(ctrl,FakeGuard(),driver,FakeTTS(),clock=lambda:clock[0],audit=sink)
            def step(kind,at,**fields):
                line=ctrl.line
                rt.post(Envelope(kind,'s',at,line_epoch=line.epoch if line else None,
                                  cancel_token=line.token if line else None,**fields))
                rt.step()
            step(E.SCREEN_CHANGED,1,screen=SS.DIALOGUE)
            step(E.FOCUS_CHANGED,2,focused=True)
            step(E.CAPTURE_HEALTH,3,capture_ok=True)
            step(E.LINE_CANDIDATE,4,source_seq=1,source_line_id='a',text='おはよう',text_ready=True)
            step(E.ADVANCE_GATE,5,gate_allowed=True,gate_proof_id="gate-A",gate_source="game")
            step(E.VOICE_PRESENCE,6,voice=VoicePresence.ABSENT,evidence=Evidence.METADATA)
            sink('asr_progress',{'session_id':'s','line_epoch':1,'asr_coverage':.8,'asr_tail_confidence':.2})
            clock[0]=3001
            step(E.TIMER,3000)
            self.assertEqual(driver.count,1)
            step(E.TRANSITION_ACK,3100,transition_id='new-line')
            rt.finalize()
            sink.close()
            rows=[json.loads(x) for x in (Path(td)/'result.jsonl').read_text().splitlines()]
            summary=[x for x in rows if x['name']=='line_summary']
            self.assertEqual(len(summary),1)
            self.assertEqual(summary[0]['final_status'],'advanced_and_acknowledged')
            self.assertEqual(summary[0]['transition_ack'],'new-line')
            self.assertEqual(summary[0]['asr_coverage'],.8)
            self.assertEqual(summary[0]['asr_model_id'],'model.int8.onnx')
            self.assertEqual(summary[0]['input_at_ms'],3001)
            self.assertEqual(len(summary[0]['raw_sha256']),64)
            self.assertNotIn('おはよう',str(rows))

    def test_live_config_requires_explicit_game_input_profile(self):
        data=Path('examples/live-profile.example.json').read_text(encoding='utf-8')
        with TemporaryDirectory() as td:
            p=Path(td)/'profile.json'
            p.write_text(data,encoding='utf-8')
            cfg=LiveConfig.load(p)
            self.assertEqual((cfg.input_mode,cfg.input_virtual_key),('keyboard',13))
            profile=json.loads(data)
            profile.pop('advance_input')
            p.write_text(json.dumps(profile),encoding='utf-8')
            with self.assertRaises(ValueError):LiveConfig.load(p)
            valid=json.loads(data)
            for field,value in [('tts_required','false'),('pid',True),('tts_speed',-1)]:
                altered={**valid,field:value}
                p.write_text(json.dumps(altered),encoding='utf-8')
                with self.subTest(field=field),self.assertRaises(ValueError):
                    LiveConfig.load(p)
            invalid={**valid,'silent_dwell':{'base_ms':-1,'min_ms':1000,'reading_chars_per_sec':12}}
            p.write_text(json.dumps(invalid),encoding='utf-8')
            with self.assertRaises(ValueError):LiveConfig.load(p)

if __name__=='__main__':unittest.main()
