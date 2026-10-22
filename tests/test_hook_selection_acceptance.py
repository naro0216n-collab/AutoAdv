"""Acceptance contracts for user-selected LunaHOOK candidates across sessions."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import HookSelection, Profile
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
                                         HookSelection('S', 5, 6, 'speaker')])
        self.sent = []
        self.c = Controller(lambda *_: self.sent.append('advance'), lambda _: True)

    def begin(self, present):
        self.c.begin(self.p, Target(101, 400), 'host', 7, tuple(present))
        self.c.permit(True)

    def test_prioritized_available_hook_is_only_dialogue_owner(self):
        self.begin((self.b.identity, self.a.identity))
        self.assertEqual(self.c.active_text_source, self.a.identity)
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

    def test_fallback_available_at_session_start_does_not_switch_mid_line(self):
        self.begin((self.b.identity,))
        self.assertEqual(self.c.active_text_source, self.b.identity)
        t = output(self.c, self.b, 'line', 1)
        self.assertIsNone(output(self.c, self.a, 'late high priority duplicate', 2))
        self.assertEqual(self.c.token, t)
        self.c.presentation_event(t, 'text_elapsed')
        self.assertTrue(self.c.try_advance())
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(output(self.c, self.a, 'late duplicate', 3))

    def test_no_observed_source_then_first_selected_output_owns_session(self):
        self.begin(())
        self.assertIsNone(self.c.active_text_source)
        self.assertIsNone(output(self.c, source('X', 7), 'unselected', 1))
        self.assertIsNone(self.c.active_text_source)
        self.assertIsNotNone(output(self.c, self.b, 'valid', 2))
        self.assertEqual(self.c.active_text_source, self.b.identity)
        self.assertIsNone(output(self.c, self.a, 'not owner', 3))

    def test_speaker_pairing_works_with_prioritized_text(self):
        self.p.speaker_links = {self.a.identity: self.s.identity}
        self.begin((self.a.identity, self.b.identity, self.s.identity))
        def e(source, seq, text):
            return HookEvent('output', 'host', 7, seq, seq * 100_000_000, 101, source, text)
        ticket = self.c.observe_group([e(self.b, 1, 'duplicate'), e(self.s, 2, '京'),
                                       e(self.a, 3, 'おはよう')])
        self.assertIsNotNone(ticket)
        self.assertEqual(self.c.utterance.speaker, '京')
        self.assertEqual(self.c.utterance.raw, 'おはよう')

    def test_saved_hook_priority_restores_without_pid_or_thread_address(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'settings.json'
            catalog = Catalog()
            game = catalog.create_game('Some game')
            profile = game.profiles[game.default_profile]
            profile.hooks = list(self.p.hooks)
            profile.hooks[0], profile.hooks[1] = profile.hooks[1], profile.hooks[0]
            profile.speaker_links = {profile.hooks[1].identity: self.s.identity}
            save(catalog, path)
            restored = load(path).games[game.id].profiles[profile.id]
            self.assertEqual([h.identity for h in restored.hooks], [self.b.identity, self.a.identity, self.s.identity])
            self.assertEqual(restored.speaker_links, profile.speaker_links)
            self.assertNotIn('address', path.read_text(encoding='utf-8'))
            c = Controller(lambda *_: None, lambda _: True)
            c.begin(restored, Target(101, 401), 'new-host', 1,
                    (self.a.identity, self.b.identity))
            self.assertEqual(c.active_text_source, self.b.identity)
            self.assertTrue(restored.hooks[0].matches(source('B', 3, address=0xBEEF)))

    def test_switching_sessions_revokes_prior_selection_and_input(self):
        self.begin((self.a.identity,))
        original = output(self.c, self.a, 'old', 1)
        self.c.begin(self.p, Target(101, 400), 'host', 7, (self.b.identity,))
        self.assertFalse(self.c.presentation_event(original, 'text_elapsed'))
        self.assertFalse(self.c.try_advance())
        self.assertEqual(self.c.active_text_source, self.b.identity)


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
                window.pid.setValue(101)
                commands = Mock()
                window.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                window.on_hook(HookEvent('connected', 'host', 7, 1, 100, process_id=101))
                existing = source('A', 1)
                window.on_hook(HookEvent('output', 'host', 7, 2, 200,
                                         process_id=101, source=existing, text='台詞A'))
                self.assertEqual(window.source_table.rowCount(), 1)
                self.assertEqual(window.source_table.item(0, 1).text(), '台詞A')
                self.assertFalse(window.capture_hint.isVisible())
                commands.reset_mock()
                window.btn_recovery_search.click()
                commands.assert_any_call('find_hooks', pid=101, boundary_module='',
                                         parameters={'maxRecords': 64})
            finally:
                window.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_trial_hook_is_persisted_only_after_real_output_selection_and_recipe_commit(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        catalog.create_game('Recovery')

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.pid.setValue(101)
                commands = Mock()
                window.hook_host = SimpleNamespace(command=commands, child=SimpleNamespace(poll=lambda: None))
                window.on_hook(HookEvent('connected', 'host', 7, 1, 100, process_id=101))
                window.btn_recovery_search.click()
                window.on_hook(HookEvent('hook_found', 'host', 7, 2, 200,
                                         process_id=101, detail='B', text='探索試料'))
                commands.assert_any_call('insert_hook', pid=101, code='B')
                self.assertEqual(window.current_profile().inserted_hooks, [])
                found = source('B', 5)
                window.on_hook(HookEvent('output', 'host', 7, 3, 300,
                                         process_id=101, source=found, text='台詞B'))
                buttons = window._capture_role_buttons[(found.identity, found.address)]
                buttons['text'].click()
                self.assertEqual(window.current_profile().hooks, [])
                self.assertEqual(window.current_profile().inserted_hooks, [])
                self.assertTrue(window.btn_commit_capture.isEnabled())
                window.btn_commit_capture.click()
                self.assertEqual([h.code for h in window.current_profile().hooks], ['B'])
                self.assertEqual(window.current_profile().inserted_hooks, ['B'])
            finally:
                window.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_one_dialogue_and_one_speaker_are_implicitly_paired_in_preview(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        catalog.create_game('Pairing')

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.pid.setValue(101)
                window.hook_host = SimpleNamespace(command=Mock(), child=SimpleNamespace(poll=lambda: None))
                window.on_hook(HookEvent('connected', 'host', 3, 1, 100, process_id=101))
                text_source = source('T', 1)
                speaker_source = source('S', 5, address=0x200)
                window.on_hook(HookEvent('output', 'host', 3, 2, 200,
                                         process_id=101, source=text_source, text='本文'))
                window.on_hook(HookEvent('output', 'host', 3, 3, 220,
                                         process_id=101, source=speaker_source, text='京'))
                window.flush_capture_outputs()
                window._capture_role_buttons[(text_source.identity, text_source.address)]['text'].click()
                window._capture_role_buttons[(speaker_source.identity, speaker_source.address)]['speaker'].click()
                self.assertFalse(window.route_table.isVisible())
                self.assertIn('京', window.capture_dialogue_preview.toPlainText())
                self.assertIn('本文', window.capture_dialogue_preview.toPlainText())
                window.btn_commit_capture.click()
                profile = window.current_profile()
                text_hook = next(h for h in profile.hooks if h.role == 'text')
                speaker_hook = next(h for h in profile.hooks if h.role == 'speaker')
                self.assertEqual(profile.speaker_links[text_hook.identity], speaker_hook.identity)
            finally:
                window.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_multiple_dialogue_sources_expose_route_priority_only_when_needed(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        catalog.create_game('Routes')

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                window.pid.setValue(101)
                window.hook_host = SimpleNamespace(command=Mock(), child=SimpleNamespace(poll=lambda: None))
                window.on_hook(HookEvent('connected', 'host', 7, 1, 100, process_id=101))
                a, b = source('A', 1), source('B', 3, address=0x300)
                window.on_hook(HookEvent('output', 'host', 7, 2, 200, process_id=101, source=a, text='A'))
                window.on_hook(HookEvent('output', 'host', 7, 3, 300, process_id=101, source=b, text='B'))
                window._capture_role_buttons[(a.identity, a.address)]['text'].click()
                self.assertFalse(window.route_table.isVisible())
                window._capture_role_buttons[(b.identity, b.address)]['text'].click()
                self.assertTrue(window.route_table.isVisible())
                self.assertEqual(window.route_table.rowCount(), 2)
                window.move_capture_route(b.identity, -1)
                self.assertEqual([h.code for h in window.application.capture.profile.hooks if h.role == 'text'], ['B', 'A'])
                self.assertEqual(window.current_profile().hooks, [])  # still a draft
            finally:
                window.hook_host = None
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)


if __name__ == '__main__':
    unittest.main()
