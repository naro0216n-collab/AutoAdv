"""A single GUI integrating game profiles, LunaHook, Irodori, and advancement."""
from __future__ import annotations
import ctypes
import sys
import threading
from pathlib import Path

from .conversation import Controller, Target, Token
from .hook_host import HookHost
from .hook_protocol import HookEvent, ThreadSource
from .irodori_server import IrodoriServer
from .model import AudioMode, HookSelection, Profile
from .storage import Catalog, load, save
from .voice import VoiceJobs
from .windows_input import WindowsAdvancementInput


def run_gui() -> int:
    from PySide6.QtCore import QObject, Signal, QTimer
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                                   QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
                                   QLabel, QPushButton, QComboBox, QFileDialog,
                                   QSpinBox, QGroupBox)

    class SignalBridge(QObject):
        hook = Signal(object)
        closed = Signal(str)
        voice = Signal(object, bool, str)
        server = Signal(object)

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
            self.jobs = VoiceJobs(lambda token, done, reason: self.bridge.voice.emit(token, done, reason))
            self.controller = Controller(self.send_input, self.preflight)
            self.hook_host: HookHost | None = None
            self.host_generation = ''
            self.connection_generation = 0
            self.sources: dict[str, ThreadSource] = {}
            self.game_id = ''
            self.profile_id = ''
            self.server: IrodoriServer | None = None
            panel = QWidget()
            self.setCentralWidget(panel)
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
            self.source_choice = QComboBox()
            capture.addRow('Observed HOOK sources', self.source_choice)
            row = QHBoxLayout()
            self.text_hook = QPushButton('Use as dialogue HOOK')
            self.speaker_hook = QPushButton('Use as speaker HOOK')
            self.text_hook.clicked.connect(lambda: self.select_hook('text'))
            self.speaker_hook.clicked.connect(lambda: self.select_hook('speaker'))
            row.addWidget(self.text_hook)
            row.addWidget(self.speaker_hook)
            capture.addRow(row)
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
            self.btn_assign = QPushButton('Save speaker routing')
            self.btn_assign.clicked.connect(self.assign_speaker)
            speech.addRow(self.btn_assign)
            self.reading_ms = QSpinBox()
            self.reading_ms.setRange(0, 600000)
            self.reading_ms.setValue(1200)
            speech.addRow('Reading time (ms)', self.reading_ms)
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
            self.selected_hooks.setText('Selected HOOKs: ' + ', '.join(f'{h.role}: {h.code}' for h in profile.hooks))

        def add_game(self):
            try:
                game = self.catalog.create_game(self.new_game.text())
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
                return
            p.default_mode = AudioMode(self.default_mode.currentText())
            p.default_voice = self.default_voice.currentText().strip()
            p.reading_ms = self.reading_ms.value()
            p.repeat_rule = self.repeat.currentText()
            self.persist()
            self.note('Profile saved; restart monitoring to apply new rules')

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
            self.note('Saved speaker: ' + name)

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
            self.source_choice.clear()
            self.hook_host = HookHost(Path(self.catalog.host_dll),
                                      self.bridge.hook.emit, self.bridge.closed.emit)
            try:
                self.hook_host.start()
                self.host_status.setText('LunaHook Host: launched')
            except Exception as exc:
                self.host_status.setText(f'LunaHook Host: {exc}')

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
            if event.kind == 'connected' and event.process_id == self.pid.value():
                if self.connection_generation != event.connection_generation or self.host_generation != event.host_generation:
                    self.stop_session()
                self.host_generation = event.host_generation
                self.connection_generation = event.connection_generation
                self.host_status.setText(f'LunaHook connected: PID {event.process_id}')
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
                result = self.controller.observe(generation=event.host_generation,
                    connection=event.connection_generation, sequence=event.sequence,
                    source=event.source, text=event.text, pid=event.process_id or 0)
                if result and self.controller.utterance:
                    u = self.controller.utterance
                    self.preview.setPlainText(f'Original: {u.raw}\nSpeaker: {u.speaker}\nText: {u.text}\nReading: {u.reading}\nRoute: {u.mode.value}; Voice: {u.voice}')
                    settling = self.controller.profile.settle_ms
                    QTimer.singleShot(settling, lambda t=result: self.present(t) if self.controller.token == t else None)
                    self.refresh_status()
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
            p.hooks = [h for h in p.hooks if h.role != role and h != selection] + [selection]
            self.persist()
            self.selected_hooks.setText('Selected HOOKs: ' + ', '.join(f'{h.role}: {h.code}' for h in p.hooks))

        def begin_monitor(self):
            self.save_profile()
            p = self.current_profile()
            try:
                hwnd = int(self.hwnd.text().strip(), 0)
                if not p:
                    raise ValueError('select a game and profile')
                self.jobs.cancel()
                self.controller.begin(p, Target(self.pid.value(), hwnd),
                                      self.host_generation, self.connection_generation)
                self.note('Monitoring started; auto advance remains unauthorized')
            except Exception as exc:
                self.note(f'Monitor not started: {exc}')
            self.refresh_status()

        def present(self, token: Token):
            u = self.controller.utterance
            if not u:
                return
            self.jobs.cancel()
            if u.mode == AudioMode.READING:
                QTimer.singleShot(self.controller.profile.reading_ms, lambda: self.complete(token, True))
            elif u.mode == AudioMode.TTS:
                if not u.voice:
                    self.complete(token, False, 'TTS requires a Voice')
                else:
                    self.jobs.start(token, self.catalog.irodori_url, u.reading, u.voice)
            else:
                # No guessed duration or fictional ASR match. Keep game input disabled
                # until a process-scoped native presentation adapter is connected.
                self.note(f'{u.mode.value} selected: native audio completion not connected; advance held')
                self.controller.pause('native_audio_unavailable')

        def on_voice(self, token, success, reason):
            self.complete(token, success, reason)

        def complete(self, token, success, reason=''):
            if self.controller.mark_complete(token, success=success):
                self.controller.try_advance()
            elif reason:
                self.note(reason)
            self.refresh_status()

        def preflight(self, target: Target) -> bool:
            if sys.platform != 'win32':
                return False
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            focus = user32.GetForegroundWindow()
            pid = ctypes.c_uint32()
            user32.GetWindowThreadProcessId(ctypes.c_void_p(focus), ctypes.byref(pid))
            return focus == target.hwnd and pid.value == target.pid

        def send_input(self, target: Target, virtual_key: int):
            WindowsAdvancementInput(target.hwnd, virtual_key=virtual_key).advance()

        def permit(self):
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
            self.jobs.cancel()
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
