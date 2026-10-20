import json
import tempfile
import unittest
from pathlib import Path
from autoadv.model import AudioMode, HookSelection, ReplacementRule
from autoadv.storage import Catalog, load, save


class SettingsContract(unittest.TestCase):
    def test_roundtrip_multiple_games_profiles_and_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            c = Catalog(irodori_root='C:/tts', host_dll='C:/luna/LunaHost.dll')
            a = c.create_game('ゲーム1', 'C:/games/a.exe')
            b = c.create_game('ゲーム2', 'C:/games/b.exe')
            p = c.add_profile(a.id, 'Route B')
            p.hooks = [HookSelection('HX', 1, 2, 'text'), HookSelection('HY', 5, 6, 'speaker')]
            p.speaker_modes = {'大和': AudioMode.TTS}
            p.speaker_voices = {'大和': 'voice-m'}
            p.replacements = [ReplacementRule('東京', 'とうきょう')]
            a.default_profile = p.id
            save(c, path)
            restored = load(path)
            self.assertEqual(set(restored.games), {a.id, b.id})
            q = restored.games[a.id].profiles[p.id]
            self.assertEqual(q.hooks, p.hooks)
            self.assertEqual(q.speaker_modes['大和'], AudioMode.TTS)
            self.assertEqual(q.speaker_voices['大和'], 'voice-m')
            self.assertEqual(q.replacements, p.replacements)
            self.assertNotIn('pid', path.read_text(encoding='utf-8'))

    def test_unrecognized_old_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            path.write_text('{"schema_version": 2, "original": 42}', encoding='utf-8')
            with self.assertRaises(ValueError):
                load(path)
            self.assertEqual(json.loads(path.read_text())['original'], 42)


if __name__ == '__main__':
    unittest.main()
