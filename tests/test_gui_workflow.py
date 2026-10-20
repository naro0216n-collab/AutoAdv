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
                for file in ('capture.dll','asr.onnx','tokens.txt'):
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
                        # The standard HOOK mode must save without any visual calibration.
                        window.save_monitor_profile()
                        saved=json.loads(settings.read_text(encoding='utf-8'))
                        self.assertEqual(saved['schema_version'],2)
                        self.assertNotIn('live_profiles',saved)
                        raw=window.service.monitor_document()
                        self.assertNotIn('pid',raw)
                        self.assertNotIn('hwnd',raw)
                        self.assertTrue(window.pick_ocr.isEnabled())
                        self.assertFalse(window.service.autoplay_allowed)
                        self.assertFalse(window.allow.isEnabled())
                        self.assertFalse(window.monitor_dirty)
                    except BaseException as exc:
                        failures.append(exc)
                    finally:
                        if window is not None:window.close()
                QTimer.singleShot(0,inspect)
                def timeout():
                    if any(x.isVisible() for x in app.topLevelWidgets() if isinstance(x,QMainWindow)):
                        failures.append(TimeoutError('GUI shutdown did not complete'))
                    app.quit()
                QTimer.singleShot(15000,timeout)
                with patch('autoadv.gui.AutoAdvService',lambda:AutoAdvService(settings_file=settings)):
                    run_gui()
                if failures:raise failures[0]
