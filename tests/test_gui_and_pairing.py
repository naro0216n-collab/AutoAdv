"""UI and multi-HOOK contracts for the task-oriented interface."""
import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import HookSelection, Profile


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
        self.assertTrue(c.authorized)
        c.observe_group([e(p.hooks[0], 5, '次')])
        self.assertEqual(c.utterance.speaker, '')

    def test_invalid_speaker_link_is_rejected_in_one_profile_config(self):
        p = Profile('id', 'invalid', hooks=[HookSelection('T', 1, 2)])
        p.speaker_links = {p.hooks[0].identity: 'nonexistent|2|3'}
        with self.assertRaises(ValueError):
            p.validate()


class GuiSourceArchitectureTests(unittest.TestCase):
    """These contracts run even when Qt is unavailable locally."""

    def test_old_form_and_status_contracts_are_not_the_new_gui_api(self):
        source = Path('autoadv/gui.py').read_text(encoding='utf-8')
        self.assertNotIn('def save_profile(', source)
        self.assertNotIn('overview_capture', source)
        self.assertNotIn('overview_voice', source)
        self.assertNotIn('overview_advance', source)
        self.assertNotIn('profile_save_state', source)
        self.assertNotIn('inherit_controls', source)
        self.assertNotIn("QPushButton('ほかの取得候補を探す')", source)
        self.assertIn("QPushButton('高度なHOOK探索を試す')", source)
        self.assertNotIn('from .hook_host import HookHost', source)
        self.assertNotIn('self.hook_host', source)
        self.assertNotIn('from .irodori_server import IrodoriServer', source)
        self.assertNotIn('self.server =', source)
        self.assertNotIn('self.server:', source)
        self.assertNotIn('from .native_audio import NativeAudio', source)
        self.assertNotIn('from .ocr import OCRWorker', source)
        self.assertNotIn('from .voice import VoiceJobs', source)
        self.assertNotIn('self.native_audio', source)
        self.assertNotIn('self.ocr_worker', source)
        self.assertNotIn('self.jobs', source)
        self.assertNotIn('self.preview_jobs', source)
        self.assertNotIn('self.sources', source)
        self.assertNotIn('self.source_samples', source)
        self.assertNotIn('self.ambiguous_sources', source)
        self.assertNotIn('self._searched_codes', source)
        self.assertNotIn('self._trial_hook_codes', source)
        self.assertNotIn('status_reason_label', source)
        self.assertNotIn('native_inconclusive_hold', source)
        self.assertNotIn('application.controller', source)
        self.assertIn("('home', 'プレイ'), ('game', 'ゲーム'), ('settings', '設定')", source)

    def test_normal_hook_output_updates_capture_application_without_refresh_command(self):
        source = Path('autoadv/gui.py').read_text(encoding='utf-8')
        self.assertIn('self.application.observe_capture(', source)
        self.assertIn('self.application.observe_capture_group(group)', source)
        self.assertIn('self.refresh_capture_workspace()', source)


@unittest.skipUnless(importlib.util.find_spec('PySide6'), 'Windows GUI dependencies not installed')
class WindowsGuiInitialization(unittest.TestCase):
    def test_multiple_speakers_and_rule_preview_are_editable_in_the_same_game_workspace(self):
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
                window.example_raw.setPlainText('大和「おはよう」')
                self.assertIn('話者: 大和', window.editor_preview.toPlainText())
                self.assertIn('TTS: Voice v2', window.editor_preview.toPlainText())
                row = next(i for i in range(window.speaker_table.rowCount())
                           if window.speaker_table.item(i, 0).text() == '大和')
                window.speaker_table.cellWidget(row, 2).setEditText('v3')
                self.assertEqual(window.current_profile().speaker_voices['大和'], 'v3')
                self.assertIn('TTS: Voice v3', window.editor_preview.toPlainText())
                window.clear_structure_editor()
                window.structure_pattern.setText(r'^(?P<speaker>[^「」]+)「(?P<body>.+)」$')
                window.add_structure_rule()
                self.assertEqual(window.structure_table.item(0, 0).text(), '1')
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
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

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
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
                self.assertFalse(hasattr(window, 'btn_find'))
                window.insert_pc_hooks_on_attach.setChecked(True)
                self.assertTrue(window.current_profile().insert_pc_hooks)
                window.pid.setValue(31)
                window.hwnd.setText('0x100')
                window.application.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                window.connect_game()
                commands.assert_any_call('connect', pid=31)
                commands.reset_mock()
                window.on_hook(HookEvent('connected', 'host', 1, 1, 100, process_id=31))
                commands.assert_any_call('insert_pc_hooks', pid=31)
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_gui_construction_without_a_game_or_external_server(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        with patch('autoadv.gui.load', return_value=Catalog()):
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
        profile.hooks = [HookSelection('HOOK-1', 1, 2, 'text')]

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.advance_authorization.click()
                self.assertTrue(window.application.runtime_state().authorized)
                self.assertEqual(window.advance_authorization.text(), '自動送り ON')
                self.assertFalse(window.play_issue.isVisible())
                window.pid.setValue(101)
                window.hwnd.setText('0x1234')
                commands = Mock()
                window.application.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                window.connect_game()
                window.on_hook(HookEvent('connected', 'host', 1, 1, 100, process_id=101))
                src = ThreadSource(101, 0x500, 1, 2, 'HOOK-1', 'HOOK-1')
                window.on_hook(HookEvent('output', 'host', 1, 2, 200,
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

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_current_dialogue_edit_stays_in_game_workspace_and_does_not_authorize_input(self):
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
                window.example_raw.setPlainText('京「おはよう」')
                window.btn_edit_detected_speaker.click()
                self.assertEqual(window.new_speaker_name.text(), '京')
                self.assertEqual(window.tabs.currentIndex(), 1)
                window.btn_fix_extraction.click()
                self.assertTrue(window.section_toggles['extraction'].isChecked())
                self.assertFalse(window.application.runtime_state().authorized)
                self.assertFalse(hasattr(window, 'btn_monitor'))
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)
