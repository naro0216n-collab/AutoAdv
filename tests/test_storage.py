import tempfile
import unittest
from pathlib import Path
from autoadv.model import AdvanceMode, HookSelection, ReplacementRule
from autoadv.storage import Catalog, load, save


class SettingsContract(unittest.TestCase):
    def test_roundtrip_multiple_games_profiles_and_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            c = Catalog(irodori_root='C:/tts', host_dll='C:/luna/LunaHost.dll')
            a = c.create_game('ゲーム1', 'C:/games/a.exe')
            b = c.create_game('ゲーム2', 'C:/games/b.exe')
            p = c.add_profile(a.id, 'Route B')
            p.hooks = [HookSelection('HX', 1, 2), HookSelection('HY', 5, 6)]
            p.body_source = p.hooks[0].identity
            p.speaker_source = p.hooks[1].identity
            p.unwrap_speaker_enclosed_body = True
            p.speaker_tts = {'大和': True}
            p.speaker_voices = {'大和': 'voice-m'}
            p.replacements = [ReplacementRule('東京', 'とうきょう')]
            a.default_profile = p.id
            save(c, path)
            restored = load(path)
            self.assertEqual(set(restored.games), {a.id, b.id})
            q = restored.games[a.id].profiles[p.id]
            self.assertEqual(q.hooks, p.hooks)
            self.assertEqual(q.body_source, p.body_source)
            self.assertEqual(q.speaker_source, p.speaker_source)
            self.assertTrue(q.unwrap_speaker_enclosed_body)
            self.assertEqual(q.speaker_tts['大和'], True)
            self.assertEqual(q.speaker_voices['大和'], 'voice-m')
            self.assertEqual(q.replacements, p.replacements)
            self.assertNotIn('pid', path.read_text(encoding='utf-8'))



if __name__ == '__main__':
    unittest.main()
