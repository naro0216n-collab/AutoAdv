"""Headless Qt integration of current model -> GUI -> Controller presentation path."""
import os
import unittest
from unittest.mock import patch
from autoadv.conversation import Target
from autoadv.model import AdvanceMode
from autoadv.storage import Catalog


@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Qt runtime required')
class TwoModeGuiTests(unittest.TestCase):
    def run_case(self, *, mode, tts, fallback=False, native=None, check):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        catalog = Catalog()
        game = catalog.create_game('GUI game')
        profile = game.profiles[game.default_profile]
        profile.source = 'ocr'
        # This case verifies game-specific controls, so do not leave these
        # fields inherited from the catalog common defaults.
        for field in ('advance_mode', 'default_tts', 'default_voice',
                      'uncertain_text_fallback', 'reading_ms', 'per_character_wait_ms'):
            if field in profile.inherit_common:
                profile.inherit_common.remove(field)
        profile.advance_mode = mode
        profile.default_tts = tts
        profile.default_voice = 'speaker-voice'
        profile.uncertain_text_fallback = fallback
        profile.reading_ms = 200
        profile.per_character_wait_ms = 100
        sent = []

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertTrue(window.advance_text_button.isVisible())
                self.assertTrue(window.advance_audio_button.isVisible())
                self.assertEqual(window.current_advance_mode(), mode)
                app = window.application
                c = app.controller
                c.input_send = lambda *_: sent.append('advance')
                c.target_ready = lambda *_: True
                app.begin_runtime(profile, Target(100, 200), 'ocr', 1)
                app.set_auto_advance(True)
                token = app.observe_runtime_ocr(sequence=1, text='「こんにちは！」', pid=100, session=app.runtime_state().session)
                requests = []
                window.application.start_voice = lambda token, reading, voice: requests.append((token, reading, voice))
                if native is not None:
                    window.application._native_audio = native
                window.display_utterance(token)
                self.assertEqual(c.utterance.character_count, 5)
                self.assertIn('500 ms', window.wait_preview.text())
                self.assertIn('こんにちは', window.preview.toPlainText())
                self.assertEqual(window.advance_authorization.text(), '自動送り ON')
                self.assertFalse(window.play_issue.isVisible())
                check(window, c, token, requests, sent)
                window.application._native_audio = None
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_text_mode_tts_requires_both_conditions(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][2], 'speaker-voice')
            window.application.runtime_presentation_event(token, 'text_elapsed')
            self.assertFalse(c.completed)
            window.application.runtime_presentation_event(token, 'tts_finished', success=True)
            self.assertEqual(c.presentation_reason, 'tts_and_text')
            self.assertEqual(sent, ['advance'])
            window.application.runtime_presentation_event(token, 'tts_finished', success=True)
            self.assertEqual(sent, ['advance'])
        self.run_case(mode=AdvanceMode.TEXT, tts=True, check=check)

    def test_audio_mode_tts_only_waits_for_playback(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(len(requests), 1)
            self.assertFalse(window.application.native_audio_available)
            window.application.runtime_presentation_event(token, 'fallback_elapsed')
            self.assertEqual(sent, [])
            window.application.runtime_presentation_event(token, 'tts_finished', success=True)
            self.assertEqual(c.presentation_reason, 'tts')
            self.assertEqual(sent, ['advance'])
        self.run_case(mode=AdvanceMode.AUDIO, tts=True, fallback=True, check=check)

    def test_audio_uncertain_fallback_is_opt_in(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(requests, [])
            window.application.runtime_presentation_event(token, 'fallback_elapsed')
            self.assertEqual(c.presentation_reason, 'uncertain_text_fallback')
            self.assertEqual(sent, ['advance'])
        self.run_case(mode=AdvanceMode.AUDIO, tts=False, fallback=True, check=check)

    def test_audio_without_fallback_returns_control_to_user(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(requests, [])
            window.application.runtime_presentation_event(token, 'fallback_elapsed')
            self.assertEqual(sent, [])
            self.assertFalse(c.completed)
            self.assertEqual(c.pause_reason, 'native_inconclusive_hold')
            self.assertTrue(c.authorized)
        self.run_case(mode=AdvanceMode.AUDIO, tts=False, fallback=False, check=check)
