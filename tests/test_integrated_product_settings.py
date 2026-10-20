"""Cross-domain contracts exercising configurations, provenance and transition edges."""
import json
import tempfile
import unittest
from pathlib import Path
from autoadv.conversation import Controller, Target, Token
from autoadv.hook_protocol import ThreadSource, HookEvent
from autoadv.import_legacy import inspect_legacy, add_legacy_games
from autoadv.import_luna import inspect_luna, add_luna_games
from autoadv.model import Profile, HookSelection, AudioMode, interpret, OCRSettings
from autoadv.storage import Catalog, CommonSettings, load, save
from autoadv.ocr import OCRWorker


class SessionOwnership(unittest.TestCase):
    def test_different_hook_audio_and_input_processes_keep_roles(self):
        p = Profile('p', 'test', hooks=[HookSelection('H', 2, 3)])
        sent = []
        controller = Controller(lambda target, key: sent.append((target.pid, target.audio_pid, key)),
                                lambda target: target.hwnd == 800 and target.pid == 22)
        target = Target(22, 800, hook_pid=11, audio_pid=33)
        controller.begin(p, target, 'native-host', 3)
        hook = ThreadSource(11, 100, 2, 3, 'H', 'hook')
        self.assertIsNone(controller.observe(generation='native-host', connection=3,
                    sequence=1, source=hook, text='foreign input pid', pid=22))
        token = controller.observe(generation='native-host', connection=3,
                    sequence=2, source=hook, text='real hook output', pid=11)
        self.assertIsNotNone(token)
        controller.permit(True)
        controller.mark_complete(token)
        self.assertTrue(controller.try_advance())
        self.assertEqual(sent, [(22, 33, 13)])
        self.assertFalse(controller.try_advance())

    def test_pause_reason_survives_audio_completion_and_late_line(self):
        p = Profile('p', 'P', hooks=[HookSelection('H', 2, 3)])
        c = Controller(lambda *_: None, lambda _: True)
        c.begin(p, Target(10, 20), 'host', 1)
        source = ThreadSource(10, 100, 2, 3, 'H', 'hook')
        token = c.observe(generation='host', connection=1, sequence=1,
                          source=source, text='line', pid=10)
        c.permit(True)
        c.pause('manual_intervention')
        c.mark_complete(token)
        self.assertEqual(c.reason, 'manual_intervention')
        self.assertFalse(c.authorized)
        second = c.observe(generation='host', connection=1, sequence=2,
                           source=source, text='another', pid=10)
        self.assertIsNotNone(second)
        self.assertEqual(c.reason, 'manual_intervention')
        c.permit(True)
        self.assertEqual(c.reason, 'presenting')

    def test_selected_text_priority_vs_multiple_flushes_of_one_owner(self):
        p = Profile('p', 'many', hooks=[HookSelection('H', 2, 3), HookSelection('ALT', 4, 5)])
        c = Controller(lambda *_: None, lambda _: True)
        c.begin(p, Target(10, 20), 'host', 1, ('H|2|3', 'ALT|4|5'))
        primary = ThreadSource(10, 100, 2, 3, 'H', 'primary')
        aux = ThreadSource(10, 200, 4, 5, 'ALT', 'aux')
        def event(src, seq, text):
            return HookEvent('output', 'host', 1, seq, seq * 1_000_000, 10, src, text)
        token = c.observe_group([event(aux, 1, 'duplicate'), event(primary, 2, 'chosen')])
        self.assertIsNotNone(token)
        self.assertEqual(c.utterance.raw, 'chosen')
        c.permit(True)
        self.assertIsNone(c.observe_group([event(primary, 3, 'first'), event(primary, 4, 'second')]))
        self.assertEqual(c.reason, 'ambiguous_multihook_lines')
        self.assertFalse(c.authorized)


class CommonPreferencesContract(unittest.TestCase):
    def test_resolve_at_session_start_preserves_explicit_overrides_and_legacy_values(self):
        c = Catalog(common=CommonSettings(default_mode=AudioMode.TTS,
                                          default_voice='narrator', asr_model='C:/asr', input_key=32))
        g = c.create_game('first')
        p = g.profiles[g.default_profile]
        self.assertEqual(c.effective_profile(p).default_voice, 'narrator')
        self.assertEqual(c.effective_profile(p).input_key, 32)
        p.inherit_common.remove('input_key')
        p.input_key = 13
        effective = c.effective_profile(p)
        self.assertEqual(effective.input_key, 13)
        self.assertEqual(effective.asr_model, 'C:/asr')
        c.common.default_voice = 'another'
        self.assertEqual(effective.default_voice, 'narrator')  # snapshot is immutable to later config changes
        self.assertEqual(c.effective_profile(p).default_voice, 'another')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            g.audio_executable, g.input_executable = 'C:/audio.exe', 'C:/window.exe'
            save(c, path)
            restored = load(path)
            self.assertEqual(restored.effective_profile(restored.games[g.id].profiles[p.id]).input_key, 13)
            self.assertEqual(restored.games[g.id].audio_executable, 'C:/audio.exe')
            self.assertNotIn('"pid"', path.read_text(encoding='utf-8'))


