"""UI and multi-HOOK contracts for the task-oriented interface."""
import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import HookSelection, Profile, SPEAKER_ONLY_RULE, BODY_ONLY_RULE


class HookPairingTests(unittest.TestCase):
    def test_body_and_speaker_fields_are_independently_composed_within_one_synchronized_batch(self):
        p = Profile('id', 'composed')
        p.hooks = [HookSelection('T', 1, 2),
                   HookSelection('S1', 3, 4),
                   HookSelection('S2', 5, 6)]
        p.body_sources = [p.hooks[0].identity]
        p.speaker_sources = [p.hooks[2].identity]
        p.source_rules[p.hooks[0].identity] = [BODY_ONLY_RULE]
        p.source_rules[p.hooks[2].identity] = [SPEAKER_ONLY_RULE]
        c = Controller(lambda *_: None, lambda _: True)
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
        p.body_sources = ['nonexistent|2|3']
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
        for runtime_policy in ('def present(', 'def fallback_due(', 'def presentation_event(',
                               'def advance_if_ready(', 'def expire_next_line('):
            self.assertNotIn(runtime_policy, source)
        self.assertIn('self.application.start_presentation(token)', source)
        for legacy_status in ('host_status', 'hook_target', 'hook_capture_status',
                              'selected_hooks', 'hook_reconnect', 'active_hook_status'):
            self.assertNotIn(legacy_status, source)
        self.assertIn('diagnostics_snapshot', source)
        self.assertIn("('home', 'プレイ'), ('game', 'ゲーム'), ('settings', '設定')", source)
        self.assertIn("group('話者・音声', page='game', section='speakers')", source)
        self.assertNotIn("group('話者とVoice', page='game', section='speakers')", source)

    def test_capture_workspace_exposes_format_preview_and_independent_final_fields(self):
        source = Path('autoadv/gui.py').read_text(encoding='utf-8')
        self.assertIn("QLabel('1. 取得元')", source)
        self.assertIn("QLabel('2. 選択した取得元の整形')", source)
        self.assertIn("QLabel('3. 会話の組み立て')", source)
        self.assertIn("['原文', '話者候補', '本文候補']", source)
        self.assertIn("QLabel('最終結果プレビュー')", source)
        self.assertIn('self.capture_body_source', source)
        self.assertIn('self.capture_speaker_source', source)
        self.assertIn("('話者 + 括弧情報 + 本文', 'metadata')", source)
        self.assertIn("('原文全体を本文候補にする', 'body')", source)
        self.assertIn("('原文全体を話者候補にする', 'speaker')", source)
        self.assertIn("assembly_form.addRow('本文を使う取得元'", source)
        self.assertIn("assembly_form.addRow('話者を使う取得元'", source)
        self.assertNotIn("QLabel('完成する会話')", source)
        self.assertNotIn('_capture_role_buttons', source)
        self.assertNotIn('route_table', source)
        self.assertNotIn('speaker_links', source)

    def test_normal_hook_output_updates_capture_application_without_refresh_command(self):
        source = Path('autoadv/gui.py').read_text(encoding='utf-8')
        self.assertNotIn('self.application.observe_capture(', source)
        self.assertIn('self.application.handle_capture_host_event(event)', source)
        self.assertIn('self.application.queue_capture_output(event)', source)
        self.assertIn('self.application.flush_capture_outputs()', source)
        self.assertIn('self.application.handle_runtime_hook_output(event)', source)
        self.assertIn('self.application.flush_runtime_hook_outputs()', source)
        self.assertNotIn('self._capture_output_group', source)
        self.assertNotIn('self._output_group', source)
        self.assertNotIn('self._synced_sources', source)
        self.assertNotIn('self.host_generation', source)
        self.assertNotIn('self.connection_generation', source)
        self.assertNotIn('self.attached_pid', source)
        self.assertNotIn('self._connecting_pid', source)
        self.assertNotIn('self.pid=', source)
        self.assertNotIn('self.audio_pid=', source)
        self.assertNotIn('self.input_pid=', source)
        self.assertNotIn('self.hwnd=', source)
        self.assertNotIn('self.application.capture.profile', source)
        self.assertNotIn('profile.inherit_common', source)
        self.assertNotIn('self.catalog.create_game(', source)
        self.assertNotIn('self.catalog.add_profile(', source)
        self.assertNotIn('self.catalog.duplicate_profile(', source)
        self.assertNotIn('self.catalog.rename_profile(', source)
        self.assertNotIn('self.catalog.delete_profile(', source)
        self.assertNotIn('self._resume_monitor_requested', source)
        self.assertNotIn('self._pending_monitor_apply', source)
        self.assertNotIn('self.catalog =', source)
        self.assertNotIn('self.game_id =', source)
        self.assertNotIn('self.profile_id =', source)
        self.assertNotIn('self.application.hook_connection_state()', source)
        self.assertIn('self.application.activate_selected_target()', source)
        self.assertIn('self.application.runtime_target_state()', source)
        self.assertIn('if self.application.runtime_target_state().selected:', source)
        self.assertIn('QTimer.singleShot(0, self.connect_game)', source)
        self.assertIn('self.application.handle_hook_error(message)', source)
        self.assertNotIn('self.application.report_capture_problem(', source)
        self.assertNotIn('self.application.update_profile(', source)
        self.assertNotIn('self.application.update_capture_recipe(', source)
        self.assertNotIn('sync_hook_threads(', source)
        self.assertNotIn('capture_transition_boundary(', source)
        self.assertNotIn('consume_capture_transition(', source)
        self.assertNotIn('capture_transition_pending', source)
        self.assertIn('self.application.handle_runtime_ocr_output(', source)
        self.assertIn('self.refresh_capture_workspace()', source)


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
                window.application.observe_capture(capture, '大和「おはよう」', sequence=1)
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
                window.application.select_primary_target(31, 0x100)
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
        profile.hooks = [HookSelection('HOOK-1', 1, 2)]
        profile.body_sources = [profile.hooks[0].identity]

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
                self.assertFalse(hasattr(window, 'btn_edit_detected_speaker'))
                self.assertFalse(hasattr(window, 'btn_fix_extraction'))
                window.btn_advanced_extraction.click()
                self.assertTrue(window.section_toggles['extraction'].isChecked())
                self.assertIn('話者・音声', window.ui_sections['speakers'].title())
                self.assertFalse(window.application.runtime_state().authorized)
                self.assertFalse(hasattr(window, 'btn_monitor'))
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)
