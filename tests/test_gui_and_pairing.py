"""UI integration and source-scoped HOOK pairing in the rebuilt application."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
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
                self.assertEqual(window.tabs.count(), 3)
                window.example_raw.setPlainText('大和「おはよう」')
                self.assertIn('話者: 大和', window.editor_preview.toPlainText())
                self.assertIn('Voice v2', window.editor_preview.toPlainText())
                row = next(i for i in range(window.speaker_table.rowCount())
                           if window.speaker_table.item(i, 0).text() == '大和')
                voice = window.speaker_table.cellWidget(row, 2)
                voice.setEditText('v3')
                self.assertEqual(window.current_profile().speaker_voices['大和'], 'v3')
                self.assertIn('Voice v3', window.editor_preview.toPlainText())
                self.assertFalse(hasattr(window, 'assign_speaker'))
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
    def test_win32_generic_hooks_are_opt_in_and_restored_in_real_gui(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('Win32 test', 'C:/Game.exe')
        p = game.profiles[game.default_profile]
        p.insert_pc_hooks = False
        commands = Mock()

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertFalse(window.insert_pc_hooks_on_attach.isChecked())
                window.pid.setValue(31)
                window.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                window.find_hooks()
                commands.assert_not_called()
                window.insert_pc_hooks_on_attach.setChecked(True)
                window.connect_game()
                self.assertTrue(window.current_profile().insert_pc_hooks)
                commands.assert_any_call('connect', pid=31)
                commands.reset_mock()
                window.on_hook(HookEvent('connected', 'host', 1, 1, 100, process_id=31))
                commands.assert_any_call('insert_pc_hooks', pid=31)
                commands.reset_mock()
                window.on_hook(HookEvent('disconnected', 'host', 1, 2, 150, process_id=31))
                window.find_hooks()
                commands.assert_not_called()
                window.insert_pc_hooks_on_attach.setChecked(False)
                self.assertTrue(window.save_profile())
                self.assertFalse(window.current_profile().insert_pc_hooks)
                commands.reset_mock()
                window.on_hook(HookEvent('connected', 'host2', 2, 2, 200, process_id=31))
                commands.assert_not_called()
            finally:
                window.hook_host = None
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

@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Qt runtime required')
class TaskOrientedGuiContracts(unittest.TestCase):
    def test_every_advanced_setting_has_a_context_and_inheritance_shows_actual_value(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('作業フロー確認')
        profile = game.profiles[game.default_profile]
        profile.reading_ms = 1750
        catalog.common.reading_ms = 900
        profile.inherit_common = ['reading_ms']

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())],
                                 ['プレイ', '台詞取得', '台詞・音声'])
                self.assertFalse(hasattr(window, 'found_hooks'))
                for section in ('game_details', 'target_details', 'advance_details', 'audio_details',
                                'hook_details', 'ocr_details', 'irodori', 'extraction', 'replacement'):
                    self.assertIn(section, window.section_toggles)
                    self.assertIn(section, window.section_summaries)
                    self.assertFalse(window.section_toggles[section].isChecked())
                    self.assertTrue(window.section_summaries[section].text())
                self.assertTrue(window.inherit_controls['reading_ms'].isChecked())
                self.assertEqual(window.reading_ms.value(), 900)
                self.assertIn('共通: 900', window.inherit_source_labels['reading_ms'].text())
                self.assertFalse(window.reading_ms.isEnabled())
                window.inherit_controls['reading_ms'].setChecked(False)
                self.assertTrue(window.reading_ms.isEnabled())
                self.assertEqual(window.reading_ms.value(), 1750)
                window.reading_ms.setValue(1200)
                self.assertEqual(window.profile_save_state.text(), '未保存の変更あり')
                self.assertTrue(window.save_profile())
                self.assertEqual(window.profile_save_state.text(), '保存済み')
                self.assertEqual(profile.reading_ms, 1750)  # old instance not overwritten
                self.assertEqual(window.current_profile().reading_ms, 1200)
                window.inherit_controls['reading_ms'].setChecked(True)
                self.assertTrue(window.save_profile())
                self.assertEqual(window.current_profile().reading_ms, 1200)
                self.assertEqual(window.reading_ms.value(), 900)
                window.btn_recovery.click()
                self.assertTrue(window.section_toggles['hook_details'].isChecked())
                self.assertEqual(window.tabs.currentIndex(), 1)
                window.source_mode.setCurrentIndex(window.source_mode.findData('ocr'))
                self.assertTrue(window.section_toggles['ocr_details'].isChecked())
            finally:
                window.close()
            return 0
        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_livedialogue_edit_and_saved_resume_do_not_authorize_input(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('保存済み作品', 'C:/Game.exe')
        profile = game.profiles[game.default_profile]
        profile.hooks = [HookSelection('HOOK-1', 1, 2, 'text')]

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                # An ordinary user can find the related editor directly from a sample.
                window.example_raw.setPlainText('京「おはよう」')
                window.btn_edit_detected_speaker.click()
                self.assertEqual(window.new_speaker_name.text(), '京')
                self.assertEqual(window.tabs.currentIndex(), 2)
                window.btn_fix_extraction.click()
                self.assertTrue(window.section_toggles['extraction'].isChecked())
                self.assertFalse(hasattr(window, 'btn_resume'))
                self.assertFalse(hasattr(window, 'btn_monitor'))
                window.pid.setValue(101)
                window.hwnd.setText('0x1234')
                commands = Mock()
                window.hook_host = SimpleNamespace(command=commands,
                    child=SimpleNamespace(poll=lambda: None))
                window.connect_game()
                self.assertFalse(window.controller.authorized)
                window.on_hook(HookEvent('connected', 'host', 1, 1, 100, process_id=101))
                src = ThreadSource(101, 0x500, 1, 2, 'HOOK-1', 'HOOK-1')
                window.on_hook(HookEvent('output', 'host', 1, 2, 200,
                                         process_id=101, source=src, text='京「おはよう」'))
                QApplication.processEvents()
                self.assertIsNotNone(window.controller.profile)
                self.assertFalse(window.controller.authorized)
            finally:
                window.hook_host = None
                window.close()
            return 0
        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)