class UserConfirmedImport(unittest.TestCase):
    def test_luna_repair_and_host_settings_are_imported_without_mutating_original(self):
        payload = [['uid'], {'uid': {
            'title': 'Demo', 'gamepath': 'C:/demo.exe',
            'hook': [[100, 2, 3, 4, 'thread', 'HW']],
            'needinserthookcode': ['HS1'], 'tts_follow_default': False,
            'tts_repair': True, 'tts_repair_regex': [{'key': '京', 'value': 'みやこ', 'regex': False}],
            'hooksetting_private': {'textthreaddelay': 600, 'maxBufferSize': 4000},
            'tts_skip': True, 'tts_skip_regex': [{'key': 'x', 'target': 'skip'}],
        }}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'luna.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            entries = inspect_luna(path)
            self.assertTrue(entries[0].unmapped)
            result = add_luna_games(Catalog(), entries)
            p = next(iter(result.games.values())).profiles[next(iter(result.games.values())).default_profile]
            self.assertEqual(p.hook_flush_ms, 600)
            self.assertEqual(p.hook_max_buffer, 4000)
            self.assertEqual(p.replacements[0].replacement, 'みやこ')
            self.assertEqual(json.loads(path.read_text()), payload)

    def test_previous_autoadv_settings_never_overwrite_existing_games(self):
        prior_profile = {
            'profile_id': 'pid', 'name': 'Old Profile',
            'text': {'name_readings': {'京': 'みやこ'},
                     'speaker_routes': {'京': {'policy': 'SELECTED', 'voice_id': 'V1'}},
                     'unknown_route': 'SILENT'},
            'selected_hooks': [{'code': 'HW', 'name': 'thread', 'context': 1, 'context2': 2}],
            'quality': {'postprocessconfig': 'unknown'},
        }
        prior_game = {'game_id': 'gid', 'display_name': 'Previous',
                      'paths': ['C:/previous.exe'], 'default_profile_id': 'pid',
                      'profiles': {'pid': prior_profile}}
        data = {'schema_version': 2, 'irodori_root': 'C:/old',
                'catalog': {'games': {'gid': prior_game}}, 'globals': {}}
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / 'previous.json'
            src.write_text(json.dumps(data), encoding='utf-8')
            snapshot = src.read_bytes()
            rows = inspect_legacy(src)
            self.assertTrue(rows[0].unmapped)
            restored = add_legacy_games(Catalog(), rows)
            p = restored.games['gid'].profiles['pid']
            self.assertEqual(p.name_readings['京'], 'みやこ')
            self.assertEqual(p.speaker_voices['京'], 'V1')
            self.assertEqual(p.speaker_modes['京'], AudioMode.TTS)
            with self.assertRaisesRegex(ValueError, 'already imported'):
                add_legacy_games(restored, rows)
            self.assertEqual(snapshot, src.read_bytes())


class OcrEvidence(unittest.TestCase):
    def test_stability_and_unverified_revisions_never_trigger_an_extra_input(self):
        values = ['台詞A', '台詞B', '台詞A', '台詞A', '台詞B', '台詞B', '', '']
        outputs = []
        class Reader:
            config = OCRSettings(stable_samples=2)
            def recognize(self):
                value = values.pop(0)
                if not values:
                    worker.stopping.set()
                return value
        worker = OCRWorker(Reader(), lambda seq, text: outputs.append((seq, text)), lambda _: None)
        worker.stopping.wait = lambda _: None
        worker._run()
        self.assertEqual(outputs, [(1, '台詞A'), (2, '台詞B'), (3, '')])
        c = Controller(lambda *_: None, lambda _: True)
        p = Profile('p', 'OCR', source='ocr')
        session = c.begin(p, Target(11, 22), 'ocr', 1)
        first = c.observe_ocr(sequence=1, text='台詞A', pid=11, session=session)
        c.permit(True)
        self.assertIsNone(c.observe_ocr(sequence=2, text='台詞B', pid=11, session=session))
        self.assertEqual(c.token, first)
        self.assertFalse(c.authorized)
        self.assertIsNone(c.observe_ocr(sequence=3, text='', pid=11, session=session))
        self.assertIsNotNone(c.observe_ocr(sequence=4, text='台詞B', pid=11, session=session))


if __name__ == '__main__':
    unittest.main()
