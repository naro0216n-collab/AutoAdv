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
                c = window.controller
                c.input_send = lambda *_: sent.append('advance')
                c.preflight = lambda *_: True
                c.begin(profile, Target(100, 200), 'ocr', 1)
                c.permit(True)
                token = c.observe_ocr(sequence=1, text='「こんにちは！」', pid=100, session=c.session)
                requests = []
                window.jobs.start = lambda *args: requests.append(args)
                window.native_audio = native
                window.display_utterance(token)
                self.assertEqual(c.utterance.character_count, 5)
                self.assertIn('500ms', window.wait_preview.text())
                self.assertIn('こんにちは', window.overview_capture.text())
                self.assertIn('許可中', window.overview_advance.text())
                check(window, c, token, requests, sent)
                window.native_audio = None
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)

    def test_text_mode_tts_requires_both_conditions(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][3], 'speaker-voice')
            window.presentation_event(token, 'text_elapsed')
            self.assertFalse(c.completed)
            window.on_voice(token, True, '')
            self.assertEqual(c.presentation_reason, 'tts_and_text')
            self.assertEqual(sent, ['advance'])
            window.on_voice(token, True, '')
            self.assertEqual(sent, ['advance'])
        self.run_case(mode=AdvanceMode.TEXT, tts=True, check=check)

    def test_audio_mode_tts_only_waits_for_playback(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(len(requests), 1)
            self.assertIsNone(window.native_audio)
            window.fallback_due(token)
            self.assertEqual(sent, [])
            window.on_voice(token, True, '')
            self.assertEqual(c.presentation_reason, 'tts')
            self.assertEqual(sent, ['advance'])
        self.run_case(mode=AdvanceMode.AUDIO, tts=True, fallback=True, check=check)

    def test_audio_uncertain_fallback_is_opt_in(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(requests, [])
            window.fallback_due(token)
            self.assertEqual(c.presentation_reason, 'uncertain_text_fallback')
            self.assertEqual(sent, ['advance'])
        self.run_case(mode=AdvanceMode.AUDIO, tts=False, fallback=True, check=check)

    def test_audio_without_fallback_returns_control_to_user(self):
        def check(window, c, token, requests, sent):
            self.assertEqual(requests, [])
            window.fallback_due(token)
            self.assertEqual(sent, [])
            self.assertFalse(c.completed)
            self.assertEqual(c.pause_reason, 'native_inconclusive_manual_handoff')
            self.assertFalse(c.authorized)
        self.run_case(mode=AdvanceMode.AUDIO, tts=False, fallback=False, check=check)
