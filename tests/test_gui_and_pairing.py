"""UI integration and source-scoped HOOK pairing in the rebuilt application."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from autoadv.conversation import Controller, Target
from autoadv.model import Profile, HookSelection


class HookPairingTests(unittest.TestCase):
    def test_selected_speaker_source_pairing_across_interleaved_host_events(self):
        p = Profile('id', 'paired')
        p.hooks = [HookSelection('T', 1, 2, 'text'),
                   HookSelection('S1', 3, 4, 'speaker'),
                   HookSelection('S2', 5, 6, 'speaker')]
        p.speaker_links = {p.hooks[0].identity: p.hooks[2].identity}
        c = Controller(lambda *_: None, lambda _: True)
        c.begin(p, Target(123, 456), 'generation', 1)
        def src(selection):
            return SimpleNamespace(hook_code=selection.code, context=selection.context,
                                   context2=selection.context2, identity=selection.identity)
        self.assertIsNone(c.observe(generation='generation', connection=1,
                     sequence=1, source=src(p.hooks[1]), text='wrong', pid=123, received_ns=1_000_000_000))
        self.assertIsNone(c.observe(generation='generation', connection=1,
                     sequence=2, source=src(p.hooks[2]), text='花子', pid=123, received_ns=1_100_000_000))
        self.assertIsNotNone(c.observe(generation='generation', connection=1,
                     sequence=5, source=src(p.hooks[0]), text='台詞', pid=123, received_ns=1_200_000_000))
        self.assertEqual(c.utterance.speaker, '花子')
        self.assertIsNone(c.observe(generation='generation', connection=1,
                     sequence=6, source=src(p.hooks[2]), text='古い', pid=123, received_ns=2_000_000_000))
        c.observe(generation='generation', connection=1,
                  sequence=10, source=src(p.hooks[0]), text='次', pid=123, received_ns=5_100_000_000)
        self.assertEqual(c.utterance.speaker, '')

    def test_invalid_speaker_link_is_rejected_in_one_profile_config(self):
        p = Profile('id', 'invalid', hooks=[HookSelection('T', 1, 2)])
        p.speaker_links = {p.hooks[0].identity: 'nonexistent|2|3'}
        with self.assertRaises(ValueError):
            p.validate()


class WindowsGuiInitialization(unittest.TestCase):
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
