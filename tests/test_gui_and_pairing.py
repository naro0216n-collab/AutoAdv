"""UI and multi-HOOK contracts for the task-oriented interface."""
import importlib.util
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.g2p_support import TEST_G2P, FakeG2P

from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import HookSelection, Profile, SPEAKER_ONLY_RULE, BODY_ONLY_RULE


class HookPairingTests(unittest.TestCase):
    def test_body_and_speaker_fields_are_independently_composed_within_one_synchronized_batch(self):
        p = Profile('id', 'composed')
        p.hooks = [HookSelection('T', 1, 2),
                   HookSelection('S1', 3, 4),
                   HookSelection('S2', 5, 6)]
        p.body_source = p.hooks[0].identity
        p.speaker_source = p.hooks[2].identity
        p.source_rules[p.hooks[0].identity] = [BODY_ONLY_RULE]
        p.source_rules[p.hooks[2].identity] = [SPEAKER_ONLY_RULE]
        c = Controller(lambda *_: None, lambda _: True, TEST_G2P)
        c.begin(p, Target(123, 456), 'generation', 1)

        def e(s, seq, value):
            return HookEvent('output', 'generation', 1, seq, seq * 1_000_000,
                             123, ThreadSource(123, seq, s.context, s.context2, s.code, s.code), value)

        token = c.observe_group([e(p.hooks[1], 1, 'wrong'), e(p.hooks[2], 2, '花子'),
                                 e(p.hooks[0], 3, '台詞')])
        self.assertIsNotNone(token)
        self.assertEqual((c.utterance.speaker, c.utterance.text), ('花子', '台詞'))
        c.permit(True)
        self.assertIsNone(c.observe_group([e(p.hooks[2], 4, '古い')]))
        self.assertTrue(c.authorized)
        c.observe_group([e(p.hooks[0], 5, '次')])
        self.assertEqual(c.utterance.speaker, '')

    def test_provider_must_reference_a_selected_source(self):
        p = Profile('id', 'invalid', hooks=[HookSelection('T', 1, 2)])
        p.body_source = 'nonexistent|2|3'
        with self.assertRaises(ValueError):
            p.validate()




