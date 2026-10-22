"""A single GUI integrating game profiles, LunaHook, Irodori, and advancement."""
from __future__ import annotations
import ctypes
import os
import sys
import subprocess
import threading
import copy
import ntpath
from collections import deque
from pathlib import Path
import re

from .conversation import Controller, Target, Token
from .hook_host import HookHost
from .luna_runtime import runtime_dir, adopted_runtime, RELEASE
from .hook_protocol import HookEvent, ThreadSource
from .irodori_server import IrodoriServer
from .model import AdvanceMode, HookSelection, Profile, StructureRule, ReplacementRule, COMMON_FIELDS, interpret, matching_structure_rule
from .storage import Catalog, load, save
from .voice import VoiceJobs
from .native_audio import NativeAudio
from .manual_input import WindowsManualMonitor
from .ocr import OCRWorker, TesseractOCR
from .screen import WindowsClientCapture
from .import_luna import inspect_luna, add_luna_games
from .import_legacy import inspect_legacy, add_legacy_games
from .game_windows import find_game_windows, find_game_processes, WindowCandidate
from .running_target_dialog import choose_running_target
from .windows_input import WindowsAdvancementInput
from .validation_trace import ValidationTrace, text_digest


def run_gui(session_driver=None) -> int:
    from PySide6.QtCore import QObject, Signal, QTimer, Qt
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                                   QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
                                   QLabel, QPushButton, QComboBox, QFileDialog,
                                   QSpinBox, QGroupBox, QMessageBox, QCheckBox, QScrollArea, QInputDialog,
                                   QTableWidget, QTableWidgetItem, QAbstractItemView, QTabWidget, QHeaderView, QToolButton, QMenu)

    class SignalBridge(QObject):
        hook = Signal(object)
        closed = Signal(str)
        host_error = Signal(str)
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
            self.bridge.host_error.connect(self.on_host_error)
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
            self.attached_pid = 0
            self._resume_monitor_requested = False
            self.sources: dict[str, ThreadSource] = {}
            self.source_samples: dict[str, deque[str]] = {}
            self.ambiguous_sources: set[str] = set()
            self.source_rows: dict[tuple[str, int], int] = {}
            # Advanced search results are candidate HOOK codes, not dialogue
            # samples and never enter the live dialogue candidate list.
            self.search_codes: set[str] = set()
            self.search_insert_requested: set[str] = set()
            self.game_id = ''
            self.profile_id = ''
            self.server: IrodoriServer | None = None
            self.ocr_worker: OCRWorker | None = None
            self.native_audio: NativeAudio | None = None
            self.native_audio_setup_error: str | None = None
            self.manual_monitor: WindowsManualMonitor | None = None
            self.native_token: Token | None = None
            self.native_started = False
            self._known_game_pid = None
            self._synced_sources: dict[tuple[int, int, int, int], ThreadSource] = {}
            self._output_group: list[HookEvent] = []
            self._output_timer = QTimer(self)
            self._output_timer.setSingleShot(True)
            self._output_timer.timeout.connect(self.flush_selected_outputs)
            # The interface follows work: play, acquire dialogue, correct dialogue.
            # A detail expands beside the operation it affects; no miscellaneous tab.
            tabs = QTabWidget()
            root = QWidget()
            root_layout = QVBoxLayout(root)
            self.context_banner = QLabel('作品を選択してください')
            self.context_banner.setStyleSheet('font-size: 15px; font-weight: 600; padding: 7px;')
            root_layout.addWidget(self.context_banner)
            overview = QHBoxLayout()
            self.overview_capture = QLabel('取得: 未接続')
            self.overview_voice = QLabel('音声: 未接続')
            self.overview_advance = QLabel('自動送り: 停止')
            for label in (self.overview_capture, self.overview_voice, self.overview_advance):
                label.setStyleSheet('padding: 6px; border: 1px solid palette(mid); border-radius: 4px;')
                overview.addWidget(label, 1)
            root_layout.addLayout(overview)
            root_layout.addWidget(tabs, 1)
            self.setCentralWidget(root)
            pages = {}
            self.page_scrollers = {}
            for key, name in (('home', 'プレイ'), ('hook', '台詞取得'), ('tune', '台詞・音声')):
                scroller = QScrollArea()
                scroller.setWidgetResizable(True)
                panel = QWidget()
                scroller.setWidget(panel)
                layout = QVBoxLayout(panel)
                layout.setSpacing(14)
                if key == 'tune':
                    self.text_tab_index = tabs.count()
                    tune_page = QWidget()
                    tune_layout = QVBoxLayout(tune_page)
                    text_preview_area = QVBoxLayout()
                    tune_layout.addLayout(text_preview_area)
                    tune_layout.addWidget(scroller, 1)
                    tabs.addTab(tune_page, name)
                else:
                    tabs.addTab(scroller, name)
                pages[key] = layout
                self.page_scrollers[key] = scroller
            pages['speakers'] = pages['tune']
            pages['text'] = pages['tune']
            self.tabs = tabs
            self.ui_sections = {}
            self.section_toggles = {}

            def group(title, *, page='home', section=None, detail=False):
                box = QGroupBox(title)
                form = QFormLayout(box)
                if detail:
                    wrapper = QWidget()
                    outer = QVBoxLayout(wrapper)
                    outer.setContentsMargins(0, 0, 0, 0)
                    toggle = QToolButton()
                    toggle.setText('詳細設定: ' + title)
                    toggle.setCheckable(True)
                    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                    toggle.setArrowType(Qt.ArrowType.RightArrow)
                    toggle.toggled.connect(box.setVisible)
                    if section:
                        self.section_toggles[section] = toggle
                    toggle.toggled.connect(lambda checked, button=toggle:
                                           button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
                    box.setVisible(False)
                    outer.addWidget(toggle)
                    outer.addWidget(box)
                    widget = wrapper
                else:
                    widget = box
                if section == 'live_preview':
                    text_preview_area.addWidget(widget)
                else:
                    pages[page].addWidget(widget)
                if section:
                    self.ui_sections[section] = widget
                return form

            # Shared defaults are edited near the relevant game setting rather
            # than collected in a parallel and unrelated configuration screen.
            environment = group('Irodori接続と共通Voice設定', page='tune', section='irodori', detail=True)
            self.irodori_path = QLineEdit(self.catalog.irodori_root)
            self.luna_runtime_status = QLabel(f'{RELEASE}: {runtime_dir()}')
            self.irodori_url = QLineEdit(self.catalog.irodori_url)
            environment.addRow('Irodoriサーバーフォルダ', self.irodori_path)
            environment.addRow('Irodori接続先URL', self.irodori_url)
            environment.addRow('同梱Luna実行資産の場所', self.luna_runtime_status)
            self.btn_server = QPushButton('Irodoriに接続してVoice一覧を取得')
            self.btn_server.clicked.connect(self.start_server)
            common_advance = group('共通の進行設定', section='common_advance', detail=True)
            common_audio = group('音声判断の共通環境', section='common_audio', detail=True)
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
                destination = (environment if key in ('default_tts', 'default_voice') else
                               common_audio if key in ('native_helper', 'asr_model', 'asr_tokens') else
                               common_advance)
                destination.addRow(common_titles[key], control)
            self.btn_save_common = QPushButton('共通の既定値を保存')
            self.btn_save_common.clicked.connect(self.save_common)
            # All field groups share one catalog.common; each offers the same
            # explicit save, avoiding auto-writing global settings on navigation.
            for destination in (environment, common_advance, common_audio):
                button = self.btn_save_common if destination is common_advance else QPushButton('共通の既定値を保存')
                if button is not self.btn_save_common:
                    button.clicked.connect(self.save_common)
                destination.addRow(button)
            self.server_status = QLabel('Irodori: not connected')
            self.btn_host = QPushButton('LunaHook Hostを単独起動（通常は接続時に自動起動）')
            self.btn_host.clicked.connect(self.start_host)
            self.host_status = QLabel('LunaHook: disconnected')

            games = group('ゲームを選ぶ', section='games')
            game_details = group('作品・プロファイルの管理', section='game_details', detail=True)
            self.game_choice = QComboBox()
            self.game_choice.currentIndexChanged.connect(self.choose_game)
            games.addRow('作品', self.game_choice)
            self.btn_pick_running = QPushButton('実行中のゲーム・プロセスから選択して登録')
            self.btn_pick_running.clicked.connect(self.pick_running_game)
            games.addRow(self.btn_pick_running)
            games.addRow(QLabel('初回は上のボタンから選択。PIDやHOOKコードの入力は不要です。'))
            self.new_game = QLineEdit()
            self.new_game.setPlaceholderText('作品名')
            self.btn_game = QPushButton('作品を登録')
            row = QHBoxLayout()
            row.addWidget(self.new_game)
            row.addWidget(self.btn_game)
            game_details.addRow(row)
            self.btn_game.clicked.connect(self.add_game)
            self.game_exe = QLineEdit()
            self.game_exe.setPlaceholderText('次回以降の識別に使用するEXEのパス')
            game_details.addRow('ゲーム実行ファイル', self.game_exe)
            self.audio_exe = QLineEdit()
            self.audio_exe.setPlaceholderText('Optional: separate audio process executable')
            self.input_exe = QLineEdit()
            self.input_exe.setPlaceholderText('Optional: separate input window executable')
            self.btn_launch = QPushButton('登録済みのゲームを起動')
            self.btn_launch.clicked.connect(self.launch_game)
            games.addRow(self.btn_launch)
            self.btn_import = QPushButton('Luna設定をプレビューして取り込む')
            game_details.addRow(self.btn_import)
            self.btn_import.clicked.connect(self.import_luna)
            self.btn_import_legacy = QPushButton('旧AutoAdv設定をプレビューして取り込む')
            game_details.addRow(self.btn_import_legacy)
            self.btn_import_legacy.clicked.connect(self.import_legacy)
            self.profile_choice = QComboBox()
            self.profile_choice.currentIndexChanged.connect(self.choose_profile)
            games.addRow('作品プロファイル', self.profile_choice)
            self.new_profile = QLineEdit()
            self.new_profile.setPlaceholderText('新しい設定名')
            self.btn_profile = QPushButton('プロファイルを追加')
            row = QHBoxLayout()
            row.addWidget(self.new_profile)
            row.addWidget(self.btn_profile)
            game_details.addRow(row)
            self.btn_profile.clicked.connect(self.add_profile)
            self.btn_dup = QPushButton('プロファイルを複製')
            self.btn_rename = QPushButton('プロファイル名を変更')
            self.btn_delete = QPushButton('プロファイルを削除')
            self.btn_default = QPushButton('この作品の既定プロファイルにする')
            for button, handler in ((self.btn_dup, self.duplicate_profile),
                                    (self.btn_rename, self.rename_profile),
                                    (self.btn_delete, self.delete_profile),
                                    (self.btn_default, self.set_default_profile)):
                game_details.addRow(button)
                button.clicked.connect(handler)

            capture = group('ゲームから取得した台詞を選ぶ', page='hook', section='capture')
            hook_details = group('HOOKの取得条件・手動操作', page='hook', section='hook_details', detail=True)
            hook_details.addRow(self.btn_host)
            target_details = group('別プロセスと入力先の指定', section='target_details', detail=True)
            target_details.addRow('音声出力プロセスのEXE（別プロセス時のみ）', self.audio_exe)
            target_details.addRow('入力対象のEXE（別プロセス時のみ）', self.input_exe)
            ocr_details = group('OCRによる台詞取得の設定', page='hook', section='ocr_details', detail=True)
            self.hook_target = QLabel('まず「ゲーム・自動進行」で実行中のゲームを選択してください')
            capture.addRow(self.hook_target)
            self.pid = QSpinBox()
            self.pid.setRange(0, 2147483647)
            self.hwnd = QLineEdit()
            self.hwnd.setPlaceholderText('HWND e.g. 0x12345')
            hook_details.addRow('HOOK process PID (current run)', self.pid)
            self.audio_pid = QSpinBox()
            self.audio_pid.setRange(0, 2147483647)
            self.audio_pid.setSpecialValueText('Use HOOK PID')
            self.input_pid = QSpinBox()
            self.input_pid.setRange(0, 2147483647)
            self.input_pid.setSpecialValueText('Use HOOK PID')
            target_details.addRow('音声出力プロセスPID（未指定ならHOOK）', self.audio_pid)
            target_details.addRow('入力先プロセスPID（未指定ならHOOK）', self.input_pid)
            target_details.addRow('入力先ウィンドウ識別子（HWND）', self.hwnd)
            self.attach = QPushButton('選択したゲームにLunaHookを接続')
            self.attach.clicked.connect(self.connect_game)
            capture.addRow(self.attach)
            capture.addRow(self.host_status)
            self.btn_discover = QPushButton('Find windows matching registered game executable')
            self.btn_discover.clicked.connect(self.detect_game)
            hook_details.addRow(self.btn_discover)
            self.btn_discover_all = QPushButton('Identify a running game from all registered executables')
            self.btn_discover_all.clicked.connect(self.detect_registered_game)
            hook_details.addRow(self.btn_discover_all)
            self.btn_audio_target = QPushButton('Find audio process by saved executable')
            self.btn_input_target = QPushButton('Find input window by saved executable')
            self.btn_audio_target.clicked.connect(lambda: self.detect_associated_target('audio'))
            self.btn_input_target.clicked.connect(lambda: self.detect_associated_target('input'))
            target_details.addRow(self.btn_audio_target)
            target_details.addRow(self.btn_input_target)
            self.source_mode = QComboBox()
            self.source_mode.addItems(['hook', 'ocr'])
            capture.addRow('取得方法', self.source_mode)
            self.ocr_exe = QLineEdit('tesseract')
            self.ocr_lang = QLineEdit('jpn')
            self.ocr_region = QLineEdit('0,0,640,180')
            ocr_details.addRow('Tesseractの実行ファイル', self.ocr_exe)
            ocr_details.addRow('OCRの言語', self.ocr_lang)
            ocr_details.addRow('OCRの領域 x,y,幅,高さ', self.ocr_region)
            self.ocr_stability = QSpinBox()
            self.ocr_stability.setRange(1, 10)
            ocr_details.addRow('OCRの安定確認回数', self.ocr_stability)
            self.source_mode.currentTextChanged.connect(
                lambda selected: self.reveal_section('ocr_details', 'hook') if selected == 'ocr' else None)
            self.source_table = QTableWidget(0, 4)
            self.source_table.setHorizontalHeaderLabels(['実際に受信した本文', '用途', '取得元', '受信状態'])
            self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.source_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_table.setMinimumHeight(205)
            self.source_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.source_table.setColumnWidth(1, 120)
            self.source_table.setColumnWidth(2, 140)
            self.source_table.itemSelectionChanged.connect(self.show_hook_preview)
            capture.addRow('取得した本文', self.source_table)
            self.hook_preview = QPlainTextEdit()
            self.hook_preview.setReadOnly(True)
            self.hook_preview.setMaximumHeight(130)
            capture.addRow('選択した本文と直近の履歴', self.hook_preview)
            self.btn_test_hook_text = QPushButton('この候補の原文を本文整形で試す')
            self.btn_test_hook_text.clicked.connect(self.use_selected_hook_as_example)
            capture.addRow(self.btn_test_hook_text)
            row = QHBoxLayout()
            self.text_hook = QPushButton('この候補を本文に使用')
            self.speaker_hook = QPushButton('この候補を話者に使用')
            self.text_hook.clicked.connect(lambda: self.select_hook('text'))
            self.speaker_hook.clicked.connect(lambda: self.select_hook('speaker'))
            row.addWidget(self.text_hook)
            row.addWidget(self.speaker_hook)
            capture.addRow(row)
            self.btn_recovery = QPushButton('取得できない・文字化けする場合の調整')
            self.btn_recovery.clicked.connect(lambda: self.reveal_section('hook_details', 'hook'))
            capture.addRow(self.btn_recovery)
            self.btn_find = QPushButton('別のHOOKを探索する')
            self.btn_find.clicked.connect(self.find_hooks)
            hook_details.addRow(self.btn_find)
            self.btn_pc_hooks = QPushButton('Win32汎用HOOKを追加する')
            self.btn_pc_hooks.clicked.connect(self.insert_pc_hooks)
            hook_details.addRow(self.btn_pc_hooks)
            hook_details.addRow(QLabel('高度なHOOK探索の結果は本文候補としては扱いません。実際の出力が確認できた時点で上の一覧に現れます。'))
            self.search_code_choice = QComboBox()
            hook_details.addRow('追加探索で検出したコード（本文ではない）', self.search_code_choice)
            self.btn_insert_search = QPushButton('このコードで本文の取得を試す')
            self.btn_insert_search.clicked.connect(self.insert_selected_search)
            hook_details.addRow(self.btn_insert_search)
            capture.addRow(QLabel('ゲーム内で台詞を表示すると、実際に受信された本文が並びます。'))
            self.insert_pc_hooks_on_attach = QCheckBox('この作品では接続時にWin32汎用HOOKを追加する')
            hook_details.addRow(self.insert_pc_hooks_on_attach)
            self.hook_capture_status = QLabel('HOOK取得: 未接続')
            capture.addRow(self.hook_capture_status)
            self.manual_hook = QLineEdit()
            self.manual_hook.setPlaceholderText('Manual Luna HOOK code')
            hook_details.addRow('手動HOOKコード（通常は不要）', self.manual_hook)
            self.hook_technical_info = QLabel('取得元を選択すると識別情報を表示します')
            self.hook_technical_info.setWordWrap(True)
            hook_details.addRow('現在選択中の取得元', self.hook_technical_info)
            self.btn_insert = QPushButton('詳細操作: HOOKコードを手動追加')
            self.btn_insert.clicked.connect(self.insert_hook)
            hook_details.addRow(self.btn_insert)
            self.btn_unselect_hook = QPushButton('選択済みHOOKの割当を解除')
            self.btn_unselect_hook.clicked.connect(self.unselect_hook)
            hook_details.addRow(self.btn_unselect_hook)
            self.selected_hooks = QLabel('Selected HOOKs: none')
            capture.addRow(self.selected_hooks)
            self.saved_hook_choice = QComboBox()
            hook_details.addRow('保存したHOOKの優先順位', self.saved_hook_choice)
            row = QHBoxLayout()
            self.btn_hook_up = QPushButton('HOOKの優先度を上げる')
            self.btn_hook_down = QPushButton('HOOKの優先度を下げる')
            self.btn_hook_up.clicked.connect(lambda: self.move_saved_hook(-1))
            self.btn_hook_down.clicked.connect(lambda: self.move_saved_hook(1))
            row.addWidget(self.btn_hook_up)
            row.addWidget(self.btn_hook_down)
            hook_details.addRow(row)
            self.hook_reconnect = QLabel('Saved HOOKs: not connected')
            capture.addRow(self.hook_reconnect)
            self.active_hook_status = QLabel('Active dialogue HOOK: not monitoring')
            capture.addRow(self.active_hook_status)
            self.paired_text_hook = QComboBox()
            self.paired_speaker_hook = QComboBox()
            hook_details.addRow('本文HOOK', self.paired_text_hook)
            hook_details.addRow('対応する話者HOOK', self.paired_speaker_hook)
            self.btn_pair = QPushButton('本文HOOKと話者HOOKを対応づける')
            self.btn_pair.clicked.connect(self.pair_hooks)
            hook_details.addRow(self.btn_pair)
            self.btn_monitor = QPushButton('現在の作品設定を保存して監視開始')
            self.btn_monitor.clicked.connect(self.begin_monitor)
            self.btn_to_play = QPushButton('台詞を選択できたらプレイ画面へ')
            self.btn_to_play.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
            capture.addRow(self.btn_to_play)

            # Multiple saved speaker assignments are visible as rows. Selection
            # edits a *single row*, never silently overwrites an unrelated name.
            speech = group('話者別の音声設定', page='tune', section='speakers')
            speech.addRow(self.btn_server)
            speech.addRow(self.server_status)
            speech.addRow(QLabel('この作品の話者ごとにTTS使用有無とVoiceを設定します。'
                                 '一覧にない話者には以下の既定設定が使われます。'))
            self.default_tts = QCheckBox('未登録の話者でもTTSで読み上げる')
            speech.addRow(self.default_tts)
            self.default_voice = QComboBox()
            self.default_voice.setEditable(True)
            speech.addRow('未登録話者のVoice', self.default_voice)
            self.btn_save_speaker_defaults = QPushButton('この作品の既定TTS設定を保存（共通設定より優先）')
            self.btn_save_speaker_defaults.clicked.connect(self.save_speaker_defaults)
            speech.addRow(self.btn_save_speaker_defaults)
            self.speaker_table = QTableWidget(0, 3)
            self.speaker_table.setHorizontalHeaderLabels(['話者名', 'TTS使用', 'Voice ID（未指定は既定）'])
            self.configure_editor_table(self.speaker_table)
            self.speaker_table.itemSelectionChanged.connect(self.show_speaker_override)
            speech.addRow('登録済み話者（選択すると編集）', self.speaker_table)
            self.speaker_name = QLineEdit()
            self.speaker_name.setPlaceholderText('ゲームに表示される話者名を正確に入力')
            speech.addRow('話者名', self.speaker_name)
            self.speaker_tts = QCheckBox('この話者はTTSで読み上げる（OFFは原音／文字判断）')
            speech.addRow(self.speaker_tts)
            self.speaker_voice = QComboBox()
            self.speaker_voice.setEditable(True)
            speech.addRow('この話者のVoice ID', self.speaker_voice)
            row = QHBoxLayout()
            self.btn_assign = QPushButton('話者を追加・変更して保存')
            self.btn_assign.clicked.connect(self.assign_speaker)
            row.addWidget(self.btn_assign)
            self.btn_new_speaker = QPushButton('新しい話者')
            self.btn_new_speaker.clicked.connect(self.clear_speaker_editor)
            row.addWidget(self.btn_new_speaker)
            self.btn_unassign = QPushButton('選択した話者を削除')
            self.btn_unassign.clicked.connect(self.remove_speaker)
            row.addWidget(self.btn_unassign)
            speech.addRow(row)
            speech.addRow(QLabel('音声設定と自動進行方式は独立です。変更は保存後の次の台詞から反映します。'))
            self.speaker_example_raw = QPlainTextEdit()
            self.speaker_example_raw.setMaximumHeight(65)
            self.speaker_example_raw.setPlaceholderText('例: 大和「おはようございます」')
            speech.addRow('話者判定を試すHOOK原文', self.speaker_example_raw)
            self.speaker_preview = QPlainTextEdit()
            self.speaker_preview.setReadOnly(True)
            self.speaker_preview.setMinimumHeight(115)
            speech.addRow('話者設定の適用結果（本文整形タブの例文と連動）', self.speaker_preview)
            audition = group('Voiceの試聴', page='tune', section='audition')
            self.audition_text = QLineEdit('音声の試聴です。')
            audition.addRow('試聴する文章', self.audition_text)
            row = QHBoxLayout()
            self.btn_audition = QPushButton('選択中のVoiceを試聴')
            self.btn_audition.clicked.connect(self.audition_voice)
            row.addWidget(self.btn_audition)
            self.btn_stop_audition = QPushButton('試聴を止める')
            self.btn_stop_audition.clicked.connect(self.preview_jobs.cancel)
            row.addWidget(self.btn_stop_audition)
            audition.addRow(row)

            # The very same product interpreter is used for this test surface.
            preview = group('原文と調整結果（実際の解釈経路を使用）', page='tune', section='live_preview')
            preview.addRow(QLabel('HOOKから受信した原文、または任意の台詞を入力します。'
                                  '規則は上から順に評価され、抽出は最初の一致だけを採用します。'))
            self.example_raw = QPlainTextEdit()
            self.example_raw.setPlaceholderText('例: 大和「おはようございます」')
            self.example_raw.setPlainText('大和「おはようございます」')
            self.example_raw.setMaximumHeight(85)
            self.example_raw.textChanged.connect(
                lambda: self.sync_example(self.example_raw, self.speaker_example_raw))
            self.speaker_example_raw.textChanged.connect(
                lambda: self.sync_example(self.speaker_example_raw, self.example_raw))
            self.speaker_example_raw.setPlainText(self.example_raw.toPlainText())
            preview.addRow('試したいHOOK原文', self.example_raw)
            self.editor_preview = QPlainTextEdit()
            self.editor_preview.setReadOnly(True)
            self.editor_preview.setMinimumHeight(155)
            preview.addRow('適用後（本文・話者・TTS文・ASR照合文・Voice）', self.editor_preview)
            quick = QHBoxLayout()
            self.btn_edit_detected_speaker = QPushButton('この話者の音声を設定')
            self.btn_edit_detected_speaker.clicked.connect(self.edit_detected_speaker)
            self.btn_fix_extraction = QPushButton('話者・本文の切り出しを修正')
            self.btn_fix_extraction.clicked.connect(lambda: self.show_task_section('extraction'))
            self.btn_fix_reading = QPushButton('名前・単語の読みを修正')
            self.btn_fix_reading.clicked.connect(self.edit_preview_reading)
            for button in (self.btn_edit_detected_speaker, self.btn_fix_extraction, self.btn_fix_reading):
                quick.addWidget(button)
            preview.addRow(quick)
            self.preview_state = QLabel('保存済みの作品設定を使用')
            preview.addRow(self.preview_state)

            extraction = group('話者と本文の切り出しを調整', page='tune', section='extraction', detail=True)
            extraction.addRow(QLabel('正規表現では本文に (?P<body>...) 、話者を取る場合は'
                                     ' (?P<speaker>...) を指定します。最初に一致した有効規則だけを使います。'))
            self.structure_table = QTableWidget(0, 4)
            self.structure_table.setHorizontalHeaderLabels(['優先順位', '有効', '適用条件（任意）', '抽出パターン'])
            self.configure_editor_table(self.structure_table)
            self.structure_table.itemSelectionChanged.connect(self.show_structure_rule)
            extraction.addRow('登録済み抽出規則', self.structure_table)
            self.structure_template = QComboBox()
            for label, value in (
                ('よくある形式を選ぶ（まだ適用しない）', ''),
                ('話者「本文」', r'^\s*(?P<speaker>[^「」]+)「(?P<body>.*?)」\s*$'),
                ('話者：本文', r'^\s*(?P<speaker>[^：:\n]+)[：:]\s*(?P<body>.+)$'),
                ('話者の次行に本文', r'^\s*(?P<speaker>[^\n]+)\n(?P<body>.+)$'),
                ('「本文」のみ', r'^\s*「(?P<body>.*?)」\s*$'),
            ):
                self.structure_template.addItem(label, value)
            self.btn_apply_template = QPushButton('選択した形式を編集欄へ')
            self.btn_apply_template.clicked.connect(self.use_structure_template)
            extraction.addRow('抽出パターンの例', self.structure_template)
            extraction.addRow(self.btn_apply_template)
            self.structure_pattern = QLineEdit()
            self.structure_pattern.setPlaceholderText(r'^(?P<speaker>.+?)「(?P<body>.+)」$')
            extraction.addRow('抽出する正規表現', self.structure_pattern)
            self.structure_condition = QLineEdit()
            self.structure_condition.setPlaceholderText('空欄なら全ての原文を対象')
            extraction.addRow('適用条件（正規表現・任意）', self.structure_condition)
            self.structure_enabled = QCheckBox('有効')
            self.structure_enabled.setChecked(True)
            extraction.addRow(self.structure_enabled)
            row = QHBoxLayout()
            self.btn_structure = QPushButton('規則を追加・変更')
            self.btn_structure.clicked.connect(self.add_structure_rule)
            row.addWidget(self.btn_structure)
            self.btn_structure_new = QPushButton('新規作成')
            self.btn_structure_new.clicked.connect(self.clear_structure_editor)
            row.addWidget(self.btn_structure_new)
            extraction.addRow(row)
            row = QHBoxLayout()
            self.btn_structure_up = QPushButton('上へ（優先度↑）')
            self.btn_structure_up.clicked.connect(self.raise_structure_rule)
            self.btn_structure_down = QPushButton('下へ（優先度↓）')
            self.btn_structure_down.clicked.connect(self.lower_structure_rule)
            self.btn_structure_remove = QPushButton('選択規則を削除')
            self.btn_structure_remove.clicked.connect(self.remove_structure_rule)
            for btn in (self.btn_structure_up, self.btn_structure_down, self.btn_structure_remove):
                row.addWidget(btn)
            extraction.addRow(row)

            replacement = group('読み上げ・音声照合の置換規則', page='tune', section='replacement', detail=True)
            replacement.addRow(QLabel('表示本文は変更しません。TTSは読み上げ文、ASRは原音照合文のみを変更します。'))
            self.replacement_table = QTableWidget(0, 5)
            self.replacement_table.setHorizontalHeaderLabels(['有効', '一致対象', '置換後', '適用先', '正規表現'])
            self.configure_editor_table(self.replacement_table)
            self.replacement_table.itemSelectionChanged.connect(self.show_replacement_rule)
            replacement.addRow('登録済み置換規則（適用順）', self.replacement_table)
            self.replace_pattern = QLineEdit()
            self.replace_value = QLineEdit()
            self.replace_target = QComboBox()
            self.replace_target.addItem('読み上げ（TTS）のみ', 'tts')
            self.replace_target.addItem('原音照合（ASR）のみ', 'asr')
            self.replace_target.addItem('両方', 'both')
            self.replace_regex = QCheckBox('正規表現を使用')
            self.replace_enabled = QCheckBox('有効')
            self.replace_enabled.setChecked(True)
            replacement.addRow('置換する文字列', self.replace_pattern)
            replacement.addRow('置換後の文字列', self.replace_value)
            replacement.addRow('置換の適用先', self.replace_target)
            replacement.addRow(self.replace_regex)
            replacement.addRow(self.replace_enabled)
            row = QHBoxLayout()
            self.btn_replace = QPushButton('置換規則を追加・変更')
            self.btn_replace.clicked.connect(self.add_replacement)
            row.addWidget(self.btn_replace)
            self.btn_replace_new = QPushButton('新規作成')
            self.btn_replace_new.clicked.connect(self.clear_replacement_editor)
            row.addWidget(self.btn_replace_new)
            self.btn_replacement_remove = QPushButton('選択規則を削除')
            self.btn_replacement_remove.clicked.connect(self.remove_replacement)
            row.addWidget(self.btn_replacement_remove)
            replacement.addRow(row)
            row = QHBoxLayout()
            self.btn_replace_up = QPushButton('上へ（先に適用）')
            self.btn_replace_up.clicked.connect(lambda: self.move_replacement_rule(-1))
            self.btn_replace_down = QPushButton('下へ（後に適用）')
            self.btn_replace_down.clicked.connect(lambda: self.move_replacement_rule(1))
            row.addWidget(self.btn_replace_up)
            row.addWidget(self.btn_replace_down)
            replacement.addRow(row)

            reading = group('固有名詞の読みを直す', page='tune', section='reading')
            reading.addRow(QLabel('漢字の単語境界で読み替えます。長い単語を先に処理します。'
                                  '表示本文やASR照合文は変更しません。'))
            self.reading_table = QTableWidget(0, 2)
            self.reading_table.setHorizontalHeaderLabels(['表記', '読み'])
            self.configure_editor_table(self.reading_table)
            self.reading_table.itemSelectionChanged.connect(self.show_reading_rule)
            reading.addRow('登録済みの読み', self.reading_table)
            self.reading_word = QLineEdit()
            self.reading_value = QLineEdit()
            reading.addRow('表記', self.reading_word)
            reading.addRow('読み仮名', self.reading_value)
            row = QHBoxLayout()
            self.btn_reading = QPushButton('読みを追加・変更')
            self.btn_reading.clicked.connect(self.add_reading)
            row.addWidget(self.btn_reading)
            self.btn_reading_new = QPushButton('新規作成')
            self.btn_reading_new.clicked.connect(self.clear_reading_editor)
            row.addWidget(self.btn_reading_new)
            self.btn_reading_remove = QPushButton('選択した読みを削除')
            self.btn_reading_remove.clicked.connect(self.remove_reading)
            row.addWidget(self.btn_reading_remove)
            reading.addRow(row)

            advanced = hook_details
            audio_details = group('音声判断のモデルと捕捉環境', section='audio_details', detail=True)
            advance_details = group('送り操作と待機条件', section='advance_details', detail=True)
            self.hook_flush = QSpinBox()
            self.hook_flush.setRange(0, 10000)
            advanced.addRow('Luna HOOK出力までの待機（ms）', self.hook_flush)
            self.hook_codepage = QSpinBox()
            self.hook_codepage.setRange(0, 65535)
            self.hook_codepage.setSpecialValueText('自動 (0)')
            advanced.addRow('Luna文字コードページ', self.hook_codepage)
            self.hook_history = QSpinBox()
            self.hook_history.setRange(1, 10000000)
            advanced.addRow('Luna HOOK履歴の上限', self.hook_history)
            self.hook_buffer = QSpinBox()
            self.hook_buffer.setRange(1, 1000000)
            advanced.addRow('Luna文字バッファの上限', self.hook_buffer)
            self.input_key = QSpinBox()
            self.input_key.setRange(1, 254)
            advance_details.addRow('送信する仮想キーコード', self.input_key)
            self.native_helper = QLineEdit()
            self.asr_model = QLineEdit()
            self.asr_tokens = QLineEdit()
            audio_details.addRow('音声捕捉ヘルパーDLL（音声判断のみ）', self.native_helper)
            audio_details.addRow('日本語ASRモデル（音声判断のみ）', self.asr_model)
            audio_details.addRow('ASR語彙ファイル', self.asr_tokens)
            self.next_line_timeout = QSpinBox()
            self.next_line_timeout.setRange(1000, 120000)
            advance_details.addRow('次の台詞を待つ時間（ms）', self.next_line_timeout)
            self.repeat = QComboBox()
            self.repeat.addItem('連続した同文を重複として扱う', 'distinct_only')
            self.repeat.addItem('送り後の同文は別台詞として扱う', 'each_after_input')
            advance_details.addRow('同一本文の連続取得', self.repeat)
            self.inherit_controls = {}
            self.btn_save = QPushButton('作品の設定を保存')
            self.btn_save.clicked.connect(self.save_profile)
            live_dialogue = group('現在の台詞と適用結果', section='current_dialogue')
            self.preview = QPlainTextEdit()
            self.preview.setReadOnly(True)
            self.preview.setMinimumHeight(95)
            live_dialogue.addRow(self.preview)
            self.btn_edit_current = QPushButton('現在の台詞を使って本文・話者を調整')
            self.btn_edit_current.clicked.connect(self.edit_current_dialogue)
            live_dialogue.addRow(self.btn_edit_current)

            for w in (self.structure_pattern, self.structure_condition, self.replace_pattern,
                      self.replace_value, self.reading_word, self.reading_value, self.speaker_name):
                w.textChanged.connect(lambda _=None, control=w: self.preview_from_control(control))
            for w in (self.structure_enabled, self.replace_regex, self.replace_enabled,
                      self.speaker_tts, self.default_tts):
                w.toggled.connect(lambda _=None, control=w: self.preview_from_control(control))
            for w in (self.replace_target, self.default_voice, self.speaker_voice):
                w.currentTextChanged.connect(lambda _=None, control=w: self.preview_from_control(control))
            advance = group('進行方式と自動送り', section='advance')
            advance.addRow(self.btn_monitor)
            self.btn_resume = QPushButton('保存した設定で接続・監視（自動送りは停止のまま）')
            self.btn_resume.clicked.connect(self.resume_saved_game)
            games.addRow(self.btn_resume)
            self.advance_mode = QComboBox()
            self.advance_mode.addItem('文字のみ判断', AdvanceMode.TEXT.value)
            self.advance_mode.addItem('音声判断', AdvanceMode.AUDIO.value)
            advance.addRow('進行方式', self.advance_mode)
            self.uncertain_text_fallback = QCheckBox('音声判定が不確かなとき、文字数待機時間で進む（早送りの可能性あり）')
            advance.addRow(self.uncertain_text_fallback)
            self.per_character_wait_ms = QSpinBox()
            self.per_character_wait_ms.setRange(1, 600000)
            advance.addRow('記号除外の1文字あたり待機時間（ms）', self.per_character_wait_ms)
            self.reading_ms = QSpinBox()
            self.reading_ms.setRange(1, 600000)
            advance.addRow('最低表示時間（ms）', self.reading_ms)
            self.wait_preview = QLabel('計数: - / 待機時間: -')
            advance.addRow('現在台詞の計数と確定時間', self.wait_preview)
            advance.addRow(QLabel('文字のみ判断は原音を途中で送る場合があります。音声判断で確証がなければ停止します。'))
            row = QHBoxLayout()
            self.btn_permit = QPushButton('自動送りを許可')
            self.btn_pause = QPushButton('自動送りを停止')
            row.addWidget(self.btn_permit)
            row.addWidget(self.btn_pause)
            self.btn_permit.clicked.connect(self.permit)
            self.btn_pause.clicked.connect(self.pause)
            advance.addRow(row)
            self.status = QLabel('Status: not monitoring')
            advance.addRow(self.status)
            advance.addRow(self.btn_save)
            inherit_targets = {
                'default_tts': speech, 'default_voice': speech,
                'native_helper': audio_details, 'asr_model': audio_details, 'asr_tokens': audio_details,
                'input_key': advance_details, 'next_line_timeout_ms': advance_details,
                'advance_mode': advance, 'uncertain_text_fallback': advance,
                'reading_ms': advance, 'per_character_wait_ms': advance,
            }
            self.profile_field_controls = {
                'advance_mode': self.advance_mode,
                'uncertain_text_fallback': self.uncertain_text_fallback,
                'default_tts': self.default_tts, 'default_voice': self.default_voice,
                'reading_ms': self.reading_ms,
                'per_character_wait_ms': self.per_character_wait_ms,
                'native_helper': self.native_helper, 'asr_model': self.asr_model,
                'asr_tokens': self.asr_tokens, 'input_key': self.input_key,
                'next_line_timeout_ms': self.next_line_timeout,
            }
            for key in COMMON_FIELDS:
                control = QCheckBox('共通設定を継承: ' + common_titles[key])
                self.inherit_controls[key] = control
                control.toggled.connect(lambda _checked, name=key: self.refresh_inherited_control(name))
                inherit_targets[key].addRow(control)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumHeight(120)
            pages['home'].addWidget(self.log)
            section_order = {
                'home': ('games', 'game_details', 'current_dialogue', 'advance', 'advance_details',
                         'target_details', 'audio_details', 'common_advance', 'common_audio'),
                'hook': ('capture', 'hook_details', 'ocr_details'),
                'tune': ('speakers', 'audition', 'irodori', 'reading', 'extraction', 'replacement'),
            }
            for page, order in section_order.items():
                layout = pages[page]
                for position, section in enumerate(order):
                    widget = self.ui_sections[section]
                    layout.removeWidget(widget)
                    layout.insertWidget(position, widget)
                layout.addStretch(1)
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
            self.statusBar().showMessage(message, 12000)

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
            self._resume_monitor_requested = False
            self.stop_session()
            if self.hook_host:
                self.hook_host.close()
                self.hook_host = None
            self.host_generation = ''
            self.connection_generation = 0
            self.attached_pid = 0
            self.clear_hook_observations()
            self.host_status.setText('LunaHook: 未接続')
            self.pid.setValue(0)
            self.audio_pid.setValue(0)
            self.input_pid.setValue(0)
            self.hwnd.clear()
            self.hook_target.setText('まず実行中のゲーム・プロセスを選択してください')
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

        def refresh_inherited_control(self, key: str):
            """The displayed value and editability agree with the saved source."""
            checkbox = self.inherit_controls[key]
            control = self.profile_field_controls[key]
            inherited = checkbox.isChecked()
            control.setEnabled(not inherited)
            if not inherited:
                return
            value = getattr(self.catalog.common, key)
            was_blocked = control.blockSignals(True)
            try:
                if isinstance(control, QCheckBox):
                    control.setChecked(value)
                elif isinstance(control, QSpinBox):
                    control.setValue(value)
                elif isinstance(control, QComboBox):
                    if key == 'advance_mode':
                        control.setCurrentIndex(control.findData(value.value))
                    else:
                        control.setEditText(value)
                else:
                    control.setText(value)
            finally:
                control.blockSignals(was_blocked)
            self.refresh_rule_preview()

        def choose_profile(self, *_):
            self._resume_monitor_requested = False
            self.stop_session()
            self.profile_id = self.profile_choice.currentData() or ''
            profile = self.current_profile()
            if not profile:
                self.context_banner.setText('編集対象: 作品とプロファイルを選択してください')
                for table in (self.speaker_table, self.structure_table,
                              self.replacement_table, self.reading_table):
                    table.setRowCount(0)
                self.refresh_rule_preview()
                return
            game = self.catalog.games[self.game_id]
            self.context_banner.setText(f'編集対象: {game.name} / {profile.name}  ―  この作品の設定を編集中')
            effective = self.catalog.effective_profile(profile)
            for key, widget in self.inherit_controls.items():
                widget.setChecked(key in profile.inherit_common)
            self.default_tts.setChecked(effective.default_tts)
            self.advance_mode.setCurrentIndex(self.advance_mode.findData(effective.advance_mode.value))
            self.uncertain_text_fallback.setChecked(effective.uncertain_text_fallback)
            self.per_character_wait_ms.setValue(effective.per_character_wait_ms)
            self.default_voice.setEditText(effective.default_voice)
            self.reading_ms.setValue(effective.reading_ms)
            self.insert_pc_hooks_on_attach.setChecked(profile.insert_pc_hooks)
            self.hook_flush.setValue(profile.hook_flush_ms)
            self.hook_codepage.setValue(profile.hook_codepage)
            self.hook_buffer.setValue(profile.hook_max_buffer)
            self.hook_history.setValue(profile.hook_max_history)
            self.input_key.setValue(effective.input_key)
            self.repeat.setCurrentIndex(self.repeat.findData(profile.repeat_rule))
            self.source_mode.setCurrentText(profile.source)
            self.ocr_exe.setText(profile.ocr.executable)
            self.ocr_lang.setText(profile.ocr.language)
            self.ocr_region.setText(','.join(map(str, profile.ocr.region)))
            self.ocr_stability.setValue(profile.ocr.stable_samples)
            self.native_helper.setText(effective.native_helper)
            self.asr_model.setText(effective.asr_model)
            self.asr_tokens.setText(effective.asr_tokens)
            self.next_line_timeout.setValue(effective.next_line_timeout_ms)
            for field in COMMON_FIELDS:
                self.refresh_inherited_control(field)
            self.refresh_rules()
            self.refresh_selected_hooks()
            self.refresh_speaker_choices()
            self.refresh_hook_pairs()
            # Editors must never carry the previous game's unsaved draft.
            self.clear_structure_editor()
            self.clear_replacement_editor()
            self.clear_reading_editor()
            self.clear_speaker_editor()
            self._preview_edit_kind = None
            self.refresh_rule_preview()
            self.refresh_rule_preview()

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
                updated.insert_pc_hooks = self.insert_pc_hooks_on_attach.isChecked()
                updated.hook_flush_ms = self.hook_flush.value()
                updated.hook_codepage = self.hook_codepage.value()
                updated.hook_max_buffer = self.hook_buffer.value()
                updated.hook_max_history = self.hook_history.value()
                updated.input_key = self.input_key.value()
                updated.repeat_rule = self.repeat.currentData()
                updated.inherit_common = [key for key, widget in self.inherit_controls.items() if widget.isChecked()]
                # Inherited values are not custom overrides. Retain dormant
                # per-game values so switching inheritance off restores them.
                for field in updated.inherit_common:
                    setattr(updated, field, getattr(p, field))
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

        @staticmethod
        def configure_editor_table(table):
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            table.setMinimumHeight(155)
            table.horizontalHeader().setStretchLastSection(True)
            table.verticalHeader().setVisible(False)

        @staticmethod
        def selected_editor_row(table):
            selection = table.selectionModel()
            rows = selection.selectedRows() if selection else []
            return rows[0].row() if rows else -1

        @staticmethod
        def fill_editor_table(table, records, selected=-1):
            table.blockSignals(True)
            table.setRowCount(len(records))
            for row, fields in enumerate(records):
                for column, value in enumerate(fields):
                    table.setItem(row, column, QTableWidgetItem(str(value)))
            table.clearSelection()
            table.blockSignals(False)
            if 0 <= selected < len(records):
                table.selectRow(selected)

        def commit_rule_edit(self, change, message):
            original = self.current_profile()
            if original is None:
                self.note('作品を選択してください')
                return False
            try:
                updated = copy.deepcopy(original)
                change(updated)
                updated.validate()
                self.catalog.games[self.game_id].profiles[updated.id] = updated
                try:
                    self.persist()
                except Exception:
                    self.catalog.games[self.game_id].profiles[original.id] = original
                    raise
                self.note(message + '。監視中の台詞は変更しません。再監視時に新しい設定を適用します。')
                return True
            except Exception as exc:
                self.note('設定を保存できません: ' + str(exc))
                return False

        def save_speaker_defaults(self):
            def change(p):
                p.default_tts = self.default_tts.isChecked()
                p.default_voice = self.default_voice.currentText().strip()
                p.inherit_common = [key for key in p.inherit_common
                                    if key not in ('default_tts', 'default_voice')]
            if self.commit_rule_edit(change, '作品の既定TTS設定を保存しました'):
                self.inherit_controls['default_tts'].setChecked(False)
                self.inherit_controls['default_voice'].setChecked(False)
                self.refresh_speaker_choices()
                self.refresh_rule_preview()

        def assign_speaker(self):
            name = self.speaker_name.text().strip()
            if not name:
                self.note('話者名を入力してください')
                return
            row = self.selected_editor_row(self.speaker_table)
            old = self.speaker_table.item(row, 0).text() if row >= 0 else None
            exists = {self.speaker_table.item(i, 0).text() for i in range(self.speaker_table.rowCount())}
            if name in exists and name != old:
                self.note('同名の話者が存在します。一覧から選択して変更してください')
                return
            def change(p):
                if old and old != name:
                    p.speaker_tts.pop(old, None)
                    p.speaker_voices.pop(old, None)
                p.speaker_tts[name] = self.speaker_tts.isChecked()
                p.speaker_voices[name] = self.speaker_voice.currentText().strip()
            if self.commit_rule_edit(change, '話者設定を保存しました'):
                self.refresh_speaker_choices(name)

        def clear_speaker_editor(self):
            self.speaker_table.clearSelection()
            self.speaker_name.clear()
            self.speaker_tts.setChecked(self.default_tts.isChecked())
            self.speaker_voice.setEditText('')
            self._preview_edit_kind = 'speaker'
            self.refresh_rule_preview()

        def refresh_speaker_choices(self, selected=None):
            p = self.current_profile()
            names = sorted(set(p.speaker_tts) | set(p.speaker_voices)) if p else []
            effective = self.catalog.effective_profile(p) if p else None
            rows = [(name, '使う' if p.speaker_tts.get(name, effective.default_tts) else '使わない',
                     p.speaker_voices.get(name, '') or '（作品既定）') for name in names]
            self.fill_editor_table(self.speaker_table, rows,
                                   names.index(selected) if selected in names else -1)
            self.refresh_rule_preview()

        def show_speaker_override(self):
            row = self.selected_editor_row(self.speaker_table)
            p = self.current_profile()
            if row >= 0 and p:
                name = self.speaker_table.item(row, 0).text()
                self.speaker_name.setText(name)
                self.speaker_tts.setChecked(p.speaker_tts.get(name, p.default_tts))
                self.speaker_voice.setEditText(p.speaker_voices.get(name, ''))
                self._preview_edit_kind = 'speaker'
                self.refresh_rule_preview()

        def remove_speaker(self):
            row = self.selected_editor_row(self.speaker_table)
            if row < 0:
                self.note('削除する話者を一覧から選んでください')
                return
            name = self.speaker_table.item(row, 0).text()
            def change(p):
                p.speaker_tts.pop(name, None)
                p.speaker_voices.pop(name, None)
            if self.commit_rule_edit(change, '話者を削除しました'):
                self.refresh_speaker_choices()
                self.clear_speaker_editor()

        def refresh_rules(self, focus=None, row=-1):
            p = self.current_profile()
            if not p:
                return
            self.fill_editor_table(self.structure_table, [
                (str(i + 1), '有効' if rule.enabled else '無効', rule.condition or '（常時）', rule.pattern)
                for i, rule in enumerate(p.structure_rules)], row if focus == 'structure' else -1)
            self.fill_editor_table(self.replacement_table, [
                ('有効' if rule.enabled else '無効', rule.pattern, rule.replacement,
                 {'tts': 'TTS', 'asr': 'ASR', 'both': '両方'}[rule.target],
                 '正規表現' if rule.regex else '通常文字列')
                for rule in p.replacements], row if focus == 'replacement' else -1)
            words = list(p.name_readings)
            self.fill_editor_table(self.reading_table, [(word, p.name_readings[word]) for word in words],
                                   row if focus == 'reading' else -1)
            self.refresh_rule_preview()

        def show_structure_rule(self):
            row = self.selected_editor_row(self.structure_table)
            p = self.current_profile()
            if row >= 0 and p and row < len(p.structure_rules):
                rule = p.structure_rules[row]
                self.structure_pattern.setText(rule.pattern)
                self.structure_condition.setText(rule.condition)
                self.structure_enabled.setChecked(rule.enabled)
                self._preview_edit_kind = 'structure'
                self.refresh_rule_preview()

        def use_structure_template(self):
            template = self.structure_template.currentData()
            if template:
                self.structure_pattern.setText(template)

        def clear_structure_editor(self):
            self.structure_table.clearSelection()
            self.structure_pattern.clear()
            self.structure_condition.clear()
            self.structure_enabled.setChecked(True)
            self._preview_edit_kind = 'structure'
            self.refresh_rule_preview()

        def move_structure_rule(self, movement):
            row = self.selected_editor_row(self.structure_table)
            p = self.current_profile()
            if p is None or row < 0 or not 0 <= row + movement < len(p.structure_rules):
                return
            def change(profile):
                profile.structure_rules[row], profile.structure_rules[row + movement] = (
                    profile.structure_rules[row + movement], profile.structure_rules[row])
            if self.commit_rule_edit(change, '抽出規則の優先順位を変更しました'):
                self.refresh_rules('structure', row + movement)

        def raise_structure_rule(self):
            self.move_structure_rule(-1)

        def lower_structure_rule(self):
            self.move_structure_rule(1)

        def add_structure_rule(self):
            row = self.selected_editor_row(self.structure_table)
            try:
                rule = StructureRule(self.structure_pattern.text().strip(),
                                     self.structure_condition.text().strip(),
                                     self.structure_enabled.isChecked())
            except Exception as exc:
                self.note('抽出規則を確認してください: ' + str(exc))
                self.refresh_rule_preview()
                return
            def change(p):
                if row >= 0:
                    p.structure_rules[row] = rule
                else:
                    p.structure_rules.insert(0, rule)
            if self.commit_rule_edit(change, '抽出規則を保存しました'):
                self.refresh_rules('structure', row if row >= 0 else 0)

        def remove_structure_rule(self):
            row = self.selected_editor_row(self.structure_table)
            if row < 0:
                self.note('削除する抽出規則を一覧から選んでください')
                return
            if self.commit_rule_edit(lambda p: p.structure_rules.pop(row), '抽出規則を削除しました'):
                self.refresh_rules()
                self.clear_structure_editor()

        def show_replacement_rule(self):
            row = self.selected_editor_row(self.replacement_table)
            p = self.current_profile()
            if row >= 0 and p and row < len(p.replacements):
                rule = p.replacements[row]
                self.replace_pattern.setText(rule.pattern)
                self.replace_value.setText(rule.replacement)
                self.replace_target.setCurrentIndex(self.replace_target.findData(rule.target))
                self.replace_regex.setChecked(rule.regex)
                self.replace_enabled.setChecked(rule.enabled)
                self._preview_edit_kind = 'replacement'
                self.refresh_rule_preview()

        def clear_replacement_editor(self):
            self.replacement_table.clearSelection()
            self.replace_pattern.clear()
            self.replace_value.clear()
            self.replace_regex.setChecked(False)
            self.replace_target.setCurrentIndex(0)
            self.replace_enabled.setChecked(True)
            self._preview_edit_kind = 'replacement'
            self.refresh_rule_preview()

        def add_replacement(self):
            row = self.selected_editor_row(self.replacement_table)
            try:
                rule = ReplacementRule(self.replace_pattern.text(), self.replace_value.text(),
                    self.replace_regex.isChecked(), self.replace_target.currentData(),
                    self.replace_enabled.isChecked())
            except Exception as exc:
                self.note('置換規則を確認してください: ' + str(exc))
                self.refresh_rule_preview()
                return
            def change(p):
                if row >= 0:
                    p.replacements[row] = rule
                else:
                    p.replacements.append(rule)
            if self.commit_rule_edit(change, '置換規則を保存しました'):
                self.refresh_rules('replacement', row if row >= 0 else self.replacement_table.rowCount())

        def move_replacement_rule(self, movement):
            row = self.selected_editor_row(self.replacement_table)
            p = self.current_profile()
            if p is None or row < 0 or not 0 <= row + movement < len(p.replacements):
                return
            def change(profile):
                profile.replacements[row], profile.replacements[row + movement] = (
                    profile.replacements[row + movement], profile.replacements[row])
            if self.commit_rule_edit(change, '置換の適用順を変更しました'):
                self.refresh_rules('replacement', row + movement)

        def remove_replacement(self):
            row = self.selected_editor_row(self.replacement_table)
            if row < 0:
                self.note('削除する置換規則を一覧から選んでください')
                return
            if self.commit_rule_edit(lambda p: p.replacements.pop(row), '置換規則を削除しました'):
                self.refresh_rules()
                self.clear_replacement_editor()

        def show_reading_rule(self):
            row = self.selected_editor_row(self.reading_table)
            p = self.current_profile()
            if row >= 0 and p:
                name = self.reading_table.item(row, 0).text()
                self.reading_word.setText(name)
                self.reading_value.setText(p.name_readings[name])
                self._preview_edit_kind = 'reading'
                self.refresh_rule_preview()

        def clear_reading_editor(self):
            self.reading_table.clearSelection()
            self.reading_word.clear()
            self.reading_value.clear()
            self._preview_edit_kind = 'reading'
            self.refresh_rule_preview()

        def add_reading(self):
            word = self.reading_word.text().strip()
            reading = self.reading_value.text().strip()
            if not word or not reading:
                self.note('表記と読みの両方を入力してください')
                return
            row = self.selected_editor_row(self.reading_table)
            old = self.reading_table.item(row, 0).text() if row >= 0 else None
            p = self.current_profile()
            if p and word in p.name_readings and old != word:
                self.note('同じ表記が既にあります。一覧で選択して変更してください')
                return
            def change(profile):
                if old and old != word:
                    profile.name_readings.pop(old, None)
                profile.name_readings[word] = reading
            if self.commit_rule_edit(change, '読みを保存しました'):
                self.refresh_rules('reading', list(self.current_profile().name_readings).index(word))

        def remove_reading(self):
            row = self.selected_editor_row(self.reading_table)
            if row < 0:
                self.note('削除する読みを一覧から選んでください')
                return
            word = self.reading_table.item(row, 0).text()
            if self.commit_rule_edit(lambda p: p.name_readings.pop(word), '読みを削除しました'):
                self.refresh_rules()
                self.clear_reading_editor()

        def audition_voice(self):
            voice = self.speaker_voice.currentText().strip() or self.default_voice.currentText().strip()
            try:
                if not voice:
                    raise ValueError('Voice IDを選択してください')
                self.preview_jobs.start(Token(-1, -1), self.catalog.irodori_url,
                                        self.audition_text.text(), voice)
            except Exception as exc:
                self.note('試聴できません: ' + str(exc))

        def on_preview(self, success, reason):
            self.note('試聴が終了しました' if success else '試聴: ' + reason)

        def sync_example(self, source, destination):
            if destination.toPlainText() != source.toPlainText():
                destination.blockSignals(True)
                destination.setPlainText(source.toPlainText())
                destination.blockSignals(False)
            self.refresh_rule_preview()

        def preview_from_control(self, control):
            if control in (self.structure_pattern, self.structure_condition, self.structure_enabled):
                self._preview_edit_kind = 'structure'
            elif control in (self.replace_pattern, self.replace_value, self.replace_target,
                             self.replace_regex, self.replace_enabled):
                self._preview_edit_kind = 'replacement'
            elif control in (self.reading_word, self.reading_value):
                self._preview_edit_kind = 'reading'
            elif control in (self.speaker_name, self.speaker_voice, self.speaker_tts):
                self._preview_edit_kind = 'speaker'
            self.refresh_rule_preview()

        def refresh_rule_preview(self, *_):
            if not hasattr(self, 'example_raw') or not hasattr(self, 'editor_preview'):
                return
            p = self.current_profile()
            if p is None:
                self.editor_preview.setPlainText('作品を選択してください。')
                self.speaker_preview.setPlainText('作品を選択してください。')
                return
            draft = copy.deepcopy(self.catalog.effective_profile(p))
            draft.default_tts = self.default_tts.isChecked()
            draft.default_voice = self.default_voice.currentText().strip()
            kind = getattr(self, '_preview_edit_kind', None)
            row = -1
            try:
                if kind == 'structure' and self.structure_pattern.text().strip():
                    rule = StructureRule(self.structure_pattern.text().strip(),
                        self.structure_condition.text().strip(), self.structure_enabled.isChecked())
                    row = self.selected_editor_row(self.structure_table)
                    if row >= 0:
                        draft.structure_rules[row] = rule
                    else:
                        draft.structure_rules.insert(0, rule)
                elif kind == 'replacement' and self.replace_pattern.text():
                    rule = ReplacementRule(self.replace_pattern.text(), self.replace_value.text(),
                        self.replace_regex.isChecked(), self.replace_target.currentData(),
                        self.replace_enabled.isChecked())
                    row = self.selected_editor_row(self.replacement_table)
                    if row >= 0:
                        draft.replacements[row] = rule
                    else:
                        draft.replacements.append(rule)
                elif kind == 'reading' and self.reading_word.text().strip() and self.reading_value.text().strip():
                    row = self.selected_editor_row(self.reading_table)
                    if row >= 0:
                        draft.name_readings.pop(self.reading_table.item(row, 0).text(), None)
                    draft.name_readings[self.reading_word.text().strip()] = self.reading_value.text().strip()
                elif kind == 'speaker' and self.speaker_name.text().strip():
                    row = self.selected_editor_row(self.speaker_table)
                    if row >= 0:
                        old = self.speaker_table.item(row, 0).text()
                        draft.speaker_tts.pop(old, None)
                        draft.speaker_voices.pop(old, None)
                    draft.speaker_tts[self.speaker_name.text().strip()] = self.speaker_tts.isChecked()
                    draft.speaker_voices[self.speaker_name.text().strip()] = self.speaker_voice.currentText().strip()
                raw = self.example_raw.toPlainText()
                trace = []
                u = interpret(raw, draft, session=0, line=0, source='preview', sequence=0, trace=trace)
                _, matched = matching_structure_rule(raw, draft)
                rule_name = f'優先順位 {matched + 1} の抽出規則が一致' if matched is not None else '抽出規則に一致せず：原文を本文として使用'
                self.editor_preview.setPlainText(
                    f'話者: {u.speaker or "（未判別）"}\n'
                    f'表示本文: {u.text}\n'
                    f'TTS読み上げ文: {u.reading}\n'
                    f'ASR照合文: {u.match_text}\n'
                    f'適用した抽出規則: {rule_name}\n'
                    f'設定: TTS {"使用" if u.use_tts else "不使用"} / Voice {u.voice or "（未指定）"}\n'
                    f'計数: {u.character_count}文字 / 文字待機: {u.text_wait_ms}ms\n'
                    f'適用履歴: {" / ".join(trace)}')
                editing_name = self.speaker_name.text().strip()
                editing_line = (
                    f'編集中の話者「{editing_name}」: '
                    f'TTS {"使用" if draft.speaker_tts.get(editing_name, draft.default_tts) else "不使用"} / '
                    f'Voice {draft.speaker_voices.get(editing_name, draft.default_voice) or "（未指定）"}\n'
                    if editing_name else '編集中の話者: 未選択\n')
                self.speaker_preview.setPlainText(
                    editing_line +
                    f'例文から判別した話者: {u.speaker or "（未判別）"}\n'
                    f'例文の設定: TTS {"使用" if u.use_tts else "不使用"} / '
                    f'Voice {u.voice or "（未指定）"}\n'
                    f'例文に適用した設定: {"話者別" if u.speaker in draft.speaker_tts or u.speaker in draft.speaker_voices else "作品既定"}')
                self._preview_utterance = u
                self.btn_edit_detected_speaker.setEnabled(bool(u.speaker))
                self.preview_state.setText('保存済み設定の結果' if kind is None else
                    '編集中の入力値を仮適用しています。変更は「追加・変更」から保存してください。')
            except (ValueError, re.error, IndexError) as exc:
                self._preview_utterance = None
                self.btn_edit_detected_speaker.setEnabled(False)
                self.editor_preview.setPlainText('編集中の規則は適用できません: ' + str(exc))
                self.preview_state.setText('設定を修正するまで保存できません。')

        def reveal_section(self, section: str, page: str):
            """Contextual navigation: basic result -> relevant detailed controls."""
            toggle = self.section_toggles.get(section)
            if toggle:
                toggle.setChecked(True)
            page_index = {'home': 0, 'hook': 1, 'tune': 2}[page]
            self.tabs.setCurrentIndex(page_index)
            widget = self.ui_sections.get(section)
            if widget is not None:
                QTimer.singleShot(0, lambda w=widget, p=page:
                    self.page_scrollers[p].ensureWidgetVisible(w, 0, 22))

        def show_task_section(self, section: str):
            self.reveal_section(section, 'tune')

        def edit_detected_speaker(self):
            u = getattr(self, '_preview_utterance', None)
            if not u or not u.speaker:
                self.show_task_section('extraction')
                return
            self.speaker_table.clearSelection()
            for row in range(self.speaker_table.rowCount()):
                if self.speaker_table.item(row, 0).text() == u.speaker:
                    self.speaker_table.selectRow(row)
                    break
            else:
                self.clear_speaker_editor()
                self.speaker_name.setText(u.speaker)
            self.tabs.setCurrentIndex(self.text_tab_index)
            self.page_scrollers['tune'].ensureWidgetVisible(self.ui_sections['speakers'])
            self.speaker_name.setFocus()

        def edit_preview_reading(self):
            chosen = self.example_raw.textCursor().selectedText().strip()
            self.clear_reading_editor()
            if chosen:
                self.reading_word.setText(chosen)
            self.show_task_section('reading')
            self.reading_word.setFocus()

        def edit_current_dialogue(self):
            u = self.controller.utterance
            if not u:
                self.note('台詞を取得してから編集を始めてください')
                return
            self.example_raw.setPlainText(u.raw)
            self.tabs.setCurrentIndex(self.text_tab_index)

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
            if self.current_profile() and QMessageBox.question(self, 'プロファイルを削除',
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
            if (not self.hook_host or self.attached_pid != self.pid.value()
                    or not self.host_generation):
                self.note('HOOKの接続完了後に探索できます。接続状態と診断を確認してください。')
                return
            self.clear_search_candidates()
            try:
                game = self.catalog.games.get(self.game_id)
                module = Path(game.executable).name if game and game.executable else ''
                self.hook_host.command('find_hooks', pid=self.pid.value(), boundary_module=module)
            except Exception as exc:
                self.note(str(exc))

        def insert_pc_hooks(self):
            try:
                if not (self.host_generation and self.connection_generation and self.hook_host
                        and self.attached_pid == self.pid.value()):
                    raise RuntimeError('先にゲームへLunaHookを接続してください')
                self.hook_host.command('insert_pc_hooks', pid=self.pid.value())
                self.note('本家Lunaの標準文字API用HOOK追加を要求しました。ゲームで台詞を表示してください。')
            except (OSError, RuntimeError, ValueError) as exc:
                self.note('PC用HOOK追加に失敗: ' + str(exc))

        def insert_hook_code(self, code: str):
            p = self.current_profile()
            if not p or not code:
                return
            try:
                if (not self.hook_host or not self.host_generation or
                        self.attached_pid != self.pid.value()):
                    raise RuntimeError('先にゲームへLunaHookを接続してください')
                self.hook_host.command('insert_hook', pid=self.pid.value(), code=code)
                if code not in p.inserted_hooks:
                    p.inserted_hooks.append(code)
                self.persist()
                if code in self.search_codes:
                    self.search_insert_requested.add(code)
                    self.refresh_search_codes()
                self.note('HOOKの追加を要求しました。実際の本文を取得したら用途を選んでください: ' + code)
            except Exception as exc:
                self.note('HOOKの追加に失敗: ' + str(exc))

        def insert_hook(self):
            self.insert_hook_code(self.manual_hook.text().strip())

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
                self.overview_voice.setText('音声: 接続失敗')
                return
            self.server_status.setText(f'Irodori: {result.status} ({result.origin}); {result.message}')
            self.overview_voice.setText('音声: ' + ('接続中' if result.status == 'connected' else result.status))
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
            self.connection_generation = 0
            self.attached_pid = 0
            self.clear_hook_observations()
            try:
                runtime = adopted_runtime()
                self.hook_host = HookHost(runtime.host, self.bridge.hook.emit,
                                          self.bridge.closed.emit, self.bridge.host_error.emit)
                self.hook_host.start()
                self.host_status.setText('LunaHook Host: launched')
            except Exception as exc:
                self.hook_host = None
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

        def pick_running_game(self):
            """A first-use flow independent of saved EXE, PID or known HOOK code."""
            target = choose_running_target(self)
            if target is None:
                return
            self.use_running_target(target)

        def use_running_target(self, target: WindowCandidate):
            if not target.executable or target.pid <= 0:
                raise ValueError('A readable executable and PID are required')
            matching = [(gid, game) for gid, game in self.catalog.games.items()
                        if ntpath.normcase(ntpath.normpath(game.executable)) ==
                        ntpath.normcase(ntpath.normpath(target.executable))]
            if len(matching) > 1:
                choices = [f'{game.name} ({gid[:8]})' for gid, game in matching]
                selected, ok = QInputDialog.getItem(self, '既存の作品設定を選択',
                    'この実行ファイルには複数の作品設定があります', choices, 0, False)
                if not ok:
                    return
                game_id = matching[choices.index(selected)][0]
            elif matching:
                game_id = matching[0][0]
            else:
                title = ntpath.splitext(ntpath.basename(target.executable))[0]
                game = self.catalog.create_game(title, target.executable)
                game_id = game.id
                self.persist()
                self.fill_games()
            self.game_choice.setCurrentIndex(self.game_choice.findData(game_id))
            self.pid.setValue(target.pid)
            self.hwnd.setText(hex(target.hwnd) if target.hwnd else '')
            self.hook_target.setText(f'{target.title or "(ウィンドウなし)"} — PID {target.pid} / {target.executable}')
            if target.hwnd:
                self.note(f'対象選択: {target.title} / PID {target.pid}。HOOKタブで接続してください。')
            else:
                self.note(f'PID {target.pid} をHOOK対象に選択。自動送りには別途ゲームウィンドウの選択が必要です。')
            self.tabs.setCurrentIndex(1)

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
                self.hook_target.setText(options[index])
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
                self.hook_target.setText(labels[index])
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

        def resume_saved_game(self):
            """Reuse the selected product profile without re-running setup or granting input."""
            profile = self.current_profile()
            if profile is None:
                self.note('プレイする作品を選択してください')
                return
            if profile.source == 'hook' and not any(h.role == 'text' for h in profile.hooks):
                self.tabs.setCurrentIndex(1)
                self.note('まずゲームから実際の本文を取得し、使用するHOOKを選択してください')
                return
            if not self.pid.value() or not self.hwnd.text().strip():
                self.detect_game()
            if not self.pid.value() or not self.hwnd.text().strip():
                self.note('実行中のゲームを選択してください。複数のゲームがある場合はウィンドウを指定します。')
                return
            if profile.source == 'ocr':
                self.begin_monitor(save_draft=False)
                return
            if self.attached_pid == self.pid.value() and self.host_generation:
                self._resume_monitor_requested = True
                self.start_saved_monitor_when_ready()
                return
            self.connect_game()
            # A failure to launch the Host must never grant auto input.
            if self.hook_host and self.hook_host.child and self.hook_host.child.poll() is None:
                self._resume_monitor_requested = True
                self.note('保存済みHOOKの復元待ちです。接続・取得の状態を表示します。')

        def start_saved_monitor_when_ready(self):
            if not self._resume_monitor_requested:
                return
            profile = self.current_profile()
            if not profile or not self.host_generation or not self.attached_pid:
                return
            if (any(h.identity in self.ambiguous_sources for h in profile.hooks) or
                    not all(any(h.matches(src) for src in self.sources.values()) for h in profile.hooks)):
                return
            self._resume_monitor_requested = False
            QTimer.singleShot(0, lambda: self.begin_monitor(save_draft=False))

        def save_acquisition_settings(self):
            """Commit explicitly relevant acquisition settings, not unrelated drafts."""
            profile = self.current_profile()
            if profile is None:
                self.note('先に作品を選択してください')
                return False
            try:
                updated = copy.deepcopy(profile)
                from .model import OCRSettings
                updated.source = self.source_mode.currentText()
                updated.ocr = OCRSettings(self.ocr_exe.text().strip(), self.ocr_lang.text().strip(),
                    tuple(int(x.strip()) for x in self.ocr_region.text().split(',')),
                    stable_samples=self.ocr_stability.value())
                updated.insert_pc_hooks = self.insert_pc_hooks_on_attach.isChecked()
                updated.hook_flush_ms = self.hook_flush.value()
                updated.hook_codepage = self.hook_codepage.value()
                updated.hook_max_buffer = self.hook_buffer.value()
                updated.hook_max_history = self.hook_history.value()
                updated.validate()
                self.catalog.games[self.game_id].profiles[updated.id] = updated
                self.persist()
                return True
            except Exception as exc:
                self.note('取得設定を保存できません: ' + str(exc))
                return False

        def connect_game(self):
            if not self.pid.value():
                self.note('実行中のゲーム・プロセスを選択してください')
                return
            if not self.current_profile():
                self.note('作品を登録してください')
                return
            # Connecting a game is not authorization to save draft voice,
            # advancement or text settings. Commit only capture controls.
            if not self.save_acquisition_settings():
                return
            if not self.hook_host or self.hook_host.child is None or self.hook_host.child.poll() is not None:
                self.start_host()
            if not self.hook_host or self.hook_host.child is None:
                self.note('LunaHook Hostの起動に失敗しました')
                return
            try:
                p = self.current_profile()
                if p is None:
                    raise ValueError('select a game and profile')
                if self.attached_pid == self.pid.value() and self.host_generation:
                    self.note('このゲームには既にLunaHookが接続されています。')
                    return
                self.attached_pid = 0
                self.host_generation = ''
                self.connection_generation = 0
                self.hook_host.command('settings', flush_ms=p.hook_flush_ms,
                    codepage=p.hook_codepage, max_buffer=p.hook_max_buffer, max_history=p.hook_max_history)
                self.hook_host.command('connect', pid=self.pid.value())
                self.note('LunaHookへの接続を要求しました。接続完了または診断結果を確認してください。')
            except Exception as exc:
                self.note(str(exc))

        def clear_hook_observations(self):
            self.sources.clear()
            self.source_samples.clear()
            self.ambiguous_sources.clear()
            self.source_rows.clear()
            self.search_codes.clear()
            self.search_insert_requested.clear()
            self.source_table.setRowCount(0)
            self.search_code_choice.clear()
            self.hook_preview.clear()
            self.hook_technical_info.setText('取得元を選択すると識別情報を表示します')
            self.refresh_candidate_actions()
            self.hook_capture_status.setText('取得: 未接続または本文未受信')
            self.refresh_hook_connection()

        def insert_selected_search(self):
            code = self.search_code_choice.currentData()
            if code:
                self.insert_hook_code(code)

        def chosen_source(self):
            row = self.source_table.currentRow()
            if row < 0:
                return None
            item = self.source_table.item(row, 2)
            if item is None:
                return None
            identity = item.data(256)
            if not isinstance(identity, (tuple, list)) or len(identity) != 2:
                return None
            key, address = identity
            source = self.sources.get(key)
            return source if source and source.address == address and key not in self.ambiguous_sources else None

        def remove_candidate_row(self, row: int):
            self.source_table.removeRow(row)
            self.source_rows = {key: index - (index > row) for key, index in self.source_rows.items()
                                if index != row}

        def clear_search_candidates(self):
            self.search_codes.clear()
            self.search_insert_requested.clear()
            self.refresh_search_codes()

        def add_search_candidate(self, code: str, _sample: str = ''):
            if not code or any(source.hook_code == code for source in self.sources.values()):
                return
            self.search_codes.add(code)
            self.refresh_search_codes()

        def refresh_search_codes(self):
            previous = self.search_code_choice.currentData()
            self.search_code_choice.blockSignals(True)
            self.search_code_choice.clear()
            for code in sorted(self.search_codes):
                label = ('試用要求済み: ' if code in self.search_insert_requested else '未追加: ') + code
                self.search_code_choice.addItem(label, code)
            if previous in self.search_codes:
                self.search_code_choice.setCurrentIndex(self.search_code_choice.findData(previous))
            self.search_code_choice.blockSignals(False)
            self.btn_insert_search.setEnabled(bool(self.search_codes))

        def refresh_capture_status(self):
            if self.attached_pid and self.host_generation:
                received = sum(bool(values) for values in self.source_samples.values())
                self.hook_capture_status.setText(
                    f'ゲームに接続中: 本文を受信した取得元 {received}件 / 未受信 {len(self.sources) - received}件')
            if hasattr(self, 'overview_capture'):
                self.overview_capture.setText('台詞取得: ' + ('接続中' if self.attached_pid and self.host_generation
                                              else '未接続'))

        def refresh_candidate_actions(self, *_):
            source = self.chosen_source()
            history = self.source_samples.get(source.identity, ()) if source else ()
            self.text_hook.setEnabled(bool(source and history))
            self.speaker_hook.setEnabled(bool(source and history))
            self.btn_test_hook_text.setEnabled(bool(history))

        def update_candidate(self, source: ThreadSource, sample: str | None):
            code = source.hook_code
            if code in self.search_codes:
                self.search_codes.discard(code)
                self.search_insert_requested.discard(code)
                self.refresh_search_codes()
            key = (source.identity, source.address)
            if sample is not None and sample.strip():
                history = self.source_samples.setdefault(source.identity, deque(maxlen=4))
                if not history or history[-1] != sample:
                    history.append(sample)
            latest = self.source_samples.get(source.identity, ())
            if not latest:
                self.refresh_capture_status()
                return
            if key not in self.source_rows:
                row = self.source_table.rowCount()
                self.source_table.insertRow(row)
                self.source_rows[key] = row
            else:
                row = self.source_rows[key]
            profile = self.current_profile()
            role = '未割当'
            if profile:
                for i, hook in enumerate(profile.hooks):
                    if hook.matches(source):
                        role = f'{hook.role} #{1 + sum(h.role == hook.role for h in profile.hooks[:i])}'
                        break
            if source.identity in self.ambiguous_sources:
                role = '識別衝突（使用不可）'
            values = (latest[-1], role, source.hook_name or '取得元', '受信中')
            for col, value in enumerate(values):
                item = self.source_table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.source_table.setItem(row, col, item)
                item.setText(value)
                if col == 2:
                    item.setData(256, key)
            if self.source_table.currentRow() == row:
                self.show_hook_preview()
            self.refresh_candidate_actions()
            self.refresh_capture_status()

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
            self.selected_hooks.setText('選択中の取得元: ' + ', '.join(f'{"本文" if h.role == "text" else "話者"}: {h.code}' for h in hooks) if hooks else '使用するHOOKが未選択です')
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
            if (event.kind == 'hook_found' and event.process_id == self.attached_pid
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                self.add_search_candidate(event.detail, event.text)
                self.note('追加探索: HOOKコードを検出しました（取得方法を調整から試用できます）')
            if event.kind == 'connected' and event.process_id == self.pid.value():
                if self.connection_generation != event.connection_generation or self.host_generation != event.host_generation:
                    self.stop_session()
                    self.clear_hook_observations()
                self.host_generation = event.host_generation
                self.connection_generation = event.connection_generation
                self.attached_pid = event.process_id
                self.validation_trace.emit('hook_connected', pid=event.process_id,
                    generation=event.host_generation, connection=event.connection_generation)
                self.host_status.setText(f'LunaHook connected: PID {event.process_id}')
                self.refresh_capture_status()
                self.start_saved_monitor_when_ready()
                p = self.current_profile()
                if p and p.source == 'hook':
                    if p.insert_pc_hooks:
                        self.hook_host.command('insert_pc_hooks', pid=event.process_id)
                    for code in p.inserted_hooks:
                        self.hook_host.command('insert_hook', pid=event.process_id, code=code)
            elif event.kind == 'disconnected' and event.process_id == self.pid.value():
                self.stop_session()
                self.attached_pid = 0
                self.host_generation = ''
                self.connection_generation = 0
                self.clear_hook_observations()
                self.host_status.setText('LunaHook disconnected')
                self._resume_monitor_requested = False
            if (event.kind == 'removed' and event.source and event.process_id == self.pid.value()
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                key = event.source.identity
                if self.sources.get(key) and self.sources[key].address == event.source.address:
                    self.sources.pop(key, None)
                if self.controller.active_text_source == key:
                    self.stop_session()
                    self.note('Active dialogue HOOK disappeared; restart monitoring after checking sources')
                row = self.source_rows.get((key, event.source.address))
                if row is not None:
                    self.remove_candidate_row(row)
                self.source_samples.pop(key, None)
                self.refresh_hook_connection()
                self.refresh_capture_status()
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
                    # A colliding thread has no safe text ownership; do not
                    # display its output under the first thread's identity.
                else:
                    self.sources[key] = source
                    self.update_candidate(source, event.text if event.kind == 'output' else None)
                self.refresh_hook_connection()
                self.refresh_capture_status()
                if event.kind == 'created':
                    self.start_saved_monitor_when_ready()
                    self.validation_trace.emit('hook_candidate_created', pid=event.process_id,
                        generation=event.host_generation, connection=event.connection_generation,
                        source=source.identity, hook_sequence=event.sequence)
                else:
                    self.validation_trace.emit('hook_candidate_output', pid=event.process_id,
                        generation=event.host_generation, connection=event.connection_generation,
                        source=source.identity, hook_sequence=event.sequence,
                        raw_hash=text_digest(event.text))
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
            if event.kind == 'inserted' and event.process_id == self.pid.value():
                self.note(f'LunaによるHOOK挿入: {event.detail} {event.source.hook_code if event.source else ""}'.strip())
            if event.kind == 'diagnostic':
                self.validation_trace.emit('hook_native_diagnostic',
                    pid=event.process_id,
                    generation=event.host_generation,
                    connection=event.connection_generation,
                    info_type=event.detail.partition(':')[0][:64],
                    message_hash=text_digest(event.detail))
                # Upstream engine detection and injection warnings are product
                # connection facts. Suppressing them masked a genuine failure.
                if event.detail in ('existing_luna_injection_detected', 'cannot_attach_to_host_process'):
                    self._resume_monitor_requested = False
                if event.detail == 'existing_luna_injection_detected':
                    self.host_status.setText('対象には既にLunaHookが注入されています。ゲームを再起動して再接続してください。')
                elif event.detail == 'cannot_attach_to_host_process':
                    self.host_status.setText('LunaHost自身には接続できません。ゲームを選択してください。')
                self.note('LunaHost: ' + event.detail)

        def select_hook(self, role: str):
            p = self.current_profile()
            source = self.chosen_source()
            if not p or not source:
                self.note('本文の出力を確認できたHOOK候補を選択してください')
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
            self.hook_preview.setPlainText('\n── 前回の取得 ──\n'.join(reversed(history)))
            if source:
                self.hook_technical_info.setText(
                    f'HOOKコード: {source.hook_code} / PID: {source.process_id} / '
                    f'Context: {source.context}, {source.context2} / Thread: {hex(source.address)}')
            else:
                self.hook_technical_info.setText('取得元を選択すると識別情報を表示します')
            self.refresh_candidate_actions()

        def use_selected_hook_as_example(self):
            source = self.chosen_source()
            samples = self.source_samples.get(source.identity, ()) if source else ()
            sample = samples[-1] if samples else ''
            if not sample:
                self.note('実際に受信された本文を選択してください')
                return
            self.example_raw.setPlainText(sample)
            self.tabs.setCurrentIndex(self.text_tab_index)

        def begin_monitor(self, *, save_draft=True):
            try:
                if save_draft and not self.save_profile():
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
            self.native_audio_setup_error = None
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
                self.native_audio_setup_error = reason
                self.note('Native audio unavailable: ' + reason)
                u = self.controller.utterance
                if u and u.advance_mode == AdvanceMode.AUDIO and not u.use_tts and not u.uncertain_text_fallback:
                    self.controller.pause('native_audio_unavailable_manual_handoff')
                self.refresh_status()
                return
            self.native_audio = native
            self.validation_trace.emit('native_ready', session=session, audio_pid=native.pid)
            self.note('Process audio helper started; waiting for actual captured PCM')
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
            self.preview.setPlainText(
                f'{u.speaker or "（話者未設定）"}: {u.text}\n'
                f'音声: {"Irodori / " + (u.voice or "Voice未設定") if u.use_tts else "ゲーム原音または無音"}  '
                f'進行方式: {"音声判断" if u.advance_mode == AdvanceMode.AUDIO else "文字のみ判断"}\n'
                f'取得原文: {u.raw}')
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
                    u = c.utterance
                    if (self.native_audio and u and
                            u.advance_mode == AdvanceMode.AUDIO and not u.use_tts):
                        self.validation_trace.emit('native_capture_summary',
                            session=token.session, line=token.line,
                            audio_pid=self.native_audio.pid,
                            **self.native_audio.capture_status())
                    self._last_validation_handoff = marker
            targets = (f'HOOK {c.target.hook_pid or c.target.pid}; audio {c.target.audio_pid or c.target.pid}; '
                       f'input {c.target.pid}/{hex(c.target.hwnd)}') if c.target else 'no target'
            self.status.setText(f'Status: {c.reason} | permission={c.authorized} | '
                                f'line={c.line_no} | sent={c.sent} | completion={c.presentation_reason or "pending"} | {targets}')
            self.overview_advance.setText('自動送り: ' +
                ('許可中' if c.authorized else '停止 / 要確認') +
                (' ― ' + c.pause_reason if c.pause_reason else ''))
            if c.profile and c.utterance:
                self.overview_capture.setText('台詞: ' +
                    (c.utterance.speaker or '話者未設定') + ' / ' + c.utterance.text[:35])
            elif not self.attached_pid:
                self.overview_capture.setText('台詞取得: 未接続')
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

        def on_host_error(self, message):
            self.note('LunaHost native error: ' + message)

        def on_closed(self, reason):
            self._resume_monitor_requested = False
            self.stop_session()
            self.attached_pid = 0
            self.host_generation = ''
            self.connection_generation = 0
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
