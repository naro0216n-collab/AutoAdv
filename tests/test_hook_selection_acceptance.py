"""Acceptance contracts for user-selected LunaHOOK candidates across sessions."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from tests.g2p_support import TEST_G2P, FakeG2P

from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import HookSelection, Profile, SPEAKER_ONLY_RULE
from autoadv.storage import Catalog, load, save


def source(code: str, context: int, address: int = 0x100) -> ThreadSource:
    return ThreadSource(101, address, context, context + 1, code, code)


def output(controller: Controller, hook: ThreadSource, text: str, seq: int):
    return controller.observe(generation='host', connection=7, sequence=seq,
                              source=hook, text=text, pid=101, received_ns=seq * 100_000_000)


class SelectedHookContracts(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.s = source('A', 1), source('B', 3), source('S', 5)
        self.p = Profile('p', 'P', hooks=[HookSelection('A', 1, 2),
                                         HookSelection('B', 3, 4),
                                         HookSelection('S', 5, 6)],
                         body_source=self.a.identity,
                         source_rules={self.s.identity: [SPEAKER_ONLY_RULE]})
        self.sent = []
        self.c = Controller(lambda *_: self.sent.append('advance'), lambda _: True, TEST_G2P)

    def begin(self):
        self.c.begin(self.p, Target(101, 400), 'host', 7)
        self.c.permit(True)

    def test_selected_body_source_is_only_dialogue_owner(self):
        self.begin()
        self.assertIsNone(output(self.c, self.b, 'duplication from B', 1))
        token = output(self.c, self.a, 'first line', 2)
        self.assertIsNotNone(token)
        self.c.presentation_event(token, 'text_elapsed')
        self.assertTrue(self.c.try_advance())
        self.assertIsNone(output(self.c, self.b, 'duplication from B', 3))
        self.assertEqual(self.c.utterance.raw, 'first line')
        self.assertFalse(self.c.try_advance())
        self.assertEqual(len(self.sent), 1)
        self.assertIsNotNone(output(self.c, self.a, 'next line', 4))

    def test_unselected_source_never_becomes_body_owner(self):
        self.begin()
        self.assertIsNone(output(self.c, source('X', 7), 'unselected', 1))
        self.assertIsNone(output(self.c, self.b, 'also unselected', 2))
        self.assertIsNotNone(output(self.c, self.a, 'valid', 3))

    def test_speaker_provider_works_with_explicit_body_provider(self):
        self.p.speaker_source = self.s.identity
        self.begin()
        def e(src, seq, text):
            return HookEvent('output', 'host', 7, seq, seq * 100_000_000, 101, src, text)
        ticket = self.c.observe_group([e(self.b, 1, 'duplicate'), e(self.s, 2, '京'),
                                       e(self.a, 3, 'おはよう')])
        self.assertIsNotNone(ticket)
        self.assertEqual(self.c.utterance.speaker, '京')
        self.assertEqual(self.c.utterance.raw, 'おはよう')

    def test_saved_sources_restore_without_pid_or_thread_address(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            catalog = Catalog()
            game = catalog.create_game('Some game')
            profile = game.profiles[game.default_profile]
            profile.hooks = list(self.p.hooks)
            profile.body_source = self.b.identity
            profile.speaker_source = self.s.identity
            profile.source_rules[self.s.identity] = [SPEAKER_ONLY_RULE]
            save(catalog, path)
            restored = load(path).games[game.id].profiles[profile.id]
            self.assertEqual(restored.body_source, self.b.identity)
            self.assertEqual(restored.speaker_source, self.s.identity)
            self.assertNotIn('address', path.read_text(encoding='utf-8'))
            c = Controller(lambda *_: None, lambda _: True, TEST_G2P)
            c.begin(restored, Target(101, 401), 'new-host', 1)
            self.assertTrue(next(h for h in restored.hooks if h.identity == self.b.identity)
                            .matches(source('B', 3, address=0xBEEF)))

    def test_switching_sessions_revokes_prior_input(self):
        self.begin()
        original = output(self.c, self.a, 'old', 1)
        self.p.body_source = self.b.identity
        self.c.begin(self.p, Target(101, 400), 'host', 7)
        self.assertFalse(self.c.presentation_event(original, 'text_elapsed'))
        self.assertFalse(self.c.try_advance())
        self.assertIsNotNone(output(self.c, self.b, 'new owner', 2))


@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'PySide6 required for native GUI acceptance')
class HookCandidateGuiAcceptance(unittest.TestCase):
    def test_normal_outputs_appear_automatically_and_recovery_search_is_separate(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        catalog.create_game('Native dialogue and extended discovery')

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertEqual([window.tabs.tabText(i) for i in range(3)], ['プレイ', 'ゲーム', '設定'])
                self.assertFalse(hasattr(window, 'btn_find'))
                self.assertFalse(hasattr(window, 'attach'))
                window.application.select_primary_target(101, 0x1234)
                commands = Mock()
                window.application.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                self.assertEqual(window.application.activate_selected_target(), 'connecting')
                window.application.handle_hook_event(HookEvent('connected', 'host', 7, 1, 100, process_id=101))
                existing = source('A', 1)
                window.application.handle_hook_event(HookEvent('output', 'host', 7, 2, 200,
                                         process_id=101, source=existing, text='台詞A'))
                QApplication.processEvents()
                self.assertEqual(window.source_table.rowCount(), 1)
                self.assertEqual(window.source_table.item(0, 0).text(), '台詞A')
                self.assertEqual(window.source_table.item(0, 1).text(), '—')
                self.assertFalse(window.capture_hint.isVisible())
                commands.reset_mock()
                window.btn_recovery_search.click()
                commands.assert_any_call('find_hooks', pid=101, boundary_module='',
                                         parameters={'maxRecords': 64})
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_trial_hook_is_persisted_only_after_real_output_is_used_and_committed(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        catalog.create_game('Recovery')

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.application.select_primary_target(101, 0x1234)
                commands = Mock()
                window.application.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                self.assertEqual(window.application.activate_selected_target(), 'connecting')
                window.application.handle_hook_event(HookEvent('connected', 'host', 7, 1, 100, process_id=101))
                window.btn_recovery_search.click()
                window.application.handle_hook_event(HookEvent('hook_found', 'host', 7, 2, 200,
                                         process_id=101, detail='B', text='探索試料'))
                commands.assert_any_call('insert_hook', pid=101, code='B')
                self.assertEqual(window.current_profile().inserted_hooks, [])
                found = source('B', 5)
                window.application.handle_hook_event(HookEvent('output', 'host', 7, 3, 300,
                                         process_id=101, source=found, text='台詞B'))
                QApplication.processEvents()
                idx = next(i for i in range(window.capture_body_source.count())
                           if window.capture_body_source.itemData(i) == (found.identity, found.address))
                window.capture_body_source.setCurrentIndex(idx)
                self.assertEqual(window.current_profile().hooks, [])
                self.assertEqual(window.current_profile().inserted_hooks, [])
                self.assertTrue(window.btn_commit_capture.isEnabled())
                window.btn_commit_capture.click()
                self.assertEqual([h.code for h in window.current_profile().hooks], ['B'])
                self.assertEqual(window.current_profile().body_source, found.identity)
                self.assertEqual(window.current_profile().inserted_hooks, ['B'])
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_mixed_sources_can_supply_speaker_and_body_independently_in_final_preview(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        catalog.create_game('Field composition')

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.application.select_primary_target(101, 0x1234)
                window.application.hook_host = SimpleNamespace(command=Mock(), child=SimpleNamespace(poll=lambda: None))
                self.assertEqual(window.application.activate_selected_target(), 'connecting')
                window.application.handle_hook_event(HookEvent('connected', 'host', 3, 1, 100, process_id=101))
                speaker_source = source('S', 1)
                body_source = source('T', 5, address=0x200)
                speaker_raw = 'きぬ　（０４０６）■■壊れた本文■■'
                body_raw = '???「あれカレー屋を荒らしてくれた女だ」'
                window.application.handle_hook_event(HookEvent('output', 'host', 3, 2, 200,
                                         process_id=101, source=speaker_source, text=speaker_raw))
                window.application.handle_hook_event(HookEvent('output', 'host', 3, 3, 220,
                                         process_id=101, source=body_source, text=body_raw))
                QApplication.processEvents()
                body_index = next(i for i in range(window.capture_body_source.count())
                                  if window.capture_body_source.itemData(i) ==
                                  (body_source.identity, body_source.address))
                speaker_index = next(i for i in range(window.capture_speaker_source.count())
                                     if window.capture_speaker_source.itemData(i) ==
                                     (speaker_source.identity, speaker_source.address))
                window.capture_body_source.setCurrentIndex(body_index)
                window.capture_speaker_source.setCurrentIndex(speaker_index)
                QApplication.processEvents()
                # Pre-selection observations are candidate discovery only. Final
                # composition starts from new output after Luna synchronization.
                self.assertEqual(window.capture_result_table.rowCount(), 0)
                window.application.handle_hook_event(HookEvent('output', 'host', 3, 4, 300,
                                         process_id=101, source=speaker_source, text=speaker_raw))
                window.application.handle_hook_event(HookEvent('output', 'host', 3, 5, 320,
                                         process_id=101, source=body_source, text=body_raw))
                from PySide6.QtTest import QTest
                QTest.qWait(120)
                QApplication.processEvents()
                self.assertEqual(window.capture_result_table.rowCount(), 1)
                self.assertEqual(window.capture_result_table.item(0, 0).text(), 'きぬ')
                self.assertEqual(window.capture_result_table.item(0, 1).text(),
                                 'あれカレー屋を荒らしてくれた女だ')
                window.btn_commit_capture.click()
                profile = window.current_profile()
                self.assertEqual(profile.body_source, body_source.identity)
                self.assertEqual(profile.speaker_source, speaker_source.identity)
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_same_source_can_supply_speaker_and_body_and_format_preview_is_inline(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        catalog.create_game('Combined')
        raw = 'きぬ　（０４０６）あれカレー屋を荒らしてくれた女だ'

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.application.select_primary_target(101, 0x1234)
                window.application.hook_host = SimpleNamespace(command=Mock(), child=SimpleNamespace(poll=lambda: None))
                self.assertEqual(window.application.activate_selected_target(), 'connecting')
                window.application.handle_hook_event(HookEvent('connected', 'host', 7, 1, 100, process_id=101))
                combined = source('C', 1)
                window.application.handle_hook_event(HookEvent('output', 'host', 7, 2, 200,
                                         process_id=101, source=combined, text=raw))
                QApplication.processEvents()
                window.source_table.selectRow(0)
                QApplication.processEvents()
                self.assertEqual(window.source_extraction_preview.rowCount(), 1)
                self.assertEqual(window.source_extraction_preview.item(0, 1).text(), 'きぬ')
                self.assertEqual(window.source_extraction_preview.item(0, 2).text(), 'あれカレー屋を荒らしてくれた女だ')
                idx = next(i for i in range(window.capture_body_source.count())
                           if window.capture_body_source.itemData(i) == (combined.identity, combined.address))
                window.capture_body_source.setCurrentIndex(idx)
                QApplication.processEvents()
                self.assertEqual(window.source_table.item(0, 1).text(), '本文・話者')
                self.assertEqual(window.capture_result_table.item(0, 0).text(), 'きぬ')
                self.assertEqual(window.capture_result_table.item(0, 1).text(), 'あれカレー屋を荒らしてくれた女だ')
                self.assertFalse(hasattr(window, '_capture_role_buttons'))
                self.assertFalse(hasattr(window, 'route_table'))
            finally:
                window.application.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)


if __name__ == '__main__':
    unittest.main()