@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Windows GUI dependencies not installed')
class WindowsGuiInitialization(unittest.TestCase):
    def test_speaker_voice_edit_is_downstream_from_capture_extraction(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('検査ゲーム')
        p = game.profiles[game.default_profile]
        p.speaker_tts.update({'京': True, '大和': True})
        p.speaker_voices.update({'京': 'v1', '大和': 'v2'})

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertEqual([window.tabs.tabText(i) for i in range(window.tabs.count())],
                                 ['プレイ', 'ゲーム', '設定'])
                self.assertEqual(window.speaker_table.rowCount(), 2)
                capture = ThreadSource(101, 1, 1, 2, 'HOOK', 'HOOK')
                window.application.select_primary_target(101, 0x1234)
                window.application.hook_host = SimpleNamespace(
                    command=Mock(), child=SimpleNamespace(poll=lambda: None))
                self.assertEqual(window.application.activate_selected_target(), 'connecting')
                window.application.handle_hook_event(
                    HookEvent('connected', 'host', 1, 1, 10, process_id=101))
                window.application.handle_hook_event(
                    HookEvent('output', 'host', 1, 2, 20, 101, capture, '大和「おはよう」'))
                QApplication.processEvents()
                window.application.set_capture_field_source('body', capture.identity, capture.address)
                window.refresh_capture_workspace()
                window.example_raw.setPlainText('おはよう')
                self.assertIn('TTS: Voice v2', window.editor_preview.toPlainText())
                row = next(i for i in range(window.speaker_table.rowCount())
                           if window.speaker_table.item(i, 0).text() == '大和')
                window.speaker_table.cellWidget(row, 2).setEditText('v3')
                self.assertEqual(window.current_profile().speaker_voices['大和'], 'v3')
                self.assertIn('TTS: Voice v3', window.editor_preview.toPlainText())
                self.assertEqual(window.ui_sections['speakers'].title(), '話者・音声')
                self.assertIsNot(window.ui_sections['source_format'], window.ui_sections['speakers'])
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_irodori_audition_switches_voice_ids_without_rebuilding_the_active_row(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from types import SimpleNamespace
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog

        catalog = Catalog()
        game = catalog.create_game('試聴確認')
        profile = game.profiles[game.default_profile]
        profile.default_voice = 'voice-1'
        profile.inherit_common = [name for name in profile.inherit_common if name != 'default_voice']
        profile.speaker_tts['京'] = True
        profile.speaker_voices['京'] = 'voice-1'

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.on_server(SimpleNamespace(issue='', voices=('voice-1', 'voice-2', 'voice-3')))
                row = next(i for i in range(window.speaker_table.rowCount())
                           if window.speaker_table.item(i, 0).text() == '京')
                combo = window.speaker_table.cellWidget(row, 2)
                calls = []
                window.application.start_voice_preview = lambda reading, voice: calls.append((reading, voice))

                combo.setCurrentText('voice-2')
                QApplication.processEvents()
                self.assertIs(window.speaker_table.cellWidget(row, 2), combo)
                self.assertEqual(window.current_profile().speaker_voices['京'], 'voice-2')
                window.speaker_table.cellWidget(row, 3).click()
                self.assertEqual(calls[-1], (window.audition_text.text(), 'voice-2'))
                self.assertEqual(window.audition_voice_choice.currentText(), 'voice-2')

                window.audition_voice_choice.setCurrentText('voice-3')
                window.btn_audition.click()
                self.assertEqual(calls[-1], (window.audition_text.text(), 'voice-3'))
                window.on_server(SimpleNamespace(issue='', voices=('voice-3', 'voice-2', 'voice-1')))
                self.assertEqual(window.audition_voice_choice.currentText(), 'voice-3')
                refreshed_row = next(i for i in range(window.speaker_table.rowCount())
                                     if window.speaker_table.item(i, 0).text() == '京')
                self.assertEqual(window.speaker_table.cellWidget(refreshed_row, 2).currentText(), 'voice-2')
                window.audition_voice_choice.setCurrentText('voice-1')
                window.btn_audition.click()
                self.assertEqual(calls[-1], (window.audition_text.text(), 'voice-1'))
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_common_default_is_effective_until_direct_edit_creates_an_override(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('作業フロー確認')
        profile = game.profiles[game.default_profile]
        profile.reading_ms = 1750
        profile.inherit_common = ['reading_ms']
        catalog.common.reading_ms = 900

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertEqual(window.reading_ms.value(), 900)
                self.assertTrue(window.profile_reset_buttons['reading_ms'].isHidden())
                window.reading_ms.setValue(1200)
                self.assertEqual(window.current_profile().reading_ms, 1200)
                self.assertNotIn('reading_ms', window.current_profile().inherit_common)
                self.assertFalse(window.profile_reset_buttons['reading_ms'].isHidden())
                window.profile_reset_buttons['reading_ms'].click()
                self.assertIn('reading_ms', window.current_profile().inherit_common)
                self.assertEqual(window.reading_ms.value(), 900)
                self.assertTrue(window.profile_reset_buttons['reading_ms'].isHidden())
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_win32_generic_hooks_are_recovery_policy_not_a_normal_search_step(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('Win32 test', 'C:/Game.exe')
        commands = Mock()

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.insert_pc_hooks_on_attach.setChecked(True)
                self.assertTrue(window.current_profile().insert_pc_hooks)
                window.application.select_primary_target(31, 0x100)
                window.application.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                window.connect_game()
                commands.assert_any_call('connect', pid=31)
                commands.reset_mock()
                window.application.handle_hook_event(HookEvent('connected', 'host', 1, 1, 100, process_id=31))
                commands.assert_any_call('insert_pc_hooks', pid=31)
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_gui_construction_without_a_game_or_external_server(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        with patch('autoadv.gui.load', return_value=Catalog()), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', return_value=0):
                self.assertEqual(run_gui(), 0)


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Qt runtime required')
class TaskOrientedGuiContracts(unittest.TestCase):
    def test_auto_toggle_can_be_enabled_before_monitor_and_normal_state_needs_no_status_prose(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        game = catalog.create_game('作品', 'C:/Game.exe')
        profile = game.profiles[game.default_profile]
        profile.hooks = [HookSelection('HOOK-1', 1, 2)]
        profile.body_source = profile.hooks[0].identity

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.advance_authorization.click()
                self.assertTrue(window.application.runtime_state().authorized)
                self.assertEqual(window.advance_authorization.text(), '自動送り ON')
                self.assertFalse(window.play_issue.isVisible())
                window.application.select_primary_target(101, 0x1234)
                commands = Mock()
                window.application.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                window.connect_game()
                window.application.handle_hook_event(HookEvent('connected', 'host', 1, 1, 100, process_id=101))
                src = ThreadSource(101, 0x500, 1, 2, 'HOOK-1', 'HOOK-1')
                window.application.handle_hook_event(HookEvent('output', 'host', 1, 2, 200,
                                         process_id=101, source=src, text='京「おはよう」'))
                QApplication.processEvents()
                self.assertIsNotNone(window.application.runtime_state().profile)
                self.assertTrue(window.application.runtime_state().authorized)
                self.assertFalse(window.play_issue.isVisible())
                self.assertIn('京', window.preview.toPlainText())
                self.assertIn('おはよう', window.preview.toPlainText())
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_capture_formatting_and_speaker_voice_settings_are_separate_workflows(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        catalog.create_game('保存済み作品', 'C:/Game.exe')

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.btn_advanced_extraction.click()
                self.assertTrue(window.section_toggles['extraction'].isChecked())
                self.assertIn('話者・音声', window.ui_sections['speakers'].title())
                self.assertFalse(window.application.runtime_state().authorized)
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)
