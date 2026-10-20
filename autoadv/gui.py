"""A single GUI integrating game profiles, LunaHook, Irodori, and advancement."""
from __future__ import annotations
import ctypes
import sys
import threading
import copy
from pathlib import Path

from .conversation import Controller, Target, Token
from .hook_host import HookHost
from .hook_protocol import HookEvent, ThreadSource
from .irodori_server import IrodoriServer
from .model import AudioMode, HookSelection, Profile, StructureRule, ReplacementRule
from .storage import Catalog, load, save
from .voice import VoiceJobs
from .native_audio import NativeAudio
from .manual_input import WindowsManualMonitor
from .ocr import OCRWorker, TesseractOCR
from .screen import WindowsClientCapture
from .import_luna import inspect_luna, add_luna_games
from .game_windows import find_game_windows
from .windows_input import WindowsAdvancementInput


def run_gui() -> int:
    from PySide6.QtCore import QObject, Signal, QTimer
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                                   QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
                                   QLabel, QPushButton, QComboBox, QFileDialog,
                                   QSpinBox, QGroupBox, QMessageBox, QCheckBox, QScrollArea, QInputDialog)

    class SignalBridge(QObject):
        hook = Signal(object)
        closed = Signal(str)
        voice = Signal(object, bool, str)
        server = Signal(object)
        ocr = Signal(int, int, int, str)
        ocr_error = Signal(str)
        native = Signal(object, str, str)
        native_ready = Signal(int, object, str)
        preview = Signal(bool, str)

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('AutoAdv — HOOK / Irodori / Advance')
            self.resize(1000, 950)
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
            self.bridge.preview.connect(self.on_preview)
            self.jobs = VoiceJobs(lambda token, done, reason: self.bridge.voice.emit(token, done, reason))
            self.preview_jobs = VoiceJobs(lambda token, done, reason: self.bridge.preview.emit(done, reason))
            self.controller = Controller(self.send_input, self.preflight)
            self.hook_host: HookHost | None = None
            self.host_generation = ''
            self.connection_generation = 0
            self.sources: dict[str, ThreadSource] = {}
            self.source_samples: dict[str, str] = {}
            self.game_id = ''
            self.profile_id = ''
            self.server: IrodoriServer | None = None
            self.ocr_worker: OCRWorker | None = None
            self.native_audio: NativeAudio | None = None
            self.manual_monitor: WindowsManualMonitor | None = None
            self.native_token: Token | None = None
            self.native_started = False
            self.auto_tts_started = False
            self._known_game_pid = None
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
            self.dll_path = QLineEdit(self.catalog.host_dll)
            self.irodori_url = QLineEdit(self.catalog.irodori_url)
            environment.addRow('Installed Irodori folder', self.irodori_path)
            environment.addRow('Irodori URL', self.irodori_url)
            environment.addRow('LunaHost.dll (matching build)', self.dll_path)
            self.btn_server = QPushButton('Connect / start Irodori and read Voices')
            environment.addRow(self.btn_server)
            self.btn_server.clicked.connect(self.start_server)
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
            self.btn_import = QPushButton('Import existing Luna game settings (preview and confirm)')
            games.addRow(self.btn_import)
            self.btn_import.clicked.connect(self.import_luna)
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
            capture.addRow('Game PID (current run)', self.pid)
            capture.addRow('Game HWND (current run)', self.hwnd)
            self.attach = QPushButton('Attach to game PID')
            self.attach.clicked.connect(self.connect_game)
            capture.addRow(self.attach)
            self.btn_discover = QPushButton('Find windows matching registered game executable')
            self.btn_discover.clicked.connect(self.detect_game)
            capture.addRow(self.btn_discover)
            self.btn_find = QPushButton('Find HOOK candidates')
            self.btn_find.clicked.connect(self.find_hooks)
            capture.addRow(self.btn_find)
            self.manual_hook = QLineEdit()
            self.manual_hook.setPlaceholderText('Manual Luna HOOK code')
            capture.addRow('Manual HOOK code', self.manual_hook)
            self.btn_insert = QPushButton('Insert and save manual HOOK')
            self.btn_insert.clicked.connect(self.insert_hook)
            capture.addRow(self.btn_insert)
            self.found_hooks = QComboBox()
            capture.addRow('Search candidates (code and sample)', self.found_hooks)
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
            self.source_choice = QComboBox()
            self.source_choice.currentIndexChanged.connect(self.show_hook_preview)
            capture.addRow('Observed HOOK sources', self.source_choice)
            self.hook_preview = QPlainTextEdit()
            self.hook_preview.setReadOnly(True)
            self.hook_preview.setMaximumHeight(90)
            capture.addRow('Raw text from selected HOOK (before choosing)', self.hook_preview)
            row = QHBoxLayout()
            self.text_hook = QPushButton('Use as dialogue HOOK')
            self.speaker_hook = QPushButton('Use as speaker HOOK')
            self.text_hook.clicked.connect(lambda: self.select_hook('text'))
            self.speaker_hook.clicked.connect(lambda: self.select_hook('speaker'))
            row.addWidget(self.text_hook)
            row.addWidget(self.speaker_hook)
            capture.addRow(row)
            self.btn_unselect_hook = QPushButton('Remove selected HOOK role')
            self.btn_unselect_hook.clicked.connect(self.unselect_hook)
            capture.addRow(self.btn_unselect_hook)
            self.selected_hooks = QLabel('Selected HOOKs: none')
            capture.addRow(self.selected_hooks)
            self.btn_monitor = QPushButton('Start monitoring selected HOOK')
            self.btn_monitor.clicked.connect(self.begin_monitor)
            capture.addRow(self.btn_monitor)

            speech = group('Dialogue and voice routing')
            self.default_mode = QComboBox()
            self.default_mode.addItems([mode.value for mode in AudioMode])
            speech.addRow('Unknown speaker routing', self.default_mode)
            self.default_voice = QComboBox()
            self.default_voice.setEditable(True)
            speech.addRow('Default Irodori Voice ID', self.default_voice)
            self.speaker_name = QLineEdit()
            self.speaker_name.setPlaceholderText('Character name, empty = default')
            speech.addRow('Speaker override', self.speaker_name)
            self.speaker_mode = QComboBox()
            self.speaker_mode.addItems([m.value for m in AudioMode])
            speech.addRow('Speaker routing', self.speaker_mode)
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
            self.reading_ms.setRange(0, 600000)
            self.reading_ms.setValue(1200)
            speech.addRow('Reading time (ms)', self.reading_ms)
            self.native_helper = QLineEdit()
            self.asr_model = QLineEdit()
            self.asr_tokens = QLineEdit()
            self.native_wait = QSpinBox()
            self.native_wait.setRange(0, 20000)
            speech.addRow('Process audio helper DLL (native/auto only)', self.native_helper)
            speech.addRow('Japanese ASR model (native/auto only)', self.asr_model)
            speech.addRow('ASR tokens', self.asr_tokens)
            speech.addRow('Auto fallback wait (ms)', self.native_wait)
            self.repeat = QComboBox()
            self.repeat.addItems(['distinct_only', 'each_after_input'])
            speech.addRow('HOOK repeated-line behavior', self.repeat)
            self.btn_save = QPushButton('Apply and save profile')
            self.btn_save.clicked.connect(self.save_profile)
            speech.addRow(self.btn_save)
            self.preview = QPlainTextEdit()
            self.preview.setReadOnly(True)
            self.preview.setMaximumHeight(125)
            speech.addRow('Raw / speaker / reading / voice', self.preview)

            advance = group('Advance authority')
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
            self.fill_games()
            if self.irodori_path.text().strip():
                QTimer.singleShot(0, self.start_server)

        def note(self, message):
            self.log.appendPlainText(message)

        def persist(self):
            self.catalog.irodori_root = self.irodori_path.text().strip()
            self.catalog.irodori_url = self.irodori_url.text().strip()
            self.catalog.host_dll = self.dll_path.text().strip()
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
            self.profile_choice.blockSignals(False)
            self.choose_profile()

        def choose_profile(self, *_):
            self.stop_session()
            self.profile_id = self.profile_choice.currentData() or ''
            profile = self.current_profile()
            if not profile:
                return
            self.default_mode.setCurrentText(profile.default_mode.value)
            self.default_voice.setEditText(profile.default_voice)
            self.reading_ms.setValue(profile.reading_ms)
            self.repeat.setCurrentText(profile.repeat_rule)
            self.source_mode.setCurrentText(profile.source)
            self.ocr_exe.setText(profile.ocr.executable)
            self.ocr_lang.setText(profile.ocr.language)
            self.ocr_region.setText(','.join(map(str, profile.ocr.region)))
            self.native_helper.setText(profile.native_helper)
            self.asr_model.setText(profile.asr_model)
            self.asr_tokens.setText(profile.asr_tokens)
            self.native_wait.setValue(profile.native_wait_ms)
            self.refresh_rules()
            self.selected_hooks.setText('Selected HOOKs: ' + ', '.join(f'{h.role}: {h.code}' for h in profile.hooks))
            self.refresh_speaker_choices()

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
                    tuple(int(x.strip()) for x in self.ocr_region.text().split(',')))
                updated.native_helper = self.native_helper.text().strip()
                updated.asr_model = self.asr_model.text().strip()
                updated.asr_tokens = self.asr_tokens.text().strip()
                updated.native_wait_ms = self.native_wait.value()
                updated.default_mode = AudioMode(self.default_mode.currentText())
                updated.default_voice = self.default_voice.currentText().strip()
                updated.reading_ms = self.reading_ms.value()
                updated.repeat_rule = self.repeat.currentText()
                updated.validate()
                game = self.catalog.games[self.game_id]
                game.profiles[updated.id] = updated
                game.executable = self.game_exe.text().strip()
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
            p.speaker_modes[name] = AudioMode(self.speaker_mode.currentText())
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
                for name in p.speaker_modes:
                    self.speaker_overrides.addItem(name, name)
            self.refresh_rules()

        def show_speaker_override(self, *_):
            p = self.current_profile()
            name = self.speaker_overrides.currentData()
            if p and name:
                self.speaker_name.setText(name)
                self.speaker_mode.setCurrentText(p.speaker_modes[name].value)
                self.speaker_voice.setEditText(p.speaker_voices.get(name, ''))

        def remove_speaker(self):
            p = self.current_profile()
            name = self.speaker_overrides.currentData()
            if p and name:
                p.speaker_modes.pop(name, None)
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
                summary = '\n'.join(f'{Path(g.path).name}: {len(g.hooks)} HOOKs; {len(g.inserted)} manual' for g in entries)
                if QMessageBox.question(self, 'Confirm Luna import',
                        'Only known HOOKs and paths will be imported; original file unchanged.\n' + summary) != QMessageBox.StandardButton.Yes:
                    return
                self.stop_session()
                self.catalog = proposed
                self.persist()
                self.fill_games()
                self.note(f'Imported {len(entries)} games')
            except Exception as exc:
                self.note(f'Import not applied: {exc}')

        def find_hooks(self):
            if not self.hook_host:
                self.note('Start and attach LunaHook first')
                return
            self.found_hooks.clear()
            try:
                self.hook_host.command('find_hooks', pid=self.pid.value())
            except Exception as exc:
                self.note(str(exc))

        def insert_found(self):
            code = self.found_hooks.currentData()
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
            self.sources.clear()
            self.source_samples.clear()
            self.source_choice.clear()
            self.hook_preview.clear()
            self.hook_host = HookHost(Path(self.catalog.host_dll),
                                      self.bridge.hook.emit, self.bridge.closed.emit)
            try:
                self.hook_host.start()
                self.host_status.setText('LunaHook Host: launched')
            except Exception as exc:
                self.host_status.setText(f'LunaHook Host: {exc}')

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

        def connect_game(self):
            if not self.hook_host:
                self.note('Start LunaHook Host first')
                return
            try:
                self.hook_host.command('connect', pid=self.pid.value())
                self.note('Hook injection requested; wait for connected event')
            except Exception as exc:
                self.note(str(exc))

        def on_hook(self, event: HookEvent):
            if event.kind == 'hook_found' and event.process_id == self.pid.value():
                self.found_hooks.addItem(f'{event.detail or event.text[:55]}: {event.text[:60]}', event.detail)
                self.note('HOOK found: ' + event.detail)
            if event.kind == 'connected' and event.process_id == self.pid.value():
                if self.connection_generation != event.connection_generation or self.host_generation != event.host_generation:
                    self.stop_session()
                self.host_generation = event.host_generation
                self.connection_generation = event.connection_generation
                self.host_status.setText(f'LunaHook connected: PID {event.process_id}')
                p = self.current_profile()
                if p and p.source == 'hook':
                    for code in p.inserted_hooks:
                        self.hook_host.command('insert_hook', pid=event.process_id, code=code)
            elif event.kind == 'disconnected' and event.process_id == self.pid.value():
                self.stop_session()
                self.host_status.setText('LunaHook disconnected')
            if event.kind in ('created', 'output') and event.source and event.process_id == self.pid.value():
                key = event.source.identity
                if key in self.sources and self.sources[key] != event.source:
                    self.controller.pause('ambiguous_hook_source')
                    self.note('Multiple live HOOK threads share one saved identity; resolve selection before advancing')
                elif key not in self.sources:
                    self.sources[key] = event.source
                    self.source_choice.addItem(f'{event.source.hook_name} / {key}', key)
            if event.kind == 'output' and event.source:
                if event.process_id == self.pid.value():
                    self.source_samples[event.source.identity] = event.text
                    if self.source_choice.currentData() == event.source.identity:
                        self.show_hook_preview()
                result = self.controller.observe(generation=event.host_generation,
                    connection=event.connection_generation, sequence=event.sequence,
                    source=event.source, text=event.text, pid=event.process_id or 0)
                if result and self.controller.utterance:
                    self.display_utterance(result)
            if event.kind == 'diagnostic' and event.detail.startswith(('command_failed', 'injection_conflict')):
                self.note(event.detail)

        def select_hook(self, role: str):
            p = self.current_profile()
            source = self.sources.get(self.source_choice.currentData())
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
            self.selected_hooks.setText('Selected HOOKs: ' + ', '.join(f'{h.role}: {h.code}' for h in p.hooks))

        def unselect_hook(self):
            p = self.current_profile()
            source = self.sources.get(self.source_choice.currentData())
            if p and source:
                self.stop_session()
                p.hooks = [h for h in p.hooks if not h.matches(source)]
                self.persist()
                self.selected_hooks.setText('Selected HOOKs: ' + ', '.join(f'{h.role}: {h.code}' for h in p.hooks))

        def show_hook_preview(self, *_):
            self.hook_preview.setPlainText(self.source_samples.get(self.source_choice.currentData(), ''))

        def begin_monitor(self):
            try:
                if not self.save_profile():
                    return
                p = self.current_profile()
                hwnd = int(self.hwnd.text().strip(), 0)
                if not p:
                    raise ValueError('select a game and profile')
                self.jobs.cancel()
                self.stop_session()
                if p.source == 'ocr':
                    session = self.controller.begin(p, Target(self.pid.value(), hwnd), 'ocr', 1)
                    reader = TesseractOCR(p.ocr, WindowsClientCapture(hwnd, self.pid.value()))
                    self.ocr_worker = OCRWorker(reader,
                        lambda seq, txt, ss=session, pid=self.pid.value(): self.bridge.ocr.emit(ss, pid, seq, txt),
                        self.bridge.ocr_error.emit)
                    self.ocr_worker.start()
                else:
                    self.controller.begin(p, Target(self.pid.value(), hwnd),
                                          self.host_generation, self.connection_generation)
                if sys.platform == 'win32':
                    self.manual_monitor = WindowsManualMonitor(hwnd)
                self.start_native_audio_if_needed(p)
                self.note('Monitoring started; auto advance remains unauthorized')
            except Exception as exc:
                self.stop_session()
                self.note(f'Monitor not started: {exc}')
            self.refresh_status()

        def start_native_audio_if_needed(self, profile: Profile):
            modes = {profile.default_mode, *profile.speaker_modes.values()}
            if not modes.intersection({AudioMode.NATIVE, AudioMode.AUTO}):
                return
            if not (profile.native_helper and profile.asr_model and profile.asr_tokens):
                self.note('Native/AUTO requires Process Loopback helper and ASR paths; TTS and reading remain available')
                return
            target = self.controller.target
            if not target:
                return
            session = self.controller.session
            def load_native():
                native = None
                try:
                    native = NativeAudio(target.pid, Path(profile.native_helper),
                        Path(profile.asr_model), Path(profile.asr_tokens),
                        lambda token, event, detail: self.bridge.native.emit(token, event, detail))
                    native.start()
                    self.bridge.native_ready.emit(session, native, '')
                except Exception as exc:
                    if native:
                        native.close()
                    self.bridge.native_ready.emit(session, None, str(exc))
            self.note('Loading native audio/ASR without blocking game UI')
            threading.Thread(target=load_native, daemon=True, name='autoadv-load-native').start()

        def on_native_ready(self, session, native, reason):
            if session != self.controller.session or not self.controller.target:
                if native:
                    threading.Thread(target=native.close, daemon=True).start()
                return
            if reason:
                self.note('Native audio unavailable: ' + reason)
                return
            self.native_audio = native
            self.note('Native process audio and ASR ready')
            token = self.controller.token
            if token and self.controller.utterance and self.controller.utterance.mode in (AudioMode.NATIVE, AudioMode.AUTO):
                self.present(token)

        def on_native(self, token, event, detail):
            if not token or token != self.controller.token:
                return
            current = self.controller.utterance
            if not current or current.mode not in (AudioMode.NATIVE, AudioMode.AUTO):
                return
            if event == 'error':
                self.note('Native audio: ' + detail)
                if current.mode == AudioMode.NATIVE:
                    self.controller.pause('native_audio_failure')
            elif event == 'started':
                self.native_started = True
                if current.mode == AudioMode.AUTO:
                    self.jobs.cancel()
                self.note('Matching game speech detected')
            elif event == 'completed':
                self.complete(token, True)
            self.refresh_status()

        def auto_fallback(self, token):
            if (token != self.controller.token or not self.controller.utterance or
                    self.controller.utterance.mode != AudioMode.AUTO or self.native_started):
                return
            current = self.controller.utterance
            if not current.voice:
                self.note('AUTO requires a fallback Irodori Voice; advance held')
                self.controller.pause('auto_voice_unavailable')
            else:
                self.auto_tts_started = True
                self.note('No matching native voice yet: starting configured TTS fallback')
                self.jobs.start(token, self.catalog.irodori_url, current.reading, current.voice)
            self.refresh_status()

        def display_utterance(self, token: Token):
            u = self.controller.utterance
            if not u:
                return
            self.preview.setPlainText(f'Original: {u.raw}\nSpeaker: {u.speaker}\nText: {u.text}\nReading: {u.reading}\nMatch: {u.match_text}\nRoute: {u.mode.value}; Voice: {u.voice}')
            settling = self.controller.profile.settle_ms
            QTimer.singleShot(settling, lambda t=token: self.present(t) if self.controller.token == t else None)
            self.refresh_status()

        def present(self, token: Token):
            u = self.controller.utterance
            if not u or token != self.controller.token:
                return
            self.jobs.cancel()
            self.native_token = token
            self.native_started = False
            self.auto_tts_started = False
            if self.native_audio:
                self.native_audio.cancel()
                if u.mode in (AudioMode.NATIVE, AudioMode.AUTO):
                    self.native_audio.begin(token, u.match_text)
            if u.mode == AudioMode.READING:
                QTimer.singleShot(self.controller.profile.reading_ms, lambda: self.complete(token, True))
            elif u.mode == AudioMode.TTS:
                if not u.voice:
                    self.complete(token, False, 'TTS requires a Voice')
                else:
                    self.jobs.start(token, self.catalog.irodori_url, u.reading, u.voice)
            elif self.native_audio is None:
                self.note(f'{u.mode.value} requires configured native audio/ASR; advance held')
                self.controller.pause('native_audio_unavailable')
            elif u.mode == AudioMode.AUTO:
                QTimer.singleShot(self.controller.profile.native_wait_ms, lambda t=token: self.auto_fallback(t))
            else:
                self.note('Waiting for matching native speech to finish')

        def on_voice(self, token, success, reason):
            if (token == self.controller.token and self.controller.utterance and
                    self.controller.utterance.mode == AudioMode.AUTO and self.native_started):
                return  # Once native speech is attributed, cancelled TTS may not advance.
            self.complete(token, success, reason)

        def complete(self, token, success, reason=''):
            if self.controller.mark_complete(token, success=success):
                self.controller.try_advance()
            elif reason:
                self.note(reason)
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
            WindowsAdvancementInput(target.hwnd, virtual_key=virtual_key).advance()
            if self.native_audio:
                self.native_audio.boundary()

        def permit(self):
            if self.manual_monitor:
                self.manual_monitor.reset_after_authorization()
            self.controller.permit(True)
            self.controller.try_advance()
            self.refresh_status()

        def pause(self):
            self.controller.pause()
            self.refresh_status()

        def refresh_status(self):
            c = self.controller
            self.status.setText(f'Status: {c.reason} | permission={c.authorized} | line={c.line_no} | sent={c.sent}')

        def stop_session(self):
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
    return app.exec()
