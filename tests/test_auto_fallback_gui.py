"""Headless Qt contract for the two product advance-mode controls.

Runtime completion semantics belong to Controller/Application domain tests.  This
module verifies only that the GUI presents the persisted product settings; it
does not call Application-internal transition methods to simulate a runtime.
"""
import os
import unittest
from unittest.mock import patch

from tests.g2p_support import TEST_G2P

from autoadv.model import AdvanceMode
from autoadv.storage import Catalog


@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Qt runtime required')
class TwoModeGuiTests(unittest.TestCase):
    def run_case(self, *, mode, tts, fallback):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui

        catalog = Catalog()
        game = catalog.create_game('GUI game')
        profile = game.profiles[game.default_profile]
        profile.source = 'ocr'
        for field in ('advance_mode', 'default_tts', 'uncertain_text_fallback'):
            if field in profile.inherit_common:
                profile.inherit_common.remove(field)
        profile.advance_mode = mode
        profile.default_tts = tts
        profile.uncertain_text_fallback = fallback

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertTrue(window.advance_text_button.isVisible())
                self.assertTrue(window.advance_audio_button.isVisible())
                self.assertEqual(window.current_advance_mode(), mode)
                self.assertEqual(window.default_tts.isChecked(), tts)
                self.assertEqual(window.uncertain_text_fallback.isChecked(), fallback)
                self.assertFalse(hasattr(window.application, 'begin_runtime'))
                self.assertFalse(hasattr(window.application, 'runtime_presentation_event'))
                self.assertFalse(hasattr(window.application, 'handle_runtime_ocr_output'))
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'), \
             patch('autoadv.application.default_g2p', return_value=TEST_G2P):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_text_mode_controls_reflect_profile(self):
        self.run_case(mode=AdvanceMode.TEXT, tts=True, fallback=False)

    def test_audio_mode_controls_reflect_profile(self):
        self.run_case(mode=AdvanceMode.AUDIO, tts=False, fallback=True)
