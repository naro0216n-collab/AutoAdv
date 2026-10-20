import tempfile
import json
from pathlib import Path
from unittest import TestCase
from autoadv.services import AutoAdvService
from autoadv.text import GameTextConfig

class PersistenceTests(TestCase):
    def test_profile_roundtrip_same_interpreter(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'settings.json'
            svc=AutoAdvService(settings_file=path)
            svc.save_game('game',GameTextConfig(name_readings={'京':'みやこ'}))
            from_disk=AutoAdvService(settings_file=path)
            self.assertFalse(from_disk.autoplay_allowed)
            self.assertEqual(from_disk.preview('大和「京(みやこ)」').tts_text,'みやこ')
            self.assertEqual(from_disk.preview('大和「京(みやこ)」'),svc.preview('大和「京(みやこ)」'))
    def test_no_partial_write_when_profile_is_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'settings.json'
            p.write_text('{"games":{"bad":{"structure_rules":[{"rule_id":"a","pattern":"["}]}}}')
            with self.assertRaises(ValueError):AutoAdvService(settings_file=p)

class MonitorProfilePersistence(TestCase):
    def profile(self,root):
        for filename in ('loopback.dll','model.onnx','tokens.txt','dialogue.png','ready.png'):
            (root/filename).write_bytes(b'sample')
        live={
            'pid':123,'hwnd':456,'loopback_helper':str(root/'loopback.dll'),
            'asr_model':str(root/'model.onnx'),'asr_tokens':str(root/'tokens.txt'),
            'grace_ms':500,'allow_heuristic_absence':False,'log_path':str(root/'log.jsonl'),
            'tts_speed':1.0,'silent_dwell':{'base_ms':700,'min_ms':1000,'reading_chars_per_sec':12},
            'tts_required':False,'advance_input':{'mode':'keyboard','virtual_key':13},
        }
        screen={'markers':{'dialogue':{'image':str(root/'dialogue.png'),'rect':[0,0,40,20],'min_score':.96},
                           'text_ready':{'image':str(root/'ready.png'),'rect':[50,0,40,20],'min_score':.96}},
                'minimum_ready_observations':2,'minimum_ready_span_ms':120}
        return live,screen

    def test_game_monitor_settings_are_atomic_and_use_runtime_schema(self):
        from autoadv.live import LiveConfig
        from autoadv.screen import ScreenProfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'settings.json'
            svc=AutoAdvService(settings_file=path)
            live,screen=self.profile(root)
            configured=svc.save_monitor('つよきす',live,screen)
            same=AutoAdvService(settings_file=path)
            self.assertEqual(same.monitor_path('つよきす'),configured)
            loaded=LiveConfig.load(configured)
            self.assertEqual(loaded.pid,123)
            self.assertEqual(len(ScreenProfile.load(loaded.screen_profile).markers),2)
            raw,observed=same.monitor_document('つよきす')
            self.assertEqual(raw['advance_input']['virtual_key'],13)
            self.assertEqual(observed['markers']['dialogue']['rect'],[0,0,40,20])
            invalid={**screen,'markers':{'dialogue':{'image':'missing.png','rect':[0,0,40,20]}}}
            with self.assertRaises(ValueError):same.save_monitor('つよきす',live,invalid)
            self.assertEqual(AutoAdvService(settings_file=path).monitor_path('つよきす'),configured)
            self.assertEqual(LiveConfig.load(configured).pid,123)
            other=same.save_monitor('まじこい',{**live,'pid':789},screen)
            self.assertNotEqual(configured,other)
            self.assertEqual(LiveConfig.load(AutoAdvService(settings_file=path).monitor_path('つよきす')).pid,123)
            self.assertEqual(LiveConfig.load(other).pid,789)

    def test_explicit_import_only_legacy_path_never_becomes_runtime_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'settings.json'
            p.write_text(json.dumps({'selected_game':'game','live_config':'legacy.json'}))
            svc=AutoAdvService(settings_file=p)
            self.assertIsNone(svc.monitor_path('game'))
            live,screen=self.profile(Path(tmp))
            svc.save_monitor('game',live,screen)
            after=AutoAdvService(settings_file=p)
            self.assertEqual(after.preferences.live_config,'')
            self.assertIsNotNone(after.monitor_path('game'))
