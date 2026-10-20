"""Offscreen Qt acceptance: normal settings do not need an external JSON editor.

Linux Fast CI may omit Qt; Windows Adapter CI installs the live GUI extra.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import importlib.util


@unittest.skipUnless(importlib.util.find_spec('PySide6') is not None,'PySide6 is not installed')
class GameSettingsGUI(unittest.TestCase):
    def test_game_monitoring_can_be_saved_from_widgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ,{'APPDATA':tmp,'QT_QPA_PLATFORM':'offscreen'}):
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QApplication,QMainWindow
                from autoadv.services import AutoAdvService
                from autoadv.gui import run_gui
                from autoadv.text import GameTextConfig
                app=QApplication.instance() or QApplication([])
                settings=Path(tmp)/'settings.json'
                service=AutoAdvService(settings_file=settings)
                service.save_game('つよきす',GameTextConfig())
                for file in ('capture.dll','asr.onnx','tokens.txt','dialogue.png','ready.png'):
                    (Path(tmp)/file).write_bytes(b'file')
                failures=[]
                def inspect():
                    window=next((x for x in app.topLevelWidgets() if isinstance(x,QMainWindow)),None)
                    try:
                        self.assertIsNotNone(window)
                        window.monitor_fields['pid'].setText('123')
                        window.monitor_fields['hwnd'].setText('456')
                        for key,name in [('loopback_helper','capture.dll'),('asr_model','asr.onnx'),
                                         ('asr_tokens','tokens.txt'),('log_path','session.jsonl')]:
                            window.monitor_fields[key].setText(str(Path(tmp)/name))
                        window.markers.item(0,1).setText(str(Path(tmp)/'dialogue.png'))
                        window.markers.item(1,1).setText(str(Path(tmp)/'ready.png'))
                        window.save_monitor_profile()
                        profile=window.service.monitor_path()
                        self.assertIsNotNone(profile)
                        self.assertTrue(profile.is_file())
                        self.assertEqual(json.loads(profile.read_text())['pid'],123)
                        self.assertFalse(window.service.autoplay_allowed)
                        self.assertFalse(window.allow.isEnabled())
                        self.assertFalse(window.monitor_dirty)
                    except BaseException as exc:
                        failures.append(exc)
                    finally:
                        if window is not None:window.close()
                        app.quit()
                QTimer.singleShot(0,inspect)
                with patch('autoadv.gui.AutoAdvService',lambda:AutoAdvService(settings_file=settings)):
                    run_gui()
                if failures:raise failures[0]
