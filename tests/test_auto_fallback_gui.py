"""Windows Qt acceptance of the actual AUTO and native/TTS presentation wiring."""
from __future__ import annotations
import os
import unittest
from unittest.mock import patch
from autoadv.conversation import Target
from autoadv.model import AudioMode, Profile
from autoadv.storage import Catalog


@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Qt runtime is required')
class AutoRouteGuiTests(unittest.TestCase):
    def test_auto_uses_tts_without_asr_then_advances_only_after_playback(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        catalog = Catalog()
        game = catalog.create_game('No voice game')
        profile = game.profiles[game.default_profile]
        profile.source = 'ocr'
        profile.default_mode = AudioMode.AUTO
        profile.default_voice = 'narrator'
        profile.native_wait_ms = 0
        inputs = []

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                c = window.controller
                c.input_send = lambda *_: inputs.append('sent')
                c.preflight = lambda *_: True
                c.begin(profile, Target(100, 200), 'ocr', 1)
                c.permit(True)
                ticket = c.observe_ocr(sequence=1, text='「おはよう」', pid=100, session=c.session)
                requests = []
                window.jobs.start = lambda *args: requests.append(args)
                window.present(ticket)
                self.assertEqual(inputs, [])
                window.auto_fallback(ticket)
                window.auto_fallback(ticket)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][3], 'narrator')
                # Completion is only the actual playback callback, not synthesis request.
                self.assertFalse(c.completed)
                window.on_voice(ticket, True, '')
                self.assertEqual(inputs, ['sent'])
                window.on_voice(ticket, True, '')
                self.assertEqual(inputs, ['sent'])
                # Token from old game can never act on the new session.
                c.begin(profile, Target(101, 201), 'ocr', 1)
                window.auto_fallback(ticket)
                self.assertEqual(len(requests), 1)
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_late_native_voice_cancels_auto_tts_and_waits_for_native_end(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        catalog = Catalog()
        game = catalog.create_game('Mixed game')
        p = game.profiles[game.default_profile]
        p.source = 'ocr'
        p.default_mode = AudioMode.AUTO
        p.default_voice = 'narrator'
        sent = []

        class NativeEvidence:
            def __init__(self):
                self.started = []
                self.cancelled = 0
            def begin(self, *args): self.started.append(args)
            def cancel(self): self.cancelled += 1
            def close(self): pass

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                c = window.controller
                c.input_send = lambda *_: sent.append('advance')
                c.preflight = lambda *_: True
                c.begin(p, Target(100, 200), 'ocr', 1)
                c.permit(True)
                ticket = c.observe_ocr(sequence=1, text='原音のある台詞', pid=100, session=c.session)
                native = NativeEvidence()
                window.native_audio = native
                voice_requests = []
                cancelled = []
                window.jobs.start = lambda *a: voice_requests.append(a)
                window.jobs.cancel = lambda: cancelled.append('cancel')
                window.present(ticket)
                self.assertEqual(native.started[0][0], ticket)
                window.auto_fallback(ticket)
                self.assertEqual(len(voice_requests), 1)
                window.on_native(ticket, 'started', '')
                self.assertTrue(window.native_started)
                self.assertEqual(len(cancelled), 2)  # present and native takeover
                window.on_voice(ticket, False, 'TTS cancelled')
                self.assertEqual(sent, [])
                window.on_native(ticket, 'completed', '')
                self.assertEqual(sent, ['advance'])
                window.on_native(ticket, 'completed', '')
                self.assertEqual(sent, ['advance'])
                # Closing must not depend on the fake capture object being an actual process.
                window.native_audio = None
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)

    def test_native_mode_without_recognizer_never_uses_tts_fallback(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from autoadv.gui import run_gui
        catalog = Catalog()
        game = catalog.create_game('Original voice game')
        p = game.profiles[game.default_profile]
        p.source = 'ocr'
        p.default_mode = AudioMode.NATIVE
        p.default_voice = 'narrator'

        def check_widgets(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                c = window.controller
                c.begin(p, Target(100, 200), 'ocr', 1)
                c.permit(True)
                ticket = c.observe_ocr(sequence=1, text='原音', pid=100, session=c.session)
                calls = []
                window.jobs.start = lambda *args: calls.append(args)
                window.present(ticket)
                self.assertFalse(c.authorized)
                self.assertEqual(c.reason, 'native_audio_unavailable')
                self.assertEqual(calls, [])
            finally:
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', check_widgets):
                self.assertEqual(run_gui(), 0)
