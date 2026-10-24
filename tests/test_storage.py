import json
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
            self.assertEqual(q.speaker_tts['大和'], True)
            self.assertEqual(q.speaker_voices['大和'], 'voice-m')
            self.assertEqual(q.replacements, p.replacements)
            self.assertNotIn('pid', path.read_text(encoding='utf-8'))


    def test_old_v4_hook_roles_are_converted_once_to_field_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            c = Catalog()
            game = c.create_game('Legacy')
            profile = game.profiles[game.default_profile]
            text = HookSelection('TEXT', 1, 2)
            speaker = HookSelection('SPEAKER', 3, 4)
            profile.hooks = [text, speaker]
            profile.body_source = text.identity
            profile.speaker_source = speaker.identity
            save(c, path)

            raw = json.loads(path.read_text(encoding='utf-8'))
            payload = raw['games'][game.id]['profiles'][profile.id]
            payload['hooks'] = [
                {'code': 'TEXT', 'context': 1, 'context2': 2, 'role': 'text'},
                {'code': 'SPEAKER', 'context': 3, 'context2': 4, 'role': 'speaker'},
            ]
            payload.pop('body_source', None)
            payload.pop('speaker_source', None)
            payload.pop('source_rules', None)
            payload['structure_rules'] = [
                {'pattern': r'^(?P<body>.*)$', 'condition': '', 'enabled': True},
            ]
            payload['speaker_links'] = {text.identity: speaker.identity}
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')

            restored = load(path)
            converted = restored.games[game.id].profiles[profile.id]
            self.assertEqual(converted.body_source, text.identity)
            self.assertEqual(converted.speaker_source, speaker.identity)
            self.assertIn(text.identity, converted.source_rules)
            self.assertIn(speaker.identity, converted.source_rules)

            save(restored, path)
            stored = json.loads(path.read_text(encoding='utf-8'))
            rewritten = stored['games'][game.id]['profiles'][profile.id]
            self.assertEqual(rewritten['body_source'], text.identity)
            self.assertEqual(rewritten['speaker_source'], speaker.identity)
            self.assertNotIn('role', rewritten['hooks'][0])
            self.assertNotIn('role', rewritten['hooks'][1])
            self.assertNotIn('structure_rules', rewritten)
            self.assertNotIn('speaker_links', rewritten)

    def test_unrecognized_old_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            path.write_text('{"schema_version": 2, "original": 42}', encoding='utf-8')
            with self.assertRaises(ValueError):
                load(path)
            self.assertEqual(json.loads(path.read_text())['original'], 42)

    def test_old_free_asr_asset_paths_are_not_reused_as_alignment_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            c = Catalog()
            game = c.create_game('Legacy runtime paths')
            profile = game.profiles[game.default_profile]
            save(c, path)

            raw = json.loads(path.read_text(encoding='utf-8'))
            raw['common']['asr_model'] = 'C:/old/sensevoice.onnx'
            raw['common']['asr_tokens'] = 'C:/old/tokens.txt'
            payload = raw['games'][game.id]['profiles'][profile.id]
            payload['asr_model'] = 'C:/profile/sensevoice.onnx'
            payload['asr_tokens'] = 'C:/profile/tokens.txt'
            payload['inherit_common'].extend(['asr_model', 'asr_tokens'])
            path.write_text(json.dumps(raw, ensure_ascii=False), encoding='utf-8')

            restored = load(path)
            loaded = restored.games[game.id].profiles[profile.id]
            self.assertEqual(restored.common.alignment_model, '')
            self.assertEqual(restored.common.alignment_vocab, '')
            self.assertEqual(loaded.alignment_model, '')
            self.assertEqual(loaded.alignment_vocab, '')
            self.assertNotIn('asr_model', loaded.inherit_common)
            self.assertNotIn('asr_tokens', loaded.inherit_common)


if __name__ == '__main__':
    unittest.main()
