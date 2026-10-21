"""A single GUI integrating game profiles, LunaHook, Irodori, and advancement."""
from __future__ import annotations
import ctypes
import os
import sys
import subprocess
import threading
import copy
from collections import deque
from pathlib import Path

from .conversation import Controller, Target, Token
from .hook_host import HookHost
from .luna_runtime import runtime_dir, adopted_runtime, RELEASE
from .hook_protocol import HookEvent, ThreadSource
from .irodori_server import IrodoriServer
from .model import AdvanceMode, HookSelection, Profile, StructureRule, ReplacementRule, COMMON_FIELDS
from .storage import Catalog, load, save
from .voice import VoiceJobs
from .native_audio import NativeAudio
from .manual_input import WindowsManualMonitor
from .ocr import OCRWorker, TesseractOCR
from .screen import WindowsClientCapture
from .import_luna import inspect_luna, add_luna_games
from .import_legacy import inspect_legacy, add_legacy_games
from .game_windows import find_game_windows, find_game_processes
from .windows_input import WindowsAdvancementInput
from .validation_trace import ValidationTrace, text_digest


def run_gui(session_driver=None) -> int:
    from PySide6.QtCore import QObject, Signal, QTimer
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                                   QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
                                   QLabel, QPushButton, QComboBox, QFileDialog,
                                   QSpinBox, QGroupBox, QMessageBox, QCheckBox, QScrollArea, QInputDialog,
                                   QTableWidget, QTableWidgetItem, QAbstractItemView)

    class SignalBridge(QObject):
        hook = Signal(object)
        closed = Signal(str)
        voice = Signal(object, bool, str)
        server = Signal(object)
        ocr = Signal(int, int, int, str)
        ocr_error = Signal(str)
        native = Signal(object, str, str)
        native_ready = Signal(int, object, str)
        native_pcm = Signal(object, int)
        preview = Signal(bool, str)

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('AutoAdv — HOOK / Irodori / Advance')
            self.resize(1150, 950)
            self.catalog = load()
            self.bridge = SignalBridge()
            self.bridge.hook.connect(self.on_hook)
            self.bridge.closed.connect(self.on_closed)
            self.bridge.voice.connect(self.on_voice)
            self.bridge.server.connect(self.on_server)
            self.bridge.ocr.connect(self.on_ocr)
            self.bridge.ocr_error.connect(self.on_ocr_error)
            self.bridge.native.connect(self.on_native)
            self.bridge.native_ready.connect(self.on_native_ready)
            self.bridge.native_pcm.connect(self.on_native_pcm)
            self.bridge.preview.connect(self.on_preview)
            self.jobs = VoiceJobs(
                lambda token, done, reason: self.bridge.voice.emit(token, done, reason),
                on_timing=(lambda token, stage, duration_ms, audio_ms:
                    self.validation_trace.emit('tts_timing', session=token.session,
                        line=token.line, stage=stage, duration_ms=duration_ms, audio_ms=audio_ms))
                if os.environ.get('AUTOADV_VALIDATION_TRACE') else None)
            self.preview_jobs = VoiceJobs(lambda token, done, reason: self.bridge.preview.emit(done, reason))
            self.controller = Controller(self.send_input, self.preflight)
            self.validation_trace = ValidationTrace()
            self.hook_host: HookHost | None = None
            self.host_generation = ''
            self.connection_generation = 0
            self.sources: dict[str, ThreadSource] = {}
            self.source_samples: dict[str, deque[str]] = {}
            self.ambiguous_sources: set[str] = set()
            self.source_rows: dict[tuple[str, int], int] = {}
            self.game_id = ''
            self.profile_id = ''
            self.server: IrodoriServer | None = None
            self.ocr_worker: OCRWorker | None = None
            self.native_audio: NativeAudio | None = None
            self.manual_monitor: WindowsManualMonitor | None = None
            self.native_token: Token | None = None
            self.native_started = False
            self._known_game_pid = None
            self._synced_sources: dict[tuple[int, int, int, int], ThreadSource] = {}
            self._output_group: list[HookEvent] = []
            self._output_timer = QTimer(self)
            self._output_timer.setSingleShot(True)
            self._output_timer.timeout.connect(self.flush_selected_outputs)
            scroller = QScrollArea()
            scroller.setWidgetResizable(True)
            self.setCentralWidget(scroller)
            panel = QWidget()
            scroller.setWidget(panel)
            body = QVBoxLayout(panel)

            def group(title):
                widget = QGroupBox(title)
                form = QFormLayout(widget)
                body.addWidget(widget)
                return form
            environment = group('Common environment')
            self.irodori_path = QLineEdit(self.catalog.irodori_root)
            self.luna_runtime_status = QLabel(f'{RELEASE}: {runtime_dir()}')
            self.irodori_url = QLineEdit(self.catalog.irodori_url)
            environment.addRow('Installed Irodori folder', self.irodori_path)
            environment.addRow('Irodori URL', self.irodori_url)
            environment.addRow('Adopted Luna runtime (fixed, not downloaded on launch)', self.luna_runtime_status)
            self.btn_server = QPushButton('Connect / start Irodori and read Voices')
            environment.addRow(self.btn_server)
            self.btn_server.clicked.connect(self.start_server)
            self.common_controls = {}
            common_titles = {
                'advance_mode': '自動進行方式', 'uncertain_text_fallback': '音声判定が不確かなとき文字数時間で進む',
                'default_tts': '未知話者にもIrodori TTSを使用', 'default_voice': 'Default Voice ID',
                'reading_ms': '最低表示時間 (ms)', 'per_character_wait_ms': '1文字あたり待機時間 (ms)',
                'native_helper': 'Process Loopback helper',
                'asr_model': 'Japanese ASR model', 'asr_tokens': 'ASR tokens', 'input_key': 'Advance key code',
                'next_line_timeout_ms': 'Next line wait (ms)'}
            for key in COMMON_FIELDS:
                value = getattr(self.catalog.common, key)
                if key == 'advance_mode':
                    control = QComboBox()
                    control.addItem('文字のみ判断', AdvanceMode.TEXT.value)
                    control.addItem('音声判断', AdvanceMode.AUDIO.value)
                    control.setCurrentIndex(control.findData(value.value))
                elif isinstance(value, bool):
                    control = QCheckBox('有効')
                    control.setChecked(value)
                elif isinstance(value, int):
                    control = QSpinBox()
                    if key in ('input_key',):
                        control.setRange(1, 254)
                    elif key == 'next_line_timeout_ms':
                        control.setRange(1000, 120000)
                    else:
                        control.setRange(1, 600000)
                    control.setValue(value)
                else:
                    control = QLineEdit(value)
                self.common_controls[key] = control
                environment.addRow('Common: ' + common_titles[key], control)
            self.btn_save_common = QPushButton('Save common audio and advance defaults')
            self.btn_save_common.clicked.connect(self.save_common)
            environment.addRow(self.btn_save_common)
            self.server_status = QLabel('Irodori: not connected')
            environment.addRow(self.server_status)
            self.btn_host = QPushButton('Start LunaHook Host')
            environment.addRow(self.btn_host)
            self.btn_host.clicked.connect(self.start_host)
            self.host_status = QLabel('LunaHook: disconnected')
            environment.addRow(self.host_status)

            games = group('Game and profiles')
            self.game_choice = QComboBox()
            self.game_choice.currentIndexChanged.connect(self.choose_game)
            games.addRow('Game', self.game_choice)
            self.new_game = QLineEdit()
            self.new_game.setPlaceholderText('Game display name')
            self.btn_game = QPushButton('Register game')
            row = QHBoxLayout()
            row.addWidget(self.new_game)
            row.addWidget(self.btn_game)
            games.addRow(row)
            self.btn_game.clicked.connect(self.add_game)
            self.game_exe = QLineEdit()
            self.game_exe.setPlaceholderText('Optional game executable path for future sessions')
            games.addRow('Game executable', self.game_exe)
            self.audio_exe = QLineEdit()
            self.audio_exe.setPlaceholderText('Optional: separate audio process executable')
            games.addRow('Audio process executable', self.audio_exe)
            self.input_exe = QLineEdit()
            self.input_exe.setPlaceholderText('Optional: separate input window executable')
            games.addRow('Input process executable', self.input_exe)
            self.btn_launch = QPushButton('Launch registered game')
            self.btn_launch.clicked.connect(self.launch_game)
            games.addRow(self.btn_launch)
            self.btn_import = QPushButton('Import existing Luna game settings (preview and confirm)')
            games.addRow(self.btn_import)
            self.btn_import.clicked.connect(self.import_luna)
            self.btn_import_legacy = QPushButton('Import previous AutoAdv v2 settings (preview and confirm)')
            games.addRow(self.btn_import_legacy)
            self.btn_import_legacy.clicked.connect(self.import_legacy)
            self.profile_choice = QComboBox()
            self.profile_choice.currentIndexChanged.connect(self.choose_profile)
            games.addRow('Profile', self.profile_choice)
            self.new_profile = QLineEdit()
            self.new_profile.setPlaceholderText('New profile name')
            self.btn_profile = QPushButton('Add profile')
            row = QHBoxLayout()
            row.addWidget(self.new_profile)
            row.addWidget(self.btn_profile)
            games.addRow(row)
            self.btn_profile.clicked.connect(self.add_profile)
            self.btn_dup = QPushButton('Duplicate profile')
            self.btn_rename = QPushButton('Rename profile')
            self.btn_delete = QPushButton('Delete profile')
            self.btn_default = QPushButton('Use as default profile')
            for button, handler in ((self.btn_dup, self.duplicate_profile),
                                    (self.btn_rename, self.rename_profile),
                                    (self.btn_delete, self.delete_profile),
                                    (self.btn_default, self.set_default_profile)):
                games.addRow(button)
                button.clicked.connect(handler)

            capture = group('Live game and HOOK selection')
            self.pid = QSpinBox()
            self.pid.setRange(0, 2147483647)
            self.hwnd = QLineEdit()
            self.hwnd.setPlaceholderText('HWND e.g. 0x12345')
            capture.addRow('HOOK process PID (current run)', self.pid)
            self.audio_pid = QSpinBox()
            self.audio_pid.setRange(0, 2147483647)
            self.audio_pid.setSpecialValueText('Use HOOK PID')
            self.input_pid = QSpinBox()
            self.input_pid.setRange(0, 2147483647)
            self.input_pid.setSpecialValueText('Use HOOK PID')
            capture.addRow('Audio process PID (0 = HOOK)', self.audio_pid)
            capture.addRow('Input window PID (0 = HOOK)', self.input_pid)
            capture.addRow('Game HWND (current run)', self.hwnd)
            self.attach = QPushButton('Attach to game PID')
            self.attach.clicked.connect(self.connect_game)
            capture.addRow(self.attach)
            self.btn_discover = QPushButton('Find windows matching registered game executable')
            self.btn_discover.clicked.connect(self.detect_game)
            capture.addRow(self.btn_discover)
            self.btn_discover_all = QPushButton('Identify a running game from all registered executables')
            self.btn_discover_all.clicked.connect(self.detect_registered_game)
            capture.addRow(self.btn_discover_all)
            self.btn_audio_target = QPushButton('Find audio process by saved executable')
            self.btn_input_target = QPushButton('Find input window by saved executable')
            self.btn_audio_target.clicked.connect(lambda: self.detect_associated_target('audio'))
            self.btn_input_target.clicked.connect(lambda: self.detect_associated_target('input'))
            capture.addRow(self.btn_audio_target)
            capture.addRow(self.btn_input_target)
            self.btn_find = QPushButton('Find HOOK candidates')
            self.btn_find.clicked.connect(self.find_hooks)
            capture.addRow(self.btn_find)
            self.manual_hook = QLineEdit()
            self.manual_hook.setPlaceholderText('Manual Luna HOOK code')
            capture.addRow('Manual HOOK code', self.manual_hook)
            self.btn_insert = QPushButton('Insert and save manual HOOK')
            self.btn_insert.clicked.connect(self.insert_hook)
            capture.addRow(self.btn_insert)
            self.found_hooks = QTableWidget(0, 2)
            self.found_hooks.setHorizontalHeaderLabels(['HOOK code', 'Search sample'])
            self.found_hooks.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.found_hooks.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.found_hooks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.found_hooks.setMinimumHeight(110)
            self.found_hooks.horizontalHeader().setStretchLastSection(True)
            capture.addRow('Search results', self.found_hooks)
            self.btn_found = QPushButton('Insert selected search result')
            self.btn_found.clicked.connect(self.insert_found)
            capture.addRow(self.btn_found)
            self.source_mode = QComboBox()
            self.source_mode.addItems(['hook', 'ocr'])
            capture.addRow('Text source', self.source_mode)
            self.ocr_exe = QLineEdit('tesseract')
            self.ocr_lang = QLineEdit('jpn')
            self.ocr_region = QLineEdit('0,0,640,180')
            capture.addRow('Installed Tesseract executable', self.ocr_exe)
            capture.addRow('OCR language', self.ocr_lang)
            capture.addRow('OCR region x,y,width,height', self.ocr_region)
            self.ocr_stability = QSpinBox()
            self.ocr_stability.setRange(1, 10)
            capture.addRow('OCR repeated stable captures', self.ocr_stability)
            self.source_table = QTableWidget(0, 6)
            self.source_table.setHorizontalHeaderLabels(['Role / priority', 'HOOK code / name', 'PID', 'Context', 'Thread address', 'Latest raw text'])
            self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.source_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_table.setMinimumHeight(205)
            self.source_table.horizontalHeader().setStretchLastSection(True)
            self.source_table.itemSelectionChanged.connect(self.show_hook_preview)
            capture.addRow('Live HOOK candidates (select a row)', self.source_table)
            self.hook_preview = QPlainTextEdit()
            self.hook_preview.setReadOnly(True)
            self.hook_preview.setMaximumHeight(130)
            capture.addRow('Raw text from selected HOOK (before choosing)', self.hook_preview)
            row = QHBoxLayout()
            self.text_hook = QPushButton('Use as dialogue HOOK')
            self.speaker_hook = QPushButton('Use as speaker HOOK')
            self.text_hook.clicked.connect(lambda: self.select_hook('text'))
            self.speaker_hook.clicked.connect(lambda: self.select_hook('speaker'))
            row.addWidget(self.text_hook)
            row.addWidget(self.speaker_hook)
            capture.addRow(row)
            self.btn_unselect_hook = QPushButton('Remove saved HOOK role')
            self.btn_unselect_hook.clicked.connect(self.unselect_hook)
            capture.addRow(self.btn_unselect_hook)
            self.selected_hooks = QLabel('Selected HOOKs: none')
            capture.addRow(self.selected_hooks)
            self.saved_hook_choice = QComboBox()
            capture.addRow('Saved HOOK priority (first dialogue HOOK is preferred)', self.saved_hook_choice)
            row = QHBoxLayout()
            self.btn_hook_up = QPushButton('Move saved HOOK up')
            self.btn_hook_down = QPushButton('Move saved HOOK down')
            self.btn_hook_up.clicked.connect(lambda: self.move_saved_hook(-1))
            self.btn_hook_down.clicked.connect(lambda: self.move_saved_hook(1))
            row.addWidget(self.btn_hook_up)
            row.addWidget(self.btn_hook_down)
            capture.addRow(row)
            self.hook_reconnect = QLabel('Saved HOOKs: not connected')
            capture.addRow(self.hook_reconnect)
            self.active_hook_status = QLabel('Active dialogue HOOK: not monitoring')
            capture.addRow(self.active_hook_status)
            self.paired_text_hook = QComboBox()
            self.paired_speaker_hook = QComboBox()
            capture.addRow('Dialogue HOOK to associate', self.paired_text_hook)
            capture.addRow('Speaker HOOK to associate', self.paired_speaker_hook)
            self.btn_pair = QPushButton('Assign speaker HOOK to dialogue HOOK')
            self.btn_pair.clicked.connect(self.pair_hooks)
            capture.addRow(self.btn_pair)
            self.btn_monitor = QPushButton('Start monitoring selected HOOK')
            self.btn_monitor.clicked.connect(self.begin_monitor)
            capture.addRow(self.btn_monitor)

            speech = group('Dialogue and voice routing')
            self.default_tts = QCheckBox('未知話者にTTSを使用（進行モードと独立）')
            speech.addRow('Unknown speaker TTS', self.default_tts)
            self.default_voice = QComboBox()
            self.default_voice.setEditable(True)
            speech.addRow('Default Irodori Voice ID', self.default_voice)
            self.speaker_name = QLineEdit()
            self.speaker_name.setPlaceholderText('Character name, empty = default')
            speech.addRow('Speaker override', self.speaker_name)
            self.speaker_tts = QCheckBox('この話者にIrodori TTSを使用')
            speech.addRow('Speaker TTS', self.speaker_tts)
            self.speaker_voice = QComboBox()
            self.speaker_voice.setEditable(True)
            speech.addRow('Speaker Voice', self.speaker_voice)
            self.speaker_overrides = QComboBox()
            self.speaker_overrides.currentIndexChanged.connect(self.show_speaker_override)
            speech.addRow('Saved speaker overrides', self.speaker_overrides)
            self.btn_unassign = QPushButton('Remove selected speaker override')
            self.btn_unassign.clicked.connect(self.remove_speaker)
            speech.addRow(self.btn_unassign)
            self.structure_pattern = QLineEdit()
            self.structure_pattern.setPlaceholderText('regex with named body and optional speaker')
            speech.addRow('Add dialogue extraction regex', self.structure_pattern)
            self.btn_structure = QPushButton('Add prioritized extraction rule')
            self.btn_structure.clicked.connect(self.add_structure_rule)
            speech.addRow(self.btn_structure)
            self.rules_display = QPlainTextEdit()
            self.rules_display.setReadOnly(True)
            self.rules_display.setMaximumHeight(75)
            speech.addRow('Extraction rules in priority order', self.rules_display)
            self.structure_choice = QComboBox()
            speech.addRow('Selected structure rule', self.structure_choice)
            self.btn_structure_up = QPushButton('Increase rule priority')
            self.btn_structure_up.clicked.connect(self.raise_structure_rule)
            self.btn_structure_down = QPushButton('Decrease rule priority')
            self.btn_structure_down.clicked.connect(self.lower_structure_rule)
            self.btn_structure_remove = QPushButton('Remove extraction rule')
            self.btn_structure_remove.clicked.connect(self.remove_structure_rule)
            for btn in (self.btn_structure_up, self.btn_structure_down, self.btn_structure_remove):
                speech.addRow(btn)
            self.replace_pattern = QLineEdit()
            self.replace_value = QLineEdit()
            self.replace_target = QComboBox()
            self.replace_target.addItems(['tts', 'asr', 'both'])
            self.replace_regex = QCheckBox('Regular expression')
            speech.addRow('Text replacement match', self.replace_pattern)
            speech.addRow('Replacement text', self.replace_value)
            speech.addRow('Apply to', self.replace_target)
            speech.addRow(self.replace_regex)
            self.btn_replace = QPushButton('Add text replacement')
            self.btn_replace.clicked.connect(self.add_replacement)
            speech.addRow(self.btn_replace)
            self.replacement_choice = QComboBox()
            speech.addRow('Saved replacements', self.replacement_choice)
            self.btn_replacement_remove = QPushButton('Remove selected replacement')
            self.btn_replacement_remove.clicked.connect(self.remove_replacement)
            speech.addRow(self.btn_replacement_remove)
            self.reading_word = QLineEdit()
            self.reading_value = QLineEdit()
            speech.addRow('Name reading: written form', self.reading_word)
            speech.addRow('Name reading: pronunciation', self.reading_value)
            self.btn_reading = QPushButton('Save name reading')
            self.btn_reading.clicked.connect(self.add_reading)
            speech.addRow(self.btn_reading)
            self.reading_choice = QComboBox()
            speech.addRow('Saved name readings', self.reading_choice)
            self.btn_reading_remove = QPushButton('Remove selected name reading')
            self.btn_reading_remove.clicked.connect(self.remove_reading)
            speech.addRow(self.btn_reading_remove)
            self.audition_text = QLineEdit('音声の試聴です。')
            speech.addRow('Irodori sample text', self.audition_text)
            self.btn_audition = QPushButton('Play selected Voice sample')
            self.btn_audition.clicked.connect(self.audition_voice)
            speech.addRow(self.btn_audition)
            self.btn_stop_audition = QPushButton('Stop Voice sample')
            self.btn_stop_audition.clicked.connect(self.preview_jobs.cancel)
            speech.addRow(self.btn_stop_audition)
            self.btn_assign = QPushButton('Save speaker routing')
            self.btn_assign.clicked.connect(self.assign_speaker)
            speech.addRow(self.btn_assign)
            self.reading_ms = QSpinBox()
            self.reading_ms.setRange(1, 600000)
            self.reading_ms.setValue(700)
            # Timing controls are in the separate advance settings section below.
            self.hook_flush = QSpinBox()
            self.hook_flush.setRange(0, 10000)
            speech.addRow('Luna HOOK flush delay (ms)', self.hook_flush)
            self.hook_codepage = QSpinBox()
            self.hook_codepage.setRange(1, 65535)
            speech.addRow('Luna HOOK codepage', self.hook_codepage)
            self.hook_buffer = QSpinBox()
            self.hook_buffer.setRange(1, 1000000)
            speech.addRow('Luna HOOK max buffer (characters)', self.hook_buffer)
            self.input_key = QSpinBox()
            self.input_key.setRange(1, 254)
            speech.addRow('Advance virtual key code', self.input_key)
            self.native_helper = QLineEdit()
            self.asr_model = QLineEdit()
            self.asr_tokens = QLineEdit()
            speech.addRow('Process audio helper DLL (音声判断用)', self.native_helper)
            speech.addRow('Japanese ASR model (音声判断用)', self.asr_model)
            speech.addRow('ASR tokens', self.asr_tokens)
            self.next_line_timeout = QSpinBox()
            self.next_line_timeout.setRange(1000, 120000)
            speech.addRow('Next line wait limit (ms)', self.next_line_timeout)
            self.repeat = QComboBox()
            self.repeat.addItems(['distinct_only', 'each_after_input'])
            speech.addRow('HOOK repeated-line behavior', self.repeat)
            self.inherit_controls = {}
            for key in COMMON_FIELDS:
                control = QCheckBox('Inherit common setting: ' + key.replace('_', ' '))
                self.inherit_controls[key] = control
                speech.addRow(control)
            self.btn_save = QPushButton('Apply and save profile')
            self.btn_save.clicked.connect(self.save_profile)
            speech.addRow(self.btn_save)
            self.preview = QPlainTextEdit()
            self.preview.setReadOnly(True)
            self.preview.setMaximumHeight(125)
            speech.addRow('Raw / speaker / reading / voice', self.preview)

            advance = group('自動進行（TTS使用設定とは独立）')
            self.advance_mode = QComboBox()
            self.advance_mode.addItem('文字のみ判断', AdvanceMode.TEXT.value)
            self.advance_mode.addItem('音声判断', AdvanceMode.AUDIO.value)
            advance.addRow('進行方式', self.advance_mode)
            self.uncertain_text_fallback = QCheckBox('音声判定が不確かなとき、文字数待機時間で進む（早送りの可能性あり）')
            advance.addRow(self.uncertain_text_fallback)
            self.per_character_wait_ms = QSpinBox()
            self.per_character_wait_ms.setRange(1, 600000)
            advance.addRow('記号除外の1文字あたり待機時間（ms）', self.per_character_wait_ms)
            advance.addRow('最低表示時間（ms）', self.reading_ms)
            self.wait_preview = QLabel('計数: - / 待機時間: -')
            advance.addRow('現在台詞の計数と確定時間', self.wait_preview)
            advance.addRow(QLabel('文字のみ判断は原音を途中で送る場合があります。音声判断で確証がなければ停止します。'))
            row = QHBoxLayout()
            self.btn_permit = QPushButton('Authorize automatic advance')
            self.btn_pause = QPushButton('Pause automatic advance')
            row.addWidget(self.btn_permit)
            row.addWidget(self.btn_pause)
            self.btn_permit.clicked.connect(self.permit)
            self.btn_pause.clicked.connect(self.pause)
            advance.addRow(row)
            self.status = QLabel('Status: not monitoring')
            advance.addRow(self.status)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumHeight(120)
            body.addWidget(self.log)
            self.intervention_timer = QTimer(self)
            self.intervention_timer.setInterval(150)
            self.intervention_timer.timeout.connect(self.check_intervention)
            self.intervention_timer.start()
            self.confirm_migration_if_needed()
            self.fill_games()
            if self.irodori_path.text().strip():
                QTimer.singleShot(0, self.start_server)

        def note(self, message):
            self.log.appendPlainText(message)

        def confirm_migration_if_needed(self):
            review = getattr(self.catalog, 'migration_review', ())
            if not review:
                return
            from .storage import user_settings_path
            message = ('保存形式v3には旧4区分の音声方針があります。新2モードは自動推定しません。\n'
                       + '\n'.join(review) + '\n旧設定は別ファイルへ保全します。\n'
                       'この内容を確認して新設定へ移行しますか？')
            decision = QMessageBox.question(self, '設定移行の確認', message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if decision != QMessageBox.StandardButton.Yes:
                self.note('設定移行を取り消しました。原本を保持してGUIを終了します。')
                QTimer.singleShot(0, self.close)
                return
            source = Path(self.catalog.migration_source)
            destination = user_settings_path()
            if destination.exists():
                raise RuntimeError('移行先の設定ファイルが既に存在します')
            save(self.catalog, destination)
            self.catalog.migration_review = []
            self.note('旧設定原本を変更せず新設定へ移行しました: ' + str(source))

        def save_common(self):
            from .storage import CommonSettings
            values = {}
            for key, widget in self.common_controls.items():
                if isinstance(widget, QComboBox):
                    values[key] = AdvanceMode(widget.currentData())
                elif isinstance(widget, QCheckBox):
                    values[key] = widget.isChecked()
                elif isinstance(widget, QSpinBox):
                    values[key] = widget.value()
                else:
                    values[key] = widget.text().strip()
            try:
                common = CommonSettings(**values)
                common.validate()
                self.catalog.common = common
                self.persist()
                if self.current_profile() and not self.controller.profile:
                    self.choose_profile()
                self.note('Common defaults saved; any active session keeps its previous effective settings')
            except Exception as exc:
                self.note('Common settings not saved: ' + str(exc))

        def persist(self):
            self.catalog.irodori_root = self.irodori_path.text().strip()
            self.catalog.irodori_url = self.irodori_url.text().strip()
            save(self.catalog)

        def fill_games(self):
            self.game_choice.blockSignals(True)
            self.game_choice.clear()
            for gid, game in self.catalog.games.items():
                self.game_choice.addItem(game.name, gid)
            self.game_choice.blockSignals(False)
            self.choose_game()

        def current_profile(self) -> Profile | None:
            game = self.catalog.games.get(self.game_id)
            return game.profiles.get(self.profile_id) if game else None

        def choose_game(self, *_):
            self.stop_session()
            self.clear_hook_observations()
            self.pid.setValue(0)
            self.audio_pid.setValue(0)
            self.input_pid.setValue(0)
            self.hwnd.clear()
            self.game_id = self.game_choice.currentData() or ''
            self.profile_choice.blockSignals(True)
            self.profile_choice.clear()
            game = self.catalog.games.get(self.game_id)
            if game:
                for profile_id, profile in game.profiles.items():
                    self.profile_choice.addItem(profile.name, profile_id)
                position = self.profile_choice.findData(game.default_profile)
                self.profile_choice.setCurrentIndex(max(0, position))
                self.game_exe.setText(game.executable)
                self.audio_exe.setText(game.audio_executable)
                self.input_exe.setText(game.input_executable)
            else:
                self.game_exe.clear()
                self.audio_exe.clear()
                self.input_exe.clear()
            self.profile_choice.blockSignals(False)
            self.choose_profile()

        def choose_profile(self, *_):
            self.stop_session()
            self.profile_id = self.profile_choice.currentData() or ''
            profile = self.current_profile()
            if not profile:
                return
            effective = self.catalog.effective_profile(profile)
            for key, widget in self.inherit_controls.items():
                widget.setChecked(key in profile.inherit_common)
            self.default_tts.setChecked(effective.default_tts)
            self.advance_mode.setCurrentIndex(self.advance_mode.findData(effective.advance_mode.value))
            self.uncertain_text_fallback.setChecked(effective.uncertain_text_fallback)
            self.per_character_wait_ms.setValue(effective.per_character_wait_ms)
            self.default_voice.setEditText(effective.default_voice)
            self.reading_ms.setValue(effective.reading_ms)
            self.hook_flush.setValue(profile.hook_flush_ms)
            self.hook_codepage.setValue(profile.hook_codepage)
            self.hook_buffer.setValue(profile.hook_max_buffer)
            self.input_key.setValue(effective.input_key)
            self.repeat.setCurrentText(profile.repeat_rule)
            self.source_mode.setCurrentText(profile.source)
            self.ocr_exe.setText(profile.ocr.executable)
            self.ocr_lang.setText(profile.ocr.language)
            self.ocr_region.setText(','.join(map(str, profile.ocr.region)))
            self.ocr_stability.setValue(profile.ocr.stable_samples)
            self.native_helper.setText(effective.native_helper)
            self.asr_model.setText(effective.asr_model)
            self.asr_tokens.setText(effective.asr_tokens)
            self.next_line_timeout.setValue(effective.next_line_timeout_ms)
            self.refresh_rules()
            self.refresh_selected_hooks()
            self.refresh_speaker_choices()
            self.refresh_hook_pairs()

        def add_game(self):
            try:
                game = self.catalog.create_game(self.new_game.text(), self.game_exe.text().strip())
                self.persist()
                self.fill_games()
                self.game_choice.setCurrentIndex(self.game_choice.findData(game.id))
            except Exception as exc:
                self.note(str(exc))

        def add_profile(self):
            if not self.game_id:
                return
            try:
                p = self.catalog.add_profile(self.game_id, self.new_profile.text())
                self.persist()
                self.choose_game()
                self.profile_choice.setCurrentIndex(self.profile_choice.findData(p.id))
            except Exception as exc:
                self.note(str(exc))

        def save_profile(self):
            p = self.current_profile()
            if not p:
                self.note('Register a game first')
                return False
            try:
                updated = copy.deepcopy(p)
                from .model import OCRSettings
                updated.source = self.source_mode.currentText()
                updated.ocr = OCRSettings(self.ocr_exe.text().strip(), self.ocr_lang.text().strip(),
                    tuple(int(x.strip()) for x in self.ocr_region.text().split(',')),
                    stable_samples=self.ocr_stability.value())
                updated.native_helper = self.native_helper.text().strip()
                updated.asr_model = self.asr_model.text().strip()
                updated.asr_tokens = self.asr_tokens.text().strip()
                updated.next_line_timeout_ms = self.next_line_timeout.value()
                updated.default_tts = self.default_tts.isChecked()
                updated.advance_mode = AdvanceMode(self.advance_mode.currentData())
                updated.uncertain_text_fallback = self.uncertain_text_fallback.isChecked()
                updated.per_character_wait_ms = self.per_character_wait_ms.value()
                updated.default_voice = self.default_voice.currentText().strip()
                updated.reading_ms = self.reading_ms.value()
                updated.hook_flush_ms = self.hook_flush.value()
                updated.hook_codepage = self.hook_codepage.value()
                updated.hook_max_buffer = self.hook_buffer.value()
                updated.input_key = self.input_key.value()
                updated.repeat_rule = self.repeat.currentText()
                updated.inherit_common = [key for key, widget in self.inherit_controls.items() if widget.isChecked()]
                updated.validate()
                game = self.catalog.games[self.game_id]
                game.profiles[updated.id] = updated
                game.executable = self.game_exe.text().strip()
                game.audio_executable = self.audio_exe.text().strip()
                game.input_executable = self.input_exe.text().strip()
                self.persist()
                self.note('Profile saved; restart monitoring to apply new rules')
                return True
            except Exception as exc:
                self.note('Profile not saved: ' + str(exc))
                return False

        def assign_speaker(self):
            p = self.current_profile()
            if not p:
                return
            name = self.speaker_name.text().strip()
            if not name:
                self.note('A speaker name is required for an override')
                return
            p.speaker_tts[name] = self.speaker_tts.isChecked()
            p.speaker_voices[name] = self.speaker_voice.currentText().strip()
            self.persist()
            self.refresh_speaker_choices()
            self.note('Saved speaker: ' + name)

        def refresh_rules(self):
            p = self.current_profile()
            if not p:
                return
            self.rules_display.setPlainText('\n'.join(r.pattern for r in p.structure_rules))
            self.structure_choice.clear()
            for i, rule in enumerate(p.structure_rules):
                self.structure_choice.addItem(rule.pattern, i)
            self.replacement_choice.clear()
            for i, rule in enumerate(p.replacements):
                self.replacement_choice.addItem(f'{rule.target}: {rule.pattern} → {rule.replacement}', i)
            self.reading_choice.clear()
            for word, reading in p.name_readings.items():
                self.reading_choice.addItem(f'{word} → {reading}', word)

        def move_structure_rule(self, movement):
            p = self.current_profile()
            if p is None:
                return
            i = self.structure_choice.currentData()
            if i is None or not 0 <= i + movement < len(p.structure_rules):
                return
            p.structure_rules[i], p.structure_rules[i + movement] = p.structure_rules[i + movement], p.structure_rules[i]
            self.persist()
            self.refresh_rules()
            self.structure_choice.setCurrentIndex(i + movement)

        def raise_structure_rule(self):
            self.move_structure_rule(-1)

        def lower_structure_rule(self):
            self.move_structure_rule(1)

        def remove_structure_rule(self):
            p = self.current_profile()
            i = self.structure_choice.currentData()
            if p is not None and i is not None:
                del p.structure_rules[i]
                self.persist()
                self.refresh_rules()

        def remove_replacement(self):
            p = self.current_profile()
            i = self.replacement_choice.currentData()
            if p is not None and i is not None:
                del p.replacements[i]
                self.persist()
                self.refresh_rules()

        def remove_reading(self):
            p = self.current_profile()
            word = self.reading_choice.currentData()
            if p is not None and word:
                p.name_readings.pop(word, None)
                self.persist()
                self.refresh_rules()

        def refresh_speaker_choices(self):
            p = self.current_profile()
            self.speaker_overrides.clear()
            if p:
                for name in sorted(set(p.speaker_tts) | set(p.speaker_voices)):
                    self.speaker_overrides.addItem(name, name)
            self.refresh_rules()

        def show_speaker_override(self, *_):
            p = self.current_profile()
            name = self.speaker_overrides.currentData()
            if p and name:
                self.speaker_name.setText(name)
                self.speaker_tts.setChecked(p.speaker_tts.get(name, p.default_tts))
                self.speaker_voice.setEditText(p.speaker_voices.get(name, ''))

        def remove_speaker(self):
            p = self.current_profile()
            name = self.speaker_overrides.currentData()
            if p and name:
                p.speaker_tts.pop(name, None)
                p.speaker_voices.pop(name, None)
                self.persist()
                self.refresh_speaker_choices()

        def audition_voice(self):
            voice = self.speaker_voice.currentText().strip() or self.default_voice.currentText().strip()
            try:
                if not voice:
                    raise ValueError('choose a Voice ID first')
                self.preview_jobs.start(Token(-1, -1), self.catalog.irodori_url,
                                        self.audition_text.text(), voice)
            except Exception as exc:
                self.note('Voice sample unavailable: ' + str(exc))

        def on_preview(self, success, reason):
            self.note('Voice sample finished' if success else 'Voice sample: ' + reason)

        def add_structure_rule(self):
            p = self.current_profile()
            if p:
                try:
                    rule = StructureRule(self.structure_pattern.text().strip())
                    p.structure_rules.insert(0, rule)
                    self.persist()
                    self.refresh_rules()
                except Exception as exc:
                    self.note(str(exc))

        def add_replacement(self):
            p = self.current_profile()
            if p:
                try:
                    p.replacements.append(ReplacementRule(self.replace_pattern.text(),
                        self.replace_value.text(), self.replace_regex.isChecked(),
                        self.replace_target.currentText()))
                    self.persist()
                    self.refresh_rules()
                    self.note('Text replacement saved')
                except Exception as exc:
                    self.note(str(exc))

        def add_reading(self):
            p = self.current_profile()
            if p and self.reading_word.text().strip() and self.reading_value.text().strip():
                p.name_readings[self.reading_word.text().strip()] = self.reading_value.text().strip()
                self.persist()
                self.refresh_rules()
                self.note('Name reading saved')

        def duplicate_profile(self):
            if self.current_profile():
                p = self.catalog.duplicate_profile(self.game_id, self.profile_id,
                        self.new_profile.text().strip() or self.current_profile().name + ' copy')
                self.persist()
                self.choose_game()
                self.profile_choice.setCurrentIndex(self.profile_choice.findData(p.id))

        def rename_profile(self):
            if self.current_profile():
                try:
                    self.catalog.rename_profile(self.game_id, self.profile_id, self.new_profile.text())
                    self.persist()
                    self.choose_game()
                except ValueError as exc:
                    self.note(str(exc))

        def delete_profile(self):
            if self.current_profile() and QMessageBox.question(self, 'Delete profile',
                    'Delete this profile and its settings?') == QMessageBox.StandardButton.Yes:
                try:
                    self.catalog.delete_profile(self.game_id, self.profile_id)
                    self.persist()
                    self.choose_game()
                except ValueError as exc:
                    self.note(str(exc))

        def set_default_profile(self):
            if self.current_profile():
                self.catalog.games[self.game_id].default_profile = self.profile_id
                self.persist()
                self.note('Default profile saved')

        def import_luna(self):
            path, _ = QFileDialog.getOpenFileName(self, 'Select Luna savegamedata JSON', '', 'JSON (*.json)')
            if not path:
                return
            try:
                entries = inspect_luna(Path(path))
                proposed = add_luna_games(self.catalog, entries)
                summary = '\n'.join(f'{g.title or Path(g.path).name}: {len(g.hooks)} HOOKs; '
                        f'{len(g.inserted)} manual; {len(g.repairs)} TTS replacements; '
                        f'{len(g.hook_options)} native settings' +
                        (f'\n  Review in Luna: {", ".join(g.unmapped)}' if g.unmapped else '')
                        for g in entries)
                if QMessageBox.question(self, 'Confirm Luna import',
                        'Recognized settings only; the original file is unchanged.\n' + summary) != QMessageBox.StandardButton.Yes:
                    return
                self.stop_session()
                self.catalog = proposed
                self.persist()
                self.fill_games()
                self.note(f'Imported {len(entries)} games')
            except Exception as exc:
                self.note(f'Import not applied: {exc}')

        def import_legacy(self):
            path, _ = QFileDialog.getOpenFileName(self, 'Select old AutoAdv settings v2', '', 'JSON (*.json)')
            if not path:
                return
            try:
                entries = inspect_legacy(Path(path))
                proposed = add_legacy_games(self.catalog, entries)
                summary = '\n'.join(f'{row.game.name}: {len(row.game.profiles)} profiles' +
                                    (f'\n  Requires manual review: {"; ".join(row.unmapped)}' if row.unmapped else '')
                                    for row in entries)
                if QMessageBox.question(self, 'Confirm previous AutoAdv import',
                        'Import recognized settings as additional games? Source is read-only; '
                        'unmapped fields remain in the source.\n' + summary) != QMessageBox.StandardButton.Yes:
                    return
                self.stop_session()
                self.catalog = proposed
                self.persist()
                self.fill_games()
                self.note(f'Imported {len(entries)} previous AutoAdv games; review unmapped fields')
            except Exception as exc:
                self.note('Previous AutoAdv import not applied: ' + str(exc))

        def find_hooks(self):
            if not self.hook_host:
                self.note('Start and attach LunaHook first')
                return
            self.found_hooks.setRowCount(0)
            try:
                self.hook_host.command('find_hooks', pid=self.pid.value())
            except Exception as exc:
                self.note(str(exc))

        def insert_found(self):
            row = self.found_hooks.currentRow()
            code = self.found_hooks.item(row, 0).text() if row >= 0 and self.found_hooks.item(row, 0) else ''
            if code:
                self.manual_hook.setText(code)
                self.insert_hook()

        def insert_hook(self):
            p = self.current_profile()
            code = self.manual_hook.text().strip()
            if not p or not code:
                return
            try:
                if not self.hook_host:
                    raise RuntimeError('attach the game before inserting HOOK')
                self.hook_host.command('insert_hook', pid=self.pid.value(), code=code)
                if code not in p.inserted_hooks:
                    p.inserted_hooks.append(code)
                self.persist()
                self.note('Manual HOOK saved and inserted: ' + code)
            except Exception as exc:
                self.note(str(exc))

        def start_server(self):
            self.persist()
            if self.server:
                self.server.close()
            self.server = IrodoriServer(Path(self.catalog.irodori_root) if self.catalog.irodori_root else None,
                                        url=self.catalog.irodori_url)
            server = self.server
            self.server_status.setText('Irodori: connecting')
            def work():
                try:
                    self.bridge.server.emit(server.connect_or_start())
                except Exception as exc:
                    self.bridge.server.emit(str(exc))
            threading.Thread(target=work, daemon=True).start()

        def on_server(self, result):
            if isinstance(result, str):
                self.server_status.setText('Irodori: ' + result)
                return
            self.server_status.setText(f'Irodori: {result.status} ({result.origin}); {result.message}')
            if result.status == 'connected':
                for control in (self.default_voice, self.speaker_voice):
                    existing = control.currentText()
                    control.clear()
                    control.addItems(list(result.voices))
                    control.setEditText(existing)

        def start_host(self):
            self.stop_session()
            self.persist()
            if self.hook_host:
                self.hook_host.close()
            self.host_generation = ''
            self.clear_hook_observations()
            try:
                runtime = adopted_runtime()
                self.hook_host = HookHost(runtime.host, self.bridge.hook.emit, self.bridge.closed.emit)
                self.hook_host.start()
                self.host_status.setText('LunaHook Host: launched')
            except Exception as exc:
                self.host_status.setText(f'LunaHook Host: {exc}')

        def launch_game(self):
            game = self.catalog.games.get(self.game_id)
            if not game or not game.executable.strip():
                self.note('Specify the game executable before launching')
                return
            try:
                target = Path(game.executable)
                if not target.is_file():
                    raise FileNotFoundError(target)
                subprocess.Popen([str(target)], cwd=str(target.parent))
                self.note('Game launch requested; choose its window after startup')
            except Exception as exc:
                self.note('Game launch failed: ' + str(exc))

        def detect_game(self):
            game = self.catalog.games.get(self.game_id)
            if not game:
                return
            try:
                windows = find_game_windows(game.executable)
                if not windows:
                    self.note('No matching visible game window; enter PID/HWND or launch game')
                    return
                options = [f'{w.title or "(untitled)"} — PID {w.pid}, HWND {hex(w.hwnd)}'
                           for w in windows]
                if len(windows) == 1:
                    index = 0
                else:
                    selected, accepted = QInputDialog.getItem(self, 'Choose game window',
                        'Executable matched several windows; choose the dialogue window', options, 0, False)
                    if not accepted:
                        return
                    index = options.index(selected)
                self.stop_session()
                self.pid.setValue(windows[index].pid)
                self.hwnd.setText(hex(windows[index].hwnd))
                self.note('Selected game window: ' + options[index])
            except Exception as exc:
                self.note('Game identification failed: ' + str(exc))

        def detect_registered_game(self):
            try:
                candidates = []
                seen = set()
                for gid, game in self.catalog.games.items():
                    if not game.executable:
                        continue
                    for window in find_game_windows(game.executable):
                        key = (gid, window.pid, window.hwnd)
                        if key not in seen:
                            seen.add(key)
                            candidates.append((gid, window))
                if not candidates:
                    self.note('No running game matched the saved executable paths')
                    return
                labels = [f'{self.catalog.games[gid].name}: {window.title or "(untitled)"} '
                          f'PID {window.pid}, HWND {hex(window.hwnd)}' for gid, window in candidates]
                if len(labels) > 1:
                    choice, ok = QInputDialog.getItem(self, 'Select registered game',
                        'Multiple matching windows; choose the game and dialogue window', labels, 0, False)
                    if not ok:
                        return
                    index = labels.index(choice)
                else:
                    index = 0
                gid, window = candidates[index]
                self.game_choice.setCurrentIndex(self.game_choice.findData(gid))
                self.pid.setValue(window.pid)
                self.hwnd.setText(hex(window.hwnd))
                self.note('Selected registered game: ' + labels[index])
            except Exception as exc:
                self.note('Registered-game discovery failed: ' + str(exc))

        def detect_associated_target(self, role: str):
            game = self.catalog.games.get(self.game_id)
            if not game:
                return
            executable = self.audio_exe.text().strip() if role == 'audio' else self.input_exe.text().strip()
            if not executable:
                self.note('Set the separate process executable in game settings first')
                return
            try:
                candidates = (find_game_processes(executable) if role == 'audio'
                              else find_game_windows(executable))
                if not candidates:
                    self.note('No matching process/window; enter the PID manually')
                    return
                choices = ([f'PID {pid} — {executable}' for pid in candidates] if role == 'audio'
                           else [f'{w.title or "(untitled)"} — PID {w.pid}, HWND {hex(w.hwnd)}'
                                 for w in candidates])
                if len(candidates) > 1:
                    choice, accepted = QInputDialog.getItem(self, 'Choose ' + role + ' process',
                                                           'Matching windows', choices, 0, False)
                    if not accepted:
                        return
                    index = choices.index(choice)
                else:
                    index = 0
                self.stop_session()
                selected = candidates[index]
                if role == 'audio':
                    self.audio_pid.setValue(selected)
                else:
                    self.input_pid.setValue(selected.pid)
                    self.hwnd.setText(hex(selected.hwnd))
                self.note('Selected ' + role + ' target: ' + choices[index])
            except Exception as exc:
                self.note('Target discovery failed: ' + str(exc))

        def session_target(self) -> Target:
            input_pid = self.input_pid.value() or self.pid.value()
            hook_pid = self.pid.value() or input_pid
            return Target(input_pid,
                          int(self.hwnd.text().strip(), 0),
                          hook_pid=hook_pid, audio_pid=self.audio_pid.value() or hook_pid)

        def connect_game(self):
            if not self.hook_host:
                self.note('Start LunaHook Host first')
                return
            try:
                p = self.current_profile()
                if p is None:
                    raise ValueError('select a game and profile')
                self.hook_host.command('settings', flush_ms=p.hook_flush_ms,
                    codepage=p.hook_codepage, max_buffer=p.hook_max_buffer, max_history=p.hook_max_history)
                self.hook_host.command('connect', pid=self.pid.value())
                self.note('Hook injection requested; wait for connected event')
            except Exception as exc:
                self.note(str(exc))

        def clear_hook_observations(self):
            self.sources.clear()
            self.source_samples.clear()
            self.ambiguous_sources.clear()
            self.source_rows.clear()
            self.source_table.setRowCount(0)
            self.hook_preview.clear()
            self.refresh_hook_connection()

        def chosen_source(self):
            row = self.source_table.currentRow()
            if row < 0:
                return None
            item = self.source_table.item(row, 1)
            if item is None:
                return None
            key, address = item.data(256) or ('', -1)
            source = self.sources.get(key)
            return source if source and source.address == address and key not in self.ambiguous_sources else None

        def update_candidate(self, source: ThreadSource, sample: str | None):
            key = (source.identity, source.address)
            if key not in self.source_rows:
                row = self.source_table.rowCount()
                self.source_table.insertRow(row)
                self.source_rows[key] = row
            else:
                row = self.source_rows[key]
            profile = self.current_profile()
            role = 'candidate'
            if profile:
                for i, hook in enumerate(profile.hooks):
                    if hook.matches(source):
                        role = f'{hook.role} #{1 + sum(h.role == hook.role for h in profile.hooks[:i])}'
                        break
            if source.identity in self.ambiguous_sources:
                role = 'AMBIGUOUS'
            if sample is not None:
                history = self.source_samples.setdefault(source.identity, deque(maxlen=4))
                if not history or history[-1] != sample:
                    history.append(sample)
            latest = self.source_samples.get(source.identity, ())
            values = (role, f'{source.hook_code} / {source.hook_name}', str(source.process_id),
                      f'{source.context} / {source.context2}', hex(source.address), latest[-1] if latest else '')
            for col, value in enumerate(values):
                item = self.source_table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.source_table.setItem(row, col, item)
                item.setText(value)
                if col == 1:
                    item.setData(256, key)
            if self.source_table.currentRow() == row:
                self.show_hook_preview()

        def refresh_selected_hooks(self):
            profile = self.current_profile()
            hooks = profile.hooks if profile else []
            selected = self.saved_hook_choice.currentData()
            self.saved_hook_choice.blockSignals(True)
            self.saved_hook_choice.clear()
            for index, hook in enumerate(hooks):
                self.saved_hook_choice.addItem(f'{index + 1}. {hook.role}: {hook.identity}', hook.identity)
            chosen = self.saved_hook_choice.findData(selected)
            self.saved_hook_choice.setCurrentIndex(chosen if chosen >= 0 else (0 if hooks else -1))
            self.saved_hook_choice.blockSignals(False)
            self.selected_hooks.setText('Selected HOOKs: ' + ', '.join(f'{h.role}: {h.code}' for h in hooks) if hooks else 'Selected HOOKs: none')
            self.refresh_hook_pairs()
            for source in self.sources.values():
                self.update_candidate(source, None)
            self.refresh_hook_connection()

        def move_saved_hook(self, movement):
            profile = self.current_profile()
            if not profile:
                return
            index = self.saved_hook_choice.currentIndex()
            target = index + movement
            if index < 0 or target < 0 or target >= len(profile.hooks):
                return
            self.stop_session()
            profile.hooks[index], profile.hooks[target] = profile.hooks[target], profile.hooks[index]
            self.persist()
            self.refresh_selected_hooks()
            self.saved_hook_choice.setCurrentIndex(target)
            self.note('HOOK priority saved; restart monitoring to apply')

        def refresh_hook_connection(self):
            if not hasattr(self, 'hook_reconnect'):
                return
            profile = self.current_profile()
            if not profile or not profile.hooks:
                self.hook_reconnect.setText('Saved HOOKs: none selected')
                return
            ready = [h for h in profile.hooks if h.identity in self.sources and h.identity not in self.ambiguous_sources]
            absent = [h for h in profile.hooks if h.identity not in self.sources]
            ambiguous = [h for h in profile.hooks if h.identity in self.ambiguous_sources]
            self.hook_reconnect.setText(f'Saved HOOKs: {len(ready)}/{len(profile.hooks)} observed; '
                f'{len(absent)} waiting; {len(ambiguous)} ambiguous. Missing selections require user review.')

        def on_hook(self, event: HookEvent):
            if event.kind == 'hook_found' and event.process_id == self.pid.value():
                row = self.found_hooks.rowCount()
                self.found_hooks.insertRow(row)
                self.found_hooks.setItem(row, 0, QTableWidgetItem(event.detail))
                self.found_hooks.setItem(row, 1, QTableWidgetItem(event.text))
                self.note('HOOK found: ' + event.detail)
            if event.kind == 'connected' and event.process_id == self.pid.value():
                if self.connection_generation != event.connection_generation or self.host_generation != event.host_generation:
                    self.stop_session()
                    self.clear_hook_observations()
                    self.found_hooks.setRowCount(0)
                self.host_generation = event.host_generation
                self.connection_generation = event.connection_generation
                self.validation_trace.emit('hook_connected', pid=event.process_id,
                    generation=event.host_generation, connection=event.connection_generation)
                self.host_status.setText(f'LunaHook connected: PID {event.process_id}')
                p = self.current_profile()
                if p and p.source == 'hook':
                    for code in p.inserted_hooks:
                        self.hook_host.command('insert_hook', pid=event.process_id, code=code)
            elif event.kind == 'disconnected' and event.process_id == self.pid.value():
                self.stop_session()
                self.clear_hook_observations()
                self.host_status.setText('LunaHook disconnected')
            if (event.kind == 'removed' and event.source and event.process_id == self.pid.value()
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                key = event.source.identity
                if self.sources.get(key) and self.sources[key].address == event.source.address:
                    self.sources.pop(key, None)
                if self.controller.active_text_source == key:
                    self.stop_session()
                    self.note('Active dialogue HOOK disappeared; restart monitoring after checking sources')
                row = self.source_rows.pop((key, event.source.address), None)
                if row is not None:
                    self.source_table.removeRow(row)
                    for k, r in tuple(self.source_rows.items()):
                        if r > row:
                            self.source_rows[k] = r - 1
                self.source_samples.pop(key, None)
                self.refresh_hook_connection()
            if (event.kind in ('created', 'output') and event.source and event.process_id == self.pid.value()
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                source = event.source
                key = source.identity
                current = self.sources.get(key)
                if current and (current.process_id, current.address) != (source.process_id, source.address):
                    self.ambiguous_sources.add(key)
                    if self.controller.profile:
                        self.stop_session()
                    self.note('HOOK identity collision; select a non-conflicting source before restarting monitoring')
                    self.update_candidate(current, None)
                else:
                    self.sources[key] = source
                self.update_candidate(source, event.text if event.kind == 'output' else None)
                self.refresh_hook_connection()
            if event.kind in ('created', 'output') and event.source and event.process_id == self.pid.value():
                self.sync_hook_threads()
            if event.kind == 'output' and event.source:
                if (self.controller.profile and self.controller.profile.source == 'hook'
                        and event.process_id == (self.controller.target.hook_pid or self.controller.target.pid)
                        and event.host_generation == self.controller.source_generation
                        and event.connection_generation == self.controller.connection_generation
                        and any(h.matches(event.source) for h in self.controller.profile.hooks)):
                    self.validation_trace.emit('hook_output', session=self.controller.session,
                        generation=event.host_generation, connection=event.connection_generation,
                        hook_sequence=event.sequence, source=event.source.identity,
                        received_ns=event.received_ns, pid=event.process_id, address=event.source.address,
                        context=event.source.context, context2=event.source.context2,
                        raw_hash=text_digest(event.text))
                if (event.process_id == self.pid.value() and event.source.identity not in self.ambiguous_sources
                        and event.host_generation == self.host_generation
                        and event.connection_generation == self.connection_generation):
                    selected = self.controller.profile.hooks if self.controller.profile else ()
                    if len(selected) > 1 and any(h.matches(event.source) for h in selected):
                        # Luna's Python multi-HOOK collector groups already-flushed
                        # outputs; it does not re-flush or reconstruct raw bytes.
                        self._output_group.append(event)
                        if len(self._output_group) >= 32:
                            self._output_timer.stop()
                            self.flush_selected_outputs()
                        else:
                            self._output_timer.start(max(10, min(100, self.controller.profile.hook_flush_ms)))
                    else:
                        result = self.controller.observe(generation=event.host_generation,
                            connection=event.connection_generation, sequence=event.sequence,
                            source=event.source, text=event.text, pid=event.process_id or 0,
                            received_ns=event.received_ns)
                        if result and self.controller.utterance:
                            self.display_utterance(result)
            if event.kind == 'diagnostic' and event.detail.startswith(('command_failed', 'injection_conflict')):
                self.note(event.detail)

        def select_hook(self, role: str):
            p = self.current_profile()
            source = self.chosen_source()
            if not p or not source:
                self.note('Observe and select a Hook source first')
                return
            self.stop_session()
            selection = HookSelection(source.hook_code, source.context, source.context2, role)
            if selection not in p.hooks:
                if any(h.code == selection.code and h.context == selection.context and
                       h.context2 == selection.context2 for h in p.hooks):
                    self.note('This HOOK already has another role; remove it before reassigning')
                    return
                p.hooks.append(selection)
            self.persist()
            self.refresh_selected_hooks()

        def refresh_hook_pairs(self):
            self.paired_text_hook.clear()
            self.paired_speaker_hook.clear()
            p = self.current_profile()
            if p:
                for hook in p.hooks:
                    control = self.paired_text_hook if hook.role == 'text' else self.paired_speaker_hook
                    control.addItem(hook.identity, hook.identity)

        def pair_hooks(self):
            p = self.current_profile()
            text_source = self.paired_text_hook.currentData()
            speaker_source = self.paired_speaker_hook.currentData()
            if p and text_source and speaker_source:
                p.speaker_links[text_source] = speaker_source
                p.validate()
                self.persist()
                self.note(f'Speaker HOOK {speaker_source} paired to {text_source}')

        def unselect_hook(self):
            p = self.current_profile()
            identity = self.saved_hook_choice.currentData()
            if p and identity:
                self.stop_session()
                p.hooks = [h for h in p.hooks if h.identity != identity]
                p.speaker_links = {t: v for t, v in p.speaker_links.items()
                                   if t != identity and v != identity}
                self.persist()
                self.refresh_selected_hooks()

        def show_hook_preview(self, *_):
            source = self.chosen_source()
            history = self.source_samples.get(source.identity, ()) if source else ()
            self.hook_preview.setPlainText('\n── Previous output ──\n'.join(reversed(history)))


        def begin_monitor(self):
            try:
                if not self.save_profile():
                    return
                p = self.current_profile()
                target = self.session_target()
                if not p:
                    raise ValueError('select a game and profile')
                p = self.catalog.effective_profile(p)
                self.jobs.cancel()
                self.stop_session()
                if p.source == 'ocr':
                    session = self.controller.begin(p, target, 'ocr', 1)
                    reader = TesseractOCR(p.ocr, WindowsClientCapture(target.hwnd, target.pid))
                    self.ocr_worker = OCRWorker(reader,
                        lambda seq, txt, ss=session, pid=target.pid: self.bridge.ocr.emit(ss, pid, seq, txt),
                        self.bridge.ocr_error.emit)
                    self.ocr_worker.start()
                else:
                    if any(h.identity in self.ambiguous_sources for h in p.hooks):
                        raise ValueError('Selected HOOK identity is ambiguous; reconnect or choose a different source')
                    self.controller.begin(p, target,
                                          self.host_generation, self.connection_generation,
                                          tuple(self.sources))
                    self.sync_hook_threads()
                if sys.platform == 'win32':
                    self.manual_monitor = WindowsManualMonitor(target.hwnd)
                self.start_native_audio_if_needed(p)
                self.validation_trace.emit('monitor_started', session=self.controller.session,
                    source=p.source, game_id=self.game_id, profile_id=self.profile_id,
                    hook_pid=target.hook_pid or target.pid, audio_pid=target.audio_pid or target.pid,
                    input_pid=target.pid, generation=self.controller.source_generation,
                    connection=self.controller.connection_generation)
                self.note('Monitoring started; auto advance remains unauthorized')
            except Exception as exc:
                self.stop_session()
                self.note(f'Monitor not started: {exc}')
            self.refresh_status()

        def sync_hook_threads(self, *, clear: bool = False):
            """Apply Luna's selected-thread flush synchronization, not our own."""
            profile = None if clear else self.controller.profile
            selected = (profile.hooks if profile and profile.source == 'hook' and
                        len(profile.hooks) > 1 else ())
            desired = {}
            for source in self.sources.values():
                if (source.process_id == self.pid.value() and
                        source.identity not in self.ambiguous_sources and
                        any(h.matches(source) for h in selected)):
                    key = (source.process_id, source.address, source.context, source.context2)
                    desired[key] = source
            for key, source in tuple(self._synced_sources.items()):
                if key not in desired:
                    try:
                        if self.hook_host:
                            self.hook_host.command('sync_thread', source=source.__dict__, enabled=False)
                    except (RuntimeError, OSError, ValueError):
                        pass
                    self._synced_sources.pop(key, None)
            if self.hook_host:
                for key, source in desired.items():
                    if key not in self._synced_sources:
                        try:
                            self.hook_host.command('sync_thread', source=source.__dict__, enabled=True)
                            self._synced_sources[key] = source
                        except (RuntimeError, OSError, ValueError) as exc:
                            self.note('Luna selected-HOOK sync unavailable: ' + str(exc))

        def flush_selected_outputs(self):
            group, self._output_group = self._output_group, []
            if not group or not self.controller.profile:
                return
            result = self.controller.observe_group(group)
            if result and self.controller.utterance:
                self.display_utterance(result)
            else:
                self.refresh_status()

        def start_native_audio_if_needed(self, profile: Profile):
            if profile.advance_mode != AdvanceMode.AUDIO:
                return
            has_asr = bool(profile.asr_model and profile.asr_tokens)
            if not profile.native_helper or not has_asr:
                # This is a per-utterance prerequisite, not a session-wide
                # pause: audio-mode TTS lines require only playback drain.
                # A non-TTS line enters the normal uncertain-audio fallback
                # or manual-handoff decision when its text deadline arrives.
                self.note('非TTSの音声判断にはProcess LoopbackとASRモデル・tokensが必要です。根拠不足では設定に従います。')
                return
            target = self.controller.target
            if not target:
                return
            session = self.controller.session
            def load_native():
                native = None
                try:
                    native = NativeAudio(target.audio_pid or target.pid, Path(profile.native_helper),
                        Path(profile.asr_model), Path(profile.asr_tokens),
                        lambda token, event, detail: self.bridge.native.emit(token, event, detail),
                        on_pcm=(lambda token, frames: self.bridge.native_pcm.emit(token, frames))
                        if self.validation_trace.path else None,
                        on_inference=(lambda token, metrics:
                            self.validation_trace.emit('asr_inference', session=token.session,
                                line=token.line, **metrics))
                        if self.validation_trace.path else None,
                        on_voice_end=(lambda token, metrics:
                            self.validation_trace.emit('native_voice_end_estimate',
                                session=token.session, line=token.line,
                                estimated_at_ns=metrics['estimated_end_ms'] * 1_000_000,
                                onset_estimated_at_ns=metrics['estimated_onset_ms'] * 1_000_000,
                                evidence=metrics['source']))
                        if self.validation_trace.path else None)
                    native.start()
                    self.bridge.native_ready.emit(session, native, '')
                except Exception as exc:
                    if native:
                        native.close()
                    self.bridge.native_ready.emit(session, None, str(exc))
            self.note('Loading native audio capture without blocking game UI')
            threading.Thread(target=load_native, daemon=True, name='autoadv-load-native').start()

        def on_native_ready(self, session, native, reason):
            if session != self.controller.session or not self.controller.target:
                if native:
                    threading.Thread(target=native.close, daemon=True).start()
                return
            if reason:
                self.note('Native audio unavailable: ' + reason)
                u = self.controller.utterance
                if u and u.advance_mode == AdvanceMode.AUDIO and not u.use_tts and not u.uncertain_text_fallback:
                    self.controller.pause('native_audio_unavailable_manual_handoff')
                self.refresh_status()
                return
            self.native_audio = native
            self.validation_trace.emit('native_ready', session=session, audio_pid=native.pid)
            self.note('Native process audio and ASR ready')
            token, u = self.controller.token, self.controller.utterance
            if token and u and not self.controller.sent and not self.controller.completed and u.advance_mode == AdvanceMode.AUDIO and not u.use_tts:
                native.begin(token, u.match_text)

        def on_native_pcm(self, token, frames):
            if token is not None and token == self.controller.token:
                self.validation_trace.emit('native_pcm', session=token.session, line=token.line,
                    frames=frames, audio_pid=getattr(self.native_audio, 'pid', 0))

        def on_native(self, token, event, detail):
            if token != self.controller.token or token is None:
                return
            self.validation_trace.emit('native_event', session=token.session, line=token.line,
                status=event, method='asr')
            u = self.controller.utterance
            if not u or u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return
            if event == 'error':
                self.note('Native audio: ' + detail)
                if not u.uncertain_text_fallback:
                    self.controller.pause('native_audio_failure')
            elif event == 'started':
                self.presentation_event(token, 'native_started')
            elif event == 'quiet':
                self.presentation_event(token, 'native_quiet')
            elif event == 'completed':
                self.presentation_event(token, 'native_finished')
            self.refresh_status()

        def display_utterance(self, token: Token):
            u = self.controller.utterance
            if not u:
                return
            self.preview.setPlainText(f'Original: {u.raw}\\nSpeaker: {u.speaker}\\nText: {u.text}\\nReading: {u.reading}\\nMatch: {u.match_text}\\nTTS: {u.use_tts}; Voice: {u.voice}; Advance: {u.advance_mode.value}')
            self.wait_preview.setText(f'{u.character_count}文字 × {self.controller.profile.per_character_wait_ms}ms / 最低{self.controller.profile.reading_ms}ms → {u.text_wait_ms}ms')
            self.validation_trace.emit('utterance', session=token.session, line=token.line,
                source=u.source, hook_sequence=u.sequence, raw_hash=text_digest(u.raw),
                body_hash=text_digest(u.text), speaker=u.speaker, advance_mode=u.advance_mode.value, use_tts=u.use_tts,
                characters=u.character_count, text_wait_ms=u.text_wait_ms,
                uncertain_text_fallback=u.uncertain_text_fallback)
            # Luna emits an already-flushed sentence; do not reimplement a second flush timer.
            self.present(token)
            self.refresh_status()

        def present(self, token: Token):
            u = self.controller.utterance
            if not u or token != self.controller.token:
                return
            self.jobs.cancel()
            if self.native_audio:
                self.native_audio.cancel()
            if u.advance_mode == AdvanceMode.TEXT:
                QTimer.singleShot(u.text_wait_ms, lambda t=token: self.presentation_event(t, 'text_elapsed'))
            elif not u.use_tts:
                if self.native_audio:
                    self.native_audio.begin(token, u.match_text)
                else:
                    self.note('Native observation unavailable; uncertain audio cannot be certified')
                # The same reading deadline governs opt-in continuation and
                # opt-out manual handoff. It never certifies native audio end.
                QTimer.singleShot(u.text_wait_ms, lambda t=token: self.fallback_due(t))
            if u.use_tts:
                if not u.voice:
                    self.presentation_event(token, 'tts_finished', success=False)
                    self.note('TTS requires a configured Voice')
                else:
                    self.jobs.start(token, self.catalog.irodori_url, u.reading, u.voice)

        def fallback_due(self, token):
            u = self.controller.utterance
            if (token != self.controller.token or not u or u.use_tts or
                    u.advance_mode != AdvanceMode.AUDIO or self.controller.completed or self.controller.sent):
                return
            if self.native_audio and self.native_audio.holds_deadline(token):
                # A correlated voice is active, or a bounded real ASR analysis
                # of captured speech is pending. Neither grants completion.
                QTimer.singleShot(200, lambda t=token: self.fallback_due(t))
                return
            if self.controller.native_playing:
                self.presentation_event(token, 'native_evidence_stale')
            self.presentation_event(token, 'fallback_elapsed')

        def on_voice(self, token, success, reason):
            if token == self.controller.token and token is not None:
                self.validation_trace.emit('tts_result', session=token.session,
                    line=token.line, success=bool(success))
            self.presentation_event(token, 'tts_finished', success=success)
            if reason and not success and token == self.controller.token:
                self.note(reason)

        def presentation_event(self, token, event, *, success=True):
            if self.controller.presentation_event(token, event, success=success):
                self.validation_trace.emit('presentation_complete', session=token.session,
                    line=token.line, success=True, reason=self.controller.presentation_reason)
                self.advance_if_ready()
            self.refresh_status()

        def on_ocr(self, session, pid, sequence, text):
            token = self.controller.observe_ocr(session=session, pid=pid, sequence=sequence, text=text)
            if token:
                self.display_utterance(token)

        def on_ocr_error(self, reason):
            self.note('OCR stopped: ' + reason)
            self.controller.pause('ocr_failure')
            self.refresh_status()

        def check_intervention(self):
            if self.manual_monitor and self.controller.authorized and self.manual_monitor.intervened():
                self.controller.pause('manual_intervention')
                self.refresh_status()

        def preflight(self, target: Target) -> bool:
            if sys.platform != 'win32' or not self.manual_monitor or self.manual_monitor.intervened():
                return False
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            focus = user32.GetForegroundWindow()
            pid = ctypes.c_uint32()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(focus), ctypes.byref(pid))
            return focus == target.hwnd and pid.value == target.pid

        def send_input(self, target: Target, virtual_key: int):
            token = self.controller.token
            if token:
                self.validation_trace.emit('advance_attempt', session=token.session,
                    line=token.line)
            # Start next-line observation at the input operation, before the
            # game can render/play ahead of Luna's delayed HOOK flush.
            if self.native_audio:
                self.native_audio.boundary()
                if token:
                    self.validation_trace.emit('audio_observation_boundary',
                        session=token.session, after_line=token.line)
            WindowsAdvancementInput(target.hwnd, virtual_key=virtual_key).advance()
            if token:
                self.validation_trace.emit('advance_input', session=token.session,
                    line=token.line)

        def advance_if_ready(self):
            if not self.controller.try_advance():
                return
            token = self.controller.token
            timeout = self.controller.profile.next_line_timeout_ms
            QTimer.singleShot(timeout, lambda t=token: self.expire_next_line(t))

        def expire_next_line(self, token):
            if self.controller.expire_next_line(token):
                self.validation_trace.emit('next_line_timeout', session=token.session, line=token.line)
                self.note('No next dialogue output after advance; manual control restored')
                self.refresh_status()

        def permit(self):
            self.validation_trace.emit('permit_requested', session=self.controller.session)
            if self.manual_monitor:
                self.manual_monitor.reset_after_authorization()
            self.controller.permit(True)
            self.advance_if_ready()
            self.refresh_status()

        def pause(self):
            self.validation_trace.emit('pause_requested', session=self.controller.session)
            self.controller.pause()
            self.refresh_status()

        def refresh_status(self):
            c = self.controller
            # Observe a product handoff once per actual line/reason. This
            # records why AutoAdv stopped without inspecting or driving the
            # game's interface, and cannot confer input authority.
            token = c.token
            if self.validation_trace.path and token is not None and c.pause_reason:
                marker = (token, c.pause_reason)
                if marker != getattr(self, '_last_validation_handoff', None):
                    self.validation_trace.emit('controller_handoff', session=token.session,
                        line=token.line, reason=c.pause_reason)
                    self._last_validation_handoff = marker
            targets = (f'HOOK {c.target.hook_pid or c.target.pid}; audio {c.target.audio_pid or c.target.pid}; '
                       f'input {c.target.pid}/{hex(c.target.hwnd)}') if c.target else 'no target'
            self.status.setText(f'Status: {c.reason} | permission={c.authorized} | '
                                f'line={c.line_no} | sent={c.sent} | completion={c.presentation_reason or "pending"} | {targets}')
            if c.profile and c.profile.source == 'hook':
                self.active_hook_status.setText('Active dialogue HOOK: ' +
                    (c.active_text_source or 'waiting for selected source output'))
            else:
                self.active_hook_status.setText('Active dialogue HOOK: not monitoring')

        def stop_session(self):
            self.validation_trace.emit('monitor_ended', session=self.controller.session)
            if hasattr(self, '_output_timer'):
                self._output_timer.stop()
                self._output_group.clear()
            if hasattr(self, '_synced_sources') and self._synced_sources:
                self.sync_hook_threads(clear=True)
            if self.manual_monitor:
                monitor, self.manual_monitor = self.manual_monitor, None
                threading.Thread(target=monitor.close, daemon=True).start()
            if self.native_audio:
                native, self.native_audio = self.native_audio, None
                threading.Thread(target=native.close, daemon=True).start()
            self.native_token = None
            if self.ocr_worker:
                worker, self.ocr_worker = self.ocr_worker, None
                threading.Thread(target=worker.close, daemon=True).start()
            self.jobs.cancel()
            self.preview_jobs.cancel()
            self.controller.close('session_changed')
            if hasattr(self, 'preview'):
                self.preview.clear()
            if hasattr(self, 'status'):
                self.refresh_status()

        def on_closed(self, reason):
            self.stop_session()
            self.host_status.setText(f'LunaHook Host stopped: {reason}')

        def closeEvent(self, event):
            self.stop_session()
            if self.hook_host:
                self.hook_host.close()
            if self.server:
                self.server.close()
            super().closeEvent(event)

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    if session_driver is not None:
        QTimer.singleShot(0, lambda: session_driver(window, app))
    return app.exec()
