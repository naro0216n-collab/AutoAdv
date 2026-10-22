"""UI integration and source-scoped HOOK pairing in the rebuilt application."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from autoadv.conversation import Controller, Target
from autoadv.model import Profile, HookSelection
from autoadv.hook_protocol import HookEvent, ThreadSource


class HookPairingTests(unittest.TestCase):
    def test_selected_speaker_link_applies_only_within_synchronized_outputs(self):
        p = Profile('id', 'paired')
        p.hooks = [HookSelection('T', 1, 2, 'text'),
                   HookSelection('S1', 3, 4, 'speaker'),
                   HookSelection('S2', 5, 6, 'speaker')]
        p.speaker_links = {p.hooks[0].identity: p.hooks[2].identity}
        c = Controller(lambda *_: None, lambda _: True)
        c.begin(p, Target(123, 456), 'generation', 1)
        def e(s, seq, value):
            return HookEvent('output', 'generation', 1, seq, seq * 1_000_000,
                             123, ThreadSource(123, seq, s.context, s.context2, s.code, s.code), value)
        token = c.observe_group([e(p.hooks[1], 1, 'wrong'), e(p.hooks[2], 2, '花子'),
                                 e(p.hooks[0], 3, '台詞')])
        self.assertIsNotNone(token)
        self.assertEqual(c.utterance.speaker, '花子')
        c.permit(True)
        self.assertIsNone(c.observe_group([e(p.hooks[2], 4, '古い')]))
        self.assertFalse(c.authorized)
        c.observe_group([e(p.hooks[0], 5, '次')])
        self.assertEqual(c.utterance.speaker, '')

    def test_invalid_speaker_link_is_rejected_in_one_profile_config(self):
        p = Profile('id', 'invalid', hooks=[HookSelection('T', 1, 2)])
        p.speaker_links = {p.hooks[0].identity: 'nonexistent|2|3'}
        with self.assertRaises(ValueError):
            p.validate()


class WindowsGuiInitialization(unittest.TestCase):
    @unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Windows GUI dependencies not installed')
    def test_multiple_speakers_and_rule_preview_are_editable_in_the_same_profile(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('検査ゲーム')
        p = game.profiles[game.default_profile]
        p.speaker_tts.update({'京': True, '大和': False})
        p.speaker_voices.update({'京': 'v1', '大和': 'v2'})

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertEqual(window.speaker_table.rowCount(), 2)
                self.assertEqual(window.tabs.count(), 5)
                window.example_raw.setPlainText('大和「おはよう」')
                self.assertIn('話者: 大和', window.editor_preview.toPlainText())
                self.assertIn('Voice v2', window.editor_preview.toPlainText())
                window.speaker_table.selectRow(1)
                window.speaker_name.setText('大和')
                window.speaker_voice.setEditText('v3')
                self.assertIn('Voice v3', window.editor_preview.toPlainText())
                window.assign_speaker()
                self.assertEqual(window.current_profile().speaker_voices['大和'], 'v3')
                window.clear_structure_editor()
                window.structure_pattern.setText(r'^(?P<speaker>[^「」]+)「(?P<body>.+)」$')
                self.assertIn('優先順位 1', window.editor_preview.toPlainText())
                window.add_structure_rule()
                self.assertEqual(window.structure_table.item(0, 0).text(), '1')
                window.clear_replacement_editor()
                window.replace_pattern.setText('おはよう')
                window.replace_value.setText('こんにちは')
                self.assertIn('こんにちは', window.editor_preview.toPlainText())
                window.add_replacement()
                self.assertIn('こんにちは', window.editor_preview.toPlainText())
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    @unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Windows GUI dependencies not installed')
    def test_gui_construction_without_a_game_or_external_server(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        # This verifies actual widget construction, signals and control order.
        with patch('autoadv.gui.load', return_value=__import__('autoadv.storage', fromlist=['Catalog']).Catalog()):
            with patch.object(QApplication, 'exec', return_value=0):
                self.assertEqual(run_gui(), 0)

if __name__ == '__main__':
    unittest.main()
