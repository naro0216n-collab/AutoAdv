"""Acceptance contracts for user-selected LunaHOOK candidates across sessions."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    def test_compare_choose_save_reorder_and_restore_in_real_widgets(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        game = catalog.create_game('Sample game')
        events = []
        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if w.windowTitle().startswith('AutoAdv') and getattr(w, 'catalog', None) is catalog)
            self.assertIsNotNone(window.current_profile())
            try:
                window.pid.setValue(101)
                window.on_hook(HookEvent('connected', 'host', 7, 1, 100, process_id=101))
                for i, hook in enumerate((source('A', 1), source('B', 3)), 2):
                    window.on_hook(HookEvent('output', 'host', 7, i, i * 100,
                                            process_id=101, source=hook, text=f'raw-{hook.hook_code}'))
                self.assertEqual(window.source_table.rowCount(), 2)
                self.assertIn('raw-A', window.source_table.item(0, 0).text())
                self.assertIn('raw-B', window.source_table.item(1, 0).text())
                window.source_table.selectRow(0)
                window.select_hook('text')
                window.source_table.selectRow(1)
                window.select_hook('text')
                self.assertEqual([h.code for h in window.current_profile().hooks], ['A', 'B'])
                window.saved_hook_choice.setCurrentIndex(1)
                window.move_saved_hook(-1)
                self.assertEqual([h.code for h in window.current_profile().hooks], ['B', 'A'])
                window.on_hook(HookEvent('disconnected', 'host', 7, 4, 400, process_id=101))
                self.assertIn('0/2', window.hook_reconnect.text())
                window.on_hook(HookEvent('connected', 'newhost', 8, 5, 500, process_id=101))
                window.on_hook(HookEvent('created', 'newhost', 8, 6, 600, process_id=101, source=source('B', 3, 0xBEE)))
                self.assertIn('1/2', window.hook_reconnect.text())
                self.assertEqual(window.saved_hook_choice.count(), 2)
                self.assertEqual(window.source_table.rowCount(), 1)
                self.assertIn('text #1', window.source_table.item(0, 1).text())
                # Same saved identity appearing in two native threads is not
                # grounds for choosing one automatically or sending game input.
                window.on_hook(HookEvent('created', 'newhost', 8, 7, 700,
                              process_id=101, source=source('B', 3, 0xC0DE)))
                self.assertIn('ambiguous', window.hook_reconnect.text())
                self.assertEqual(window.source_table.rowCount(), 2)
                self.assertIsNone(window.chosen_source())
                window.hwnd.setText('0x1234')
                window.begin_monitor()
                self.assertIsNone(window.controller.profile)
                self.assertIn('ambiguous', window.log.toPlainText())
                events.append('complete')
            finally:
                window.close()
            return 0
        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)
        self.assertEqual(events, ['complete'])


if __name__ == '__main__':
    unittest.main()
