"""Persistence contracts for one catalog and ephemeral runtime bindings."""
import tempfile
import json
from pathlib import Path
from unittest import TestCase

from autoadv.preferences import Preferences,LegacyPreferencesDetected
from autoadv.services import AutoAdvService
from autoadv.text import GameTextConfig
from autoadv.live import LiveConfig


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
            self.assertEqual(json.loads(path.read_text())['schema_version'],2)

    def test_legacy_is_not_silently_accepted_as_new_preferences(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'settings.json'
            original=b'{"games":{"bad":{"structure_rules":[{"rule_id":"a","pattern":"["}]}}}'
            p.write_bytes(original)
            with self.assertRaises(LegacyPreferencesDetected):AutoAdvService(settings_file=p)
            with self.assertRaises(ValueError):Preferences.import_legacy(p)
            self.assertEqual(p.read_bytes(),original)
            self.assertFalse(list(p.parent.glob('*.bak')))


class MonitorProfilePersistence(TestCase):
    @staticmethod
    def profile(root):
        for filename in ('loopback.dll','model.onnx','tokens.txt'):
            (root/filename).write_bytes(b'sample')
        live={
            'pid':123,'hwnd':456,'loopback_helper':str(root/'loopback.dll'),
            'asr_model':str(root/'model.onnx'),'asr_tokens':str(root/'tokens.txt'),
            'grace_ms':500,'allow_heuristic_absence':False,'log_path':str(root/'log.jsonl'),
            'tts_speed':1.0,'silent_dwell':{'base_ms':700,'min_ms':1000,'reading_chars_per_sec':12},
            'tts_required':False,'advance_input':{'mode':'keyboard','virtual_key':13},
        }
        screen={}  # Default HOOK monitoring must not require image calibration.
        return live,screen

    def test_monitor_is_atomic_in_single_profile_not_secondary_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'settings.json'
            svc=AutoAdvService(settings_file=path)
            svc.new_game('つよきす')
            live,screen=self.profile(root)
            svc.save_monitor(live,screen)
            doc=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(set(doc),{'schema_version','irodori_root','irodori_url','luna_host_dll','globals','catalog'})
            self.assertNotIn('live_profiles',doc)
            current=AutoAdvService(settings_file=path)
            observed,markers=current.monitor_document()
            self.assertNotIn('pid',observed)
            self.assertNotIn('hwnd',observed)
            self.assertNotIn('screen_profile',observed)
            self.assertEqual(observed['advance_input']['virtual_key'],13)
            self.assertEqual(markers,{})
            configured,temporary=current.runtime_snapshot(999,888)
            try:
                self.assertEqual((configured.pid,configured.hwnd),(999,888))
                self.assertIsNone(configured.screen_profile)
                self.assertEqual(LiveConfig.load(Path(temporary.name)/'monitor.json'),configured)
            finally:
                temporary.cleanup()
            self.assertFalse(Path(temporary.name).exists())
            before=path.read_bytes()
            invalid={'markers':{'advance_allowed':{'image':'missing.png','rect':[0,0,40,20]}}}
            with self.assertRaises(ValueError):current.save_monitor(live,invalid)
            self.assertEqual(path.read_bytes(),before)
            duplicated=current.profile_duplicate('別設定')
            self.assertEqual(duplicated.monitor,observed)
            self.assertEqual(duplicated.screen,markers)
            current.profile_set_default()
            self.assertEqual(AutoAdvService(settings_file=path).active()[1].profile_id,duplicated.profile_id)

    def test_explicit_legacy_import_keeps_original_and_never_persists_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'settings.json'
            live,screen=self.profile(root)
            # Explicit migration of old AutoAdv files must preserve the original
            # screen document in the backup but NEVER imply input permission.
            old_screen={'markers':{
                'dialogue':{'image':'old-dialogue.png','rect':[0,0,40,20]},
                'text_ready':{'image':'old-ready.png','rect':[50,0,40,20]}}}
            screen_path=root/'old-screen.json';screen_path.write_text(json.dumps(old_screen))
            live['screen_profile']=str(screen_path)
            live_path=root/'old-live.json';live_path.write_text(json.dumps(live))
            raw={'selected_game':'game','games':{'game':GameTextConfig().to_dict()},
                 'live_profiles':{'game':str(live_path)}}
            original=json.dumps(raw).encode('utf-8');path.write_bytes(original)
            with self.assertRaises(LegacyPreferencesDetected):AutoAdvService(settings_file=path)
            Preferences.import_legacy(path)
            backups=list(root.glob('settings.json.legacy-*.bak'))
            self.assertEqual(len(backups),1)
            self.assertEqual(backups[0].read_bytes(),original)
            service=AutoAdvService(settings_file=path)
            self.assertEqual(service.current_game_name(),'game')
            observed,markers=service.monitor_document()
            self.assertNotIn('pid',observed)
            self.assertNotIn('hwnd',observed)
            self.assertEqual(observed['asr_model'],str(root/'model.onnx'))
            self.assertEqual(markers,{})
            self.assertNotIn('live_profiles',json.loads(path.read_text()))
