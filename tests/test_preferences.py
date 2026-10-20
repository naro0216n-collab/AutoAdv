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
