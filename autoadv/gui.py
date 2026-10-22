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
                                   QTableWidget, QTableWidgetItem, QAbstractItemView, QTabWidget, QHeaderView, QToolButton, QMenu, QButtonGroup)

    class SignalBridge(QObject):
        hook = Signal(int, object)
        closed = Signal(int, str)
        host_error = Signal(int, str)
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
            self.bridge.hook.connect(self.on_host_hook)
            self.bridge.closed.connect(self.on_host_closed)
            self.bridge.host_error.connect(self.on_host_error_from_host)
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
            self._host_epoch = 0
            self.host_generation = ''
            self.connection_generation = 0
            self.attached_pid = 0
            self._resume_monitor_requested = False
            self._pending_monitor_apply = False
            self._loading_profile = False
            self._profile_dirty = False
            self._override_drafts = {}
            self.sources: dict[str, ThreadSource] = {}
            self.source_samples: dict[str, deque[str]] = {}
            self.ambiguous_sources: set[str] = set()
            self.source_rows: dict[tuple[str, int], int] = {}
            # Advanced search results are transient acquisition inputs. Native
            # output, not discovery provenance or search samples, is what a
            # player chooses; trial codes become persistent only when selected.
            self._searched_codes: set[str] = set()
            self._trial_hook_codes: set[str] = set()
            self._connecting_pid = 0
            self.game_id = ''
            self.profile_id = ''
            self.server: IrodoriServer | None = None
            self.ocr_worker: OCRWorker | None = None
            self.native_audio: NativeAudio | None = None
            self.native_audio_setup_error: str | None = None
            self.manual_monitor: WindowsManualMonitor | None = None
            self._manual_input_serial = 0
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
            header = QHBoxLayout()
            self.context_banner = QLabel('作品を選択してください')
            self.context_banner.setStyleSheet('font-size: 15px; font-weight: 600; padding: 7px;')
            header.addWidget(self.context_banner, 1)
            self.profile_save_state = QLabel('作品を選択してください')
            self.profile_save_state.setProperty('statusChip', True)
            header.addWidget(self.profile_save_state)
            self.btn_save = QPushButton('作品設定を保存')
            self.btn_save.setProperty('primary', True)
            self.btn_save.clicked.connect(self.save_profile)
            header.addWidget(self.btn_save)
            root_layout.addLayout(header)
            overview = QHBoxLayout()
            self.overview_capture = QLabel('取得: 未接続')
            self.overview_voice = QLabel('音声: 未接続')
            self.overview_advance = QLabel('自動送り: 停止')
            for label in (self.overview_capture, self.overview_voice, self.overview_advance):
                label.setProperty('statusChip', True)
                overview.addWidget(label, 1)
            root_layout.addLayout(overview)
            root_layout.addWidget(tabs, 1)
            self.setCentralWidget(root)
            root.setStyleSheet("""
                QGroupBox { font-weight: 600; margin-top: 10px; padding-top: 10px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
                QPushButton, QToolButton, QComboBox, QLineEdit, QSpinBox { min-height: 28px; }
                QPushButton { padding: 4px 10px; }
                QPushButton[primary="true"] { font-weight: 700; border: 2px solid palette(highlight); }
                QPushButton:checked { background: palette(highlight); color: palette(highlighted-text); }
                QTableWidget { gridline-color: palette(mid); selection-background-color: palette(highlight); }
                QHeaderView::section { font-weight: 600; padding: 5px; }
                QLabel[statusChip="true"] { padding: 6px 10px; border: 1px solid palette(mid); border-radius: 6px; }
            """)
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
            self.section_summaries = {}

            def group(title, *, page='home', section=None, detail=False):
                box = QGroupBox(title)
                form = QFormLayout(box)
                if detail:
                    wrapper = QWidget()
                    outer = QVBoxLayout(wrapper)
                    outer.setContentsMargins(0, 0, 0, 0)
                    toggle = QToolButton()
                    toggle.setText('設定を編集 — ' + title)
                    toggle.setCheckable(True)
                    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                    toggle.setArrowType(Qt.ArrowType.RightArrow)
                    toggle.toggled.connect(box.setVisible)
                    summary = QLabel('現在値を確認中')
                    summary.setWordWrap(True)
                    summary.setStyleSheet('padding: 2px 8px;')
                    if section:
                        self.section_toggles[section] = toggle
                        self.section_summaries[section] = summary
                    toggle.toggled.connect(lambda checked, button=toggle:
                                           button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
                    box.setVisible(False)
                    outer.addWidget(toggle)
                    outer.addWidget(summary)
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

            self.inherit_controls = {}
            self.inherit_source_labels = {}
            self.profile_setting_rows = {}

            def profile_setting_row(key, control):
                wrapper = QWidget()
                layout = QHBoxLayout(wrapper)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(8)
                layout.addWidget(control, 1)
                inherit = QCheckBox('共通値を使用')
                inherit.toggled.connect(lambda checked, name=key: self.inheritance_changed(name, checked))
                source = QLabel('作品設定')
                source.setMinimumWidth(115)
                layout.addWidget(inherit)
                layout.addWidget(source)
                self.inherit_controls[key] = inherit
                self.inherit_source_labels[key] = source
                self.profile_setting_rows[key] = wrapper
                return wrapper

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
                'default_tts': '未知話者にもIrodori TTSを使用', 'default_voice': '既定Voice',
                'reading_ms': '最低表示時間 (ms)', 'per_character_wait_ms': '1文字あたり待機時間 (ms)',
                'native_helper': 'Process Loopbackヘルパー',
                'asr_model': '日本語ASRモデル', 'asr_tokens': 'ASR語彙ファイル', 'input_key': '送りキーコード',
                'next_line_timeout_ms': '次台詞待ち (ms)'}
            for key in COMMON_FIELDS:
                value = getattr(self.catalog.common, key)
                if key == 'advance_mode':
                    control = QWidget()
                    layout = QHBoxLayout(control)
                    layout.setContentsMargins(0, 0, 0, 0)
                    mode_group = QButtonGroup(self)
                    mode_group.setExclusive(True)
                    text_button = QPushButton('文字のみ判断')
                    audio_button = QPushButton('音声判断')
                    for button, mode in ((text_button, AdvanceMode.TEXT), (audio_button, AdvanceMode.AUDIO)):
                        button.setCheckable(True)
                        button.setProperty('advanceMode', mode.value)
                        mode_group.addButton(button)
                        layout.addWidget(button, 1)
                    (audio_button if value == AdvanceMode.AUDIO else text_button).setChecked(True)
                    self.common_advance_mode_group = mode_group
                    self.common_advance_text_button = text_button
                    self.common_advance_audio_button = audio_button
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
            self.server_status = QLabel('Irodori: 未接続')
            self.btn_host = QPushButton('LunaHook Hostを単独起動（通常は接続時に自動起動）')
            self.btn_host.clicked.connect(self.start_host)
            self.host_status = QLabel('LunaHook: 未接続')

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
            self.audio_exe.setPlaceholderText('別プロセスで音声を出す場合のみEXEを指定')
            self.input_exe = QLineEdit()
            self.input_exe.setPlaceholderText('別プロセスへ入力する場合のみEXEを指定')
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
            self.hook_target = QLabel('まず「プレイ」で実行中のゲームを選択してください')
            self.hook_target.setWordWrap(True)
            capture.addRow(self.hook_target)
            self.pid = QSpinBox()
            self.pid.setRange(0, 2147483647)
            self.hwnd = QLineEdit()
            self.hwnd.setPlaceholderText('例: 0x12345')
            hook_details.addRow('HOOK対象PID（現在の実行）', self.pid)
            self.audio_pid = QSpinBox()
            self.audio_pid.setRange(0, 2147483647)
            self.audio_pid.setSpecialValueText('HOOK PIDを使用')
            self.input_pid = QSpinBox()
            self.input_pid.setRange(0, 2147483647)
            self.input_pid.setSpecialValueText('HOOK PIDを使用')
            target_details.addRow('音声出力プロセスPID（未指定ならHOOK）', self.audio_pid)
            target_details.addRow('入力先プロセスPID（未指定ならHOOK）', self.input_pid)
            target_details.addRow('入力先ウィンドウ識別子（HWND）', self.hwnd)
            self.attach = QPushButton('接続を再試行')
            self.attach.clicked.connect(self.connect_game)
            hook_details.addRow(self.attach)
            self.btn_discover = QPushButton('登録済みEXEから実行中ゲームを再検出')
            self.btn_discover.clicked.connect(self.detect_game)
            hook_details.addRow(self.btn_discover)
            self.btn_discover_all = QPushButton('登録済み全作品から実行中ゲームを識別')
            self.btn_discover_all.clicked.connect(self.detect_registered_game)
            hook_details.addRow(self.btn_discover_all)
            self.btn_audio_target = QPushButton('保存EXEから音声プロセスを検出')
            self.btn_input_target = QPushButton('保存EXEから入力ウィンドウを検出')
            self.btn_audio_target.clicked.connect(lambda: self.detect_associated_target('audio'))
            self.btn_input_target.clicked.connect(lambda: self.detect_associated_target('input'))
            target_details.addRow(self.btn_audio_target)
            target_details.addRow(self.btn_input_target)
            self.source_mode_widget = QWidget()
            source_mode_layout = QHBoxLayout(self.source_mode_widget)
            source_mode_layout.setContentsMargins(0, 0, 0, 0)
            self.source_mode_group = QButtonGroup(self)
            self.source_mode_group.setExclusive(True)
            self.source_hook_button = QPushButton('LunaHook')
            self.source_ocr_button = QPushButton('OCR')
            for button, value in ((self.source_hook_button, 'hook'), (self.source_ocr_button, 'ocr')):
                button.setCheckable(True)
                button.setProperty('sourceMode', value)
                self.source_mode_group.addButton(button)
                source_mode_layout.addWidget(button, 1)
            self.source_hook_button.setChecked(True)
            capture.addRow('取得方法', self.source_mode_widget)
            self.ocr_exe = QLineEdit('tesseract')
            self.ocr_lang = QLineEdit('jpn')
            self.ocr_region = QLineEdit('0,0,640,180')
            ocr_details.addRow('Tesseractの実行ファイル', self.ocr_exe)
            ocr_details.addRow('OCRの言語', self.ocr_lang)
            ocr_details.addRow('OCRの領域 x,y,幅,高さ', self.ocr_region)
            self.ocr_stability = QSpinBox()
            self.ocr_stability.setRange(1, 10)
            ocr_details.addRow('OCRの安定確認回数', self.ocr_stability)
            self.source_ocr_button.toggled.connect(
                lambda checked: checked and self.reveal_section('ocr_details', 'hook'))

            capture.addRow(QLabel('ゲームで台詞を表示すると、実際に本文を受信した取得元だけが並びます。'
                                  '使用・役割・優先順位・話者対応を各行で直接設定します。'))
            self.source_table = QTableWidget(0, 7)
            self.source_table.setHorizontalHeaderLabels(
                ['使用', '役割', '優先', '対応話者', '実際に受信した本文', '取得元', '状態'])
            self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.source_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_table.setMinimumHeight(245)
            self.source_table.verticalHeader().setVisible(False)
            self.source_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
            self.source_table.setColumnWidth(0, 55)
            self.source_table.setColumnWidth(1, 105)
            self.source_table.setColumnWidth(2, 70)
            self.source_table.setColumnWidth(3, 210)
            self.source_table.setColumnWidth(5, 145)
            self.source_table.setColumnWidth(6, 105)
            self.source_table.itemSelectionChanged.connect(self.show_hook_preview)
            self.source_table.itemChanged.connect(self.hook_use_changed)
            capture.addRow(self.source_table)
            row = QHBoxLayout()
            self.btn_find = QPushButton('ほかの取得候補を探す')
            self.btn_find.clicked.connect(self.find_hooks)
            self.btn_find.setProperty('primary', True)
            self.btn_test_hook_text = QPushButton('選択行の原文で台詞・音声を調整')
            self.btn_test_hook_text.clicked.connect(self.use_selected_hook_as_example)
            self.btn_recovery = QPushButton('取得条件を調整')
            self.btn_recovery.clicked.connect(lambda: self.reveal_section('hook_details', 'hook'))
            row.addWidget(self.btn_find)
            row.addWidget(self.btn_test_hook_text)
            row.addWidget(self.btn_recovery)
            capture.addRow(row)
            self.hook_preview = QPlainTextEdit()
            self.hook_preview.setReadOnly(True)
            self.hook_preview.setMaximumHeight(115)
            capture.addRow('選択行の直近履歴', self.hook_preview)
            self.hook_capture_status = QLabel('HOOK取得: 未接続')
            self.hook_capture_status.setWordWrap(True)
            capture.addRow(self.hook_capture_status)
            self.selected_hooks = QLabel('使用するHOOKが未選択です')
            self.selected_hooks.setWordWrap(True)
            capture.addRow('現在の割当', self.selected_hooks)
            self.hook_reconnect = QLabel('保存済みHOOK: 未接続')
            self.hook_reconnect.setWordWrap(True)
            capture.addRow(self.hook_reconnect)
            self.active_hook_status = QLabel('監視中の本文HOOK: なし')
            capture.addRow(self.active_hook_status)

            self.btn_pc_hooks = QPushButton('Win32汎用HOOKを追加する')
            self.btn_pc_hooks.clicked.connect(self.insert_pc_hooks)
            hook_details.addRow(self.btn_pc_hooks)
            self.insert_pc_hooks_on_attach = QCheckBox('この作品では接続時にWin32汎用HOOKを追加する')
            hook_details.addRow(self.insert_pc_hooks_on_attach)
            self.manual_hook = QLineEdit()
            self.manual_hook.setPlaceholderText('Luna HOOKコードを手入力')
            hook_details.addRow('手動HOOKコード（通常は不要）', self.manual_hook)
            self.hook_technical_info = QLabel('取得元を選択すると識別情報を表示します')
            self.hook_technical_info.setWordWrap(True)
            hook_details.addRow('選択行の技術情報', self.hook_technical_info)
            self.btn_insert = QPushButton('HOOKコードを手動追加')
            self.btn_insert.clicked.connect(self.insert_hook)
            hook_details.addRow(self.btn_insert)
            self.btn_to_play = QPushButton('プレイ画面へ')
            self.btn_to_play.clicked.connect(lambda: self.tabs.setCurrentIndex(0))
            capture.addRow(self.btn_to_play)

            # Speaker settings are edited on the row they affect. Selection is
            # only a visual focus; it is never a prerequisite for changing TTS/Voice.
            speech = group('話者別の音声設定', page='tune', section='speakers')
            speech.addRow(self.btn_server)
            speech.addRow(self.server_status)
            speech.addRow(QLabel('一覧にない話者には作品既定を使用します。話者行のTTS・Voiceはその場で保存され、次の台詞から適用されます。'))
            self.default_tts = QCheckBox('未登録の話者でもTTSで読み上げる')
            speech.addRow('未登録話者のTTS', profile_setting_row('default_tts', self.default_tts))
            self.default_voice = QComboBox()
            self.default_voice.setEditable(True)
            speech.addRow('未登録話者のVoice', profile_setting_row('default_voice', self.default_voice))
            self.speaker_table = QTableWidget(0, 5)
            self.speaker_table.setHorizontalHeaderLabels(['話者', 'TTS', 'Voice', '試聴', '削除'])
            self.speaker_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.speaker_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.speaker_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
            self.speaker_table.setAlternatingRowColors(True)
            self.speaker_table.setMinimumHeight(190)
            self.speaker_table.verticalHeader().setVisible(False)
            self.speaker_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.speaker_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.speaker_table.setColumnWidth(1, 70)
            self.speaker_table.setColumnWidth(3, 80)
            self.speaker_table.setColumnWidth(4, 70)
            self.speaker_table.itemChanged.connect(self.speaker_row_item_changed)
            speech.addRow(self.speaker_table)
            add_speaker = QHBoxLayout()
            self.new_speaker_name = QLineEdit()
            self.new_speaker_name.setPlaceholderText('新しい話者名')
            self.new_speaker_tts = QCheckBox('TTS')
            self.new_speaker_tts.setChecked(True)
            self.new_speaker_voice = QComboBox()
            self.new_speaker_voice.setEditable(True)
            self.new_speaker_voice.setMinimumWidth(180)
            self.btn_add_speaker = QPushButton('話者を追加')
            self.btn_add_speaker.clicked.connect(self.add_speaker_inline)
            add_speaker.addWidget(self.new_speaker_name, 2)
            add_speaker.addWidget(self.new_speaker_tts)
            add_speaker.addWidget(self.new_speaker_voice, 2)
            add_speaker.addWidget(self.btn_add_speaker)
            speech.addRow(add_speaker)
            speech.addRow(QLabel('音声設定と自動進行方式は独立です。現在進行中の台詞は変更しません。'))
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
            self.example_raw.textChanged.connect(self.refresh_rule_preview)
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
            reading.addRow(QLabel('表示本文やASR照合文は変更せず、TTS読み上げだけを単語単位で置換します。'))
            self.reading_table = QTableWidget(0, 3)
            self.reading_table.setHorizontalHeaderLabels(['表記', '読み', '削除'])
            self.reading_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.reading_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.reading_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
            self.reading_table.setAlternatingRowColors(True)
            self.reading_table.setMinimumHeight(155)
            self.reading_table.verticalHeader().setVisible(False)
            self.reading_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.reading_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.reading_table.setColumnWidth(2, 70)
            self.reading_table.itemChanged.connect(self.reading_row_item_changed)
            reading.addRow(self.reading_table)
            add_reading = QHBoxLayout()
            self.new_reading_word = QLineEdit()
            self.new_reading_word.setPlaceholderText('表記')
            self.new_reading_value = QLineEdit()
            self.new_reading_value.setPlaceholderText('読み')
            self.btn_add_reading = QPushButton('読みを追加')
            self.btn_add_reading.clicked.connect(self.add_reading_inline)
            add_reading.addWidget(self.new_reading_word, 2)
            add_reading.addWidget(self.new_reading_value, 2)
            add_reading.addWidget(self.btn_add_reading)
            reading.addRow(add_reading)

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
            advance_details.addRow('送信する仮想キーコード', profile_setting_row('input_key', self.input_key))
            self.native_helper = QLineEdit()
            self.asr_model = QLineEdit()
            self.asr_tokens = QLineEdit()
            audio_details.addRow('音声捕捉ヘルパーDLL（音声判断のみ）', profile_setting_row('native_helper', self.native_helper))
            audio_details.addRow('日本語ASRモデル（音声判断のみ）', profile_setting_row('asr_model', self.asr_model))
            audio_details.addRow('ASR語彙ファイル', profile_setting_row('asr_tokens', self.asr_tokens))
            self.next_line_timeout = QSpinBox()
            self.next_line_timeout.setRange(1000, 120000)
            advance_details.addRow('次の台詞を待つ時間（ms）', profile_setting_row('next_line_timeout_ms', self.next_line_timeout))
            self.repeat = QComboBox()
            self.repeat.addItem('連続した同文を重複として扱う', 'distinct_only')
            self.repeat.addItem('送り後の同文は別台詞として扱う', 'each_after_input')
            advance_details.addRow('同一本文の連続取得', self.repeat)
            live_dialogue = group('現在の台詞と適用結果', section='current_dialogue')
            self.preview = QPlainTextEdit()
            self.preview.setReadOnly(True)
            self.preview.setMinimumHeight(95)
            live_dialogue.addRow(self.preview)
            self.btn_edit_current = QPushButton('現在の台詞を使って本文・話者を調整')
            self.btn_edit_current.clicked.connect(self.edit_current_dialogue)
            live_dialogue.addRow(self.btn_edit_current)

            for w in (self.structure_pattern, self.structure_condition, self.replace_pattern, self.replace_value):
                w.textChanged.connect(lambda _=None, control=w: self.preview_from_control(control))
            for w in (self.structure_enabled, self.replace_regex, self.replace_enabled, self.default_tts):
                w.toggled.connect(lambda _=None, control=w: self.preview_from_control(control))
            for w in (self.replace_target, self.default_voice):
                w.currentTextChanged.connect(lambda _=None, control=w: self.preview_from_control(control))
            advance = group('進行方式と自動送り', section='advance')
            self.advance_mode_widget = QWidget()
            mode_layout = QHBoxLayout(self.advance_mode_widget)
            mode_layout.setContentsMargins(0, 0, 0, 0)
            self.advance_mode_group = QButtonGroup(self)
            self.advance_mode_group.setExclusive(True)
            self.advance_text_button = QPushButton('文字のみ判断')
            self.advance_audio_button = QPushButton('音声判断')
            for button, value in ((self.advance_text_button, AdvanceMode.TEXT.value),
                                  (self.advance_audio_button, AdvanceMode.AUDIO.value)):
                button.setCheckable(True)
                button.setProperty('advanceMode', value)
                self.advance_mode_group.addButton(button)
                mode_layout.addWidget(button, 1)
            self.advance_text_button.setChecked(True)
            advance.addRow('進行方式', profile_setting_row('advance_mode', self.advance_mode_widget))
            self.uncertain_text_fallback = QCheckBox('不確実時は文字数時間で進む（途中送りの可能性あり）')
            advance.addRow('音声判断のフォールバック', profile_setting_row('uncertain_text_fallback', self.uncertain_text_fallback))
            self.per_character_wait_ms = QSpinBox()
            self.per_character_wait_ms.setRange(1, 600000)
            self.per_character_wait_ms.setSuffix(' ms / 文字')
            advance.addRow('記号除外の1文字待機', profile_setting_row('per_character_wait_ms', self.per_character_wait_ms))
            self.reading_ms = QSpinBox()
            self.reading_ms.setRange(1, 600000)
            self.reading_ms.setSuffix(' ms')
            advance.addRow('最低表示時間', profile_setting_row('reading_ms', self.reading_ms))
            self.wait_preview = QLabel('計数: - / 待機時間: -')
            advance.addRow('現在台詞', self.wait_preview)
            self.audio_environment_summary = QLabel('音声判断環境: 未確認')
            self.audio_environment_summary.setWordWrap(True)
            advance.addRow(self.audio_environment_summary)
            advance.addRow(QLabel('文字のみ判断は原音終了を確認しません。音声判断で根拠が不足した場合は上のフォールバック設定に従います。'))
            self.advance_authorization = QPushButton('自動送り: 停止')
            self.advance_authorization.setCheckable(True)
            self.advance_authorization.setProperty('primary', True)
            self.advance_authorization.toggled.connect(self.set_advance_authorization)
            advance.addRow('入力許可', self.advance_authorization)
            self.status = QLabel('監視: 停止 / 自動送り: 停止')
            self.status.setWordWrap(True)
            advance.addRow('現在状態', self.status)
            self.profile_field_controls = {
                'advance_mode': self.advance_mode_widget,
                'uncertain_text_fallback': self.uncertain_text_fallback,
                'default_tts': self.default_tts, 'default_voice': self.default_voice,
                'reading_ms': self.reading_ms,
                'per_character_wait_ms': self.per_character_wait_ms,
                'native_helper': self.native_helper, 'asr_model': self.asr_model,
                'asr_tokens': self.asr_tokens, 'input_key': self.input_key,
                'next_line_timeout_ms': self.next_line_timeout,
            }
            for button in (self.advance_text_button, self.advance_audio_button):
                button.toggled.connect(lambda checked, key='advance_mode': checked and self.profile_setting_changed(key))
            for button in (self.source_hook_button, self.source_ocr_button):
                button.toggled.connect(lambda checked: checked and self.mark_profile_dirty())
            for key, control in self.profile_field_controls.items():
                if key == 'advance_mode':
                    continue
                if isinstance(control, QCheckBox):
                    control.toggled.connect(lambda _=False, name=key: self.profile_setting_changed(name))
                elif isinstance(control, QSpinBox):
                    control.valueChanged.connect(lambda _=0, name=key: self.profile_setting_changed(name))
                elif isinstance(control, QComboBox):
                    control.currentTextChanged.connect(lambda _='', name=key: self.profile_setting_changed(name))
                elif isinstance(control, QLineEdit):
                    control.textChanged.connect(lambda _='', name=key: self.profile_setting_changed(name))
            for control in (self.hook_flush, self.hook_codepage, self.hook_history, self.hook_buffer,
                            self.repeat, self.ocr_exe, self.ocr_lang, self.ocr_region,
                            self.ocr_stability, self.insert_pc_hooks_on_attach, self.game_exe, self.audio_exe, self.input_exe):
                signal = (control.currentIndexChanged if isinstance(control, QComboBox) else
                          control.valueChanged if isinstance(control, QSpinBox) else
                          control.toggled if isinstance(control, QCheckBox) else control.textChanged)
                signal.connect(lambda _=None: self.mark_profile_dirty())
            diagnostics = group('診断ログ', section='diagnostics', detail=True)
            self.log = QPlainTextEdit()
            self.log.setReadOnly(True)
            self.log.setMaximumHeight(150)
            diagnostics.addRow(self.log)
            section_order = {
                'home': ('games', 'game_details', 'current_dialogue', 'advance', 'advance_details',
                         'target_details', 'audio_details', 'common_advance', 'common_audio', 'diagnostics'),
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
            if self.irodori_url.text().strip():
                QTimer.singleShot(0, self.start_server)

        def note(self, message):
            self.log.appendPlainText(message)
            self._last_note = message
            if 'diagnostics' in self.section_summaries:
                compact = message.replace('\n', ' ')
                self.section_summaries['diagnostics'].setText('最新: ' + compact[:180])
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
                if key == 'advance_mode':
                    values[key] = AdvanceMode.AUDIO if self.common_advance_audio_button.isChecked() else AdvanceMode.TEXT
                elif isinstance(widget, QComboBox):
                    values[key] = widget.currentText().strip()
                elif isinstance(widget, QCheckBox):
                    values[key] = widget.isChecked()
                elif isinstance(widget, QSpinBox):
                    values[key] = widget.value()
                else:
                    values[key] = widget.text().strip()
            previous = copy.deepcopy(self.catalog.common)
            try:
                common = CommonSettings(**values)
                common.validate()
                self.catalog.common = common
                try:
                    self.persist()
                except Exception:
                    self.catalog.common = previous
                    raise
                profile = self.current_profile()
                if profile:
                    for key in profile.inherit_common:
                        if key in self.inherit_controls:
                            self.refresh_inherited_control(key)
                    if self.controller.profile and self.controller.profile.id == profile.id:
                        effective = self.catalog.effective_profile(profile)
                        if self.runtime_capture_matches(effective):
                            self.controller.stage_profile(effective)
                            self.note('共通設定を保存しました。現在の台詞は維持し、次の台詞から反映します。')
                        else:
                            self.defer_capture_transition()
                            self.note('共通設定を保存しました。保留中の取得設定と合わせて次の台詞境界で反映します。')
                    else:
                        self.note('共通設定を保存しました。次の監視開始から反映します。')
                else:
                    self.note('共通設定を保存しました。')
                self.refresh_section_summaries()
            except Exception as exc:
                self.note('共通設定を保存できません: ' + str(exc))

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

        def runtime_capture_matches(self, profile: Profile) -> bool:
            """Whether profile edits can be staged without rebuilding capture."""
            active = self.controller.profile
            if not active or active.id != profile.id:
                return False
            return (active.source == profile.source and active.hooks == profile.hooks and
                    active.speaker_links == profile.speaker_links and
                    not (profile.source == 'ocr' and active.ocr != profile.ocr))

        def defer_capture_transition(self):
            """Keep the current Utterance intact and replace capture at a proven boundary."""
            self.controller.pause('settings_changed')
            self._pending_monitor_apply = True
            self.refresh_status()
            if self.controller.utterance is None or self.controller.sent:
                QTimer.singleShot(0, self.apply_pending_monitor_transition)

        def pending_hook_boundary(self, event: HookEvent, profile: Profile | None) -> bool:
            """Whether one new HOOK output proves the boundary needed for a pending capture swap."""
            if not self._pending_monitor_apply or not profile or profile.source != 'hook':
                return False
            if event.kind != 'output' or event.source is None or not event.text.strip():
                return False
            selected = next((h for h in profile.hooks if h.role == 'text' and h.matches(event.source)), None)
            if selected is None:
                return False
            current = self.controller.utterance
            return current is None or self.controller.sent or event.text != current.raw

        def choose_game(self, *_):
            self._resume_monitor_requested = False
            self.stop_session()
            self._host_epoch += 1
            if self.hook_host:
                self.hook_host.close()
                self.hook_host = None
            self.host_generation = ''
            self.connection_generation = 0
            self.attached_pid = 0
            self._connecting_pid = 0
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

        def current_source_mode(self) -> str:
            return 'ocr' if self.source_ocr_button.isChecked() else 'hook'

        def set_source_mode(self, source: str):
            button = self.source_ocr_button if source == 'ocr' else self.source_hook_button
            blocked = [(b, b.blockSignals(True)) for b in (self.source_hook_button, self.source_ocr_button)]
            try:
                button.setChecked(True)
            finally:
                for b, previous in blocked:
                    b.blockSignals(previous)

        def current_advance_mode(self) -> AdvanceMode:
            return (AdvanceMode.AUDIO if self.advance_audio_button.isChecked() else AdvanceMode.TEXT)

        def set_advance_mode(self, mode: AdvanceMode):
            button = self.advance_audio_button if mode == AdvanceMode.AUDIO else self.advance_text_button
            blocked = [(b, b.blockSignals(True)) for b in (self.advance_text_button, self.advance_audio_button)]
            try:
                button.setChecked(True)
            finally:
                for b, previous in blocked:
                    b.blockSignals(previous)
            self.update_audio_environment_summary()

        def set_advance_authorization(self, enabled: bool):
            if enabled:
                self.permit()
            else:
                self.pause()

        def update_audio_environment_summary(self):
            mode = self.current_advance_mode() if hasattr(self, 'advance_audio_button') else AdvanceMode.TEXT
            helper = self.native_helper.text().strip() if hasattr(self, 'native_helper') else ''
            model = self.asr_model.text().strip() if hasattr(self, 'asr_model') else ''
            tokens = self.asr_tokens.text().strip() if hasattr(self, 'asr_tokens') else ''
            if mode == AdvanceMode.TEXT:
                text = '音声判断環境: この方式では使用しません'
            elif helper and model and tokens:
                text = '音声判断環境: 準備済み'
            else:
                missing = []
                if not helper: missing.append('捕捉ヘルパー')
                if not model: missing.append('ASRモデル')
                if not tokens: missing.append('語彙')
                text = '音声判断環境: 未準備 — ' + ' / '.join(missing)
            if hasattr(self, 'audio_environment_summary'):
                self.audio_environment_summary.setText(text)


        def mark_profile_dirty(self):
            if self._loading_profile or not self.current_profile():
                return
            self._profile_dirty = True
            if hasattr(self, 'profile_save_state'):
                self.profile_save_state.setText('未保存の変更あり')
            self.refresh_section_summaries()

        def profile_setting_changed(self, key: str):
            if self._loading_profile:
                return
            self.mark_profile_dirty()
            if key in self.inherit_controls:
                self.refresh_inherited_control(key)
            else:
                self.update_audio_environment_summary()
            self.refresh_section_summaries()


        def profile_control_value(self, key: str):
            if key == 'advance_mode':
                return self.current_advance_mode()
            control = self.profile_field_controls[key]
            if isinstance(control, QCheckBox):
                return control.isChecked()
            if isinstance(control, QSpinBox):
                return control.value()
            if isinstance(control, QComboBox):
                return control.currentText().strip()
            return control.text().strip()

        def set_profile_control_value(self, key: str, value):
            if key == 'advance_mode':
                self.set_advance_mode(value)
                return
            control = self.profile_field_controls[key]
            previous = control.blockSignals(True)
            try:
                if isinstance(control, QCheckBox):
                    control.setChecked(bool(value))
                elif isinstance(control, QSpinBox):
                    control.setValue(int(value))
                elif isinstance(control, QComboBox):
                    control.setEditText(str(value))
                else:
                    control.setText(str(value))
            finally:
                control.blockSignals(previous)

        def inheritance_changed(self, key: str, inherited: bool):
            if key not in self.profile_field_controls:
                return
            profile = self.current_profile()
            if not self._loading_profile and profile:
                if inherited:
                    self._override_drafts[key] = self.profile_control_value(key)
                else:
                    self.set_profile_control_value(
                        key, self._override_drafts.get(key, getattr(profile, key)))
            self.refresh_inherited_control(key)
            if not self._loading_profile:
                self.mark_profile_dirty()

        @staticmethod
        def _short_path(value: str) -> str:
            value = (value or '').strip()
            if not value:
                return '未設定'
            return ntpath.basename(value.rstrip('/\\')) or value

        def refresh_section_summaries(self):
            if not getattr(self, 'section_summaries', None):
                return
            profile = self.current_profile()
            game = self.catalog.games.get(self.game_id)
            summaries = {}
            summaries['irodori'] = (
                f'接続先 {self.irodori_url.text().strip() or "未設定"} / '
                f'{self.server_status.text() if hasattr(self, "server_status") else "未接続"}')
            if game and profile:
                summaries['game_details'] = (
                    f'実行 {self._short_path(self.game_exe.text())} / '
                    f'音声 {self._short_path(self.audio_exe.text())} / '
                    f'入力 {self._short_path(self.input_exe.text())}')
                summaries['target_details'] = (
                    f'HOOK PID {self.pid.value() or "未選択"} / '
                    f'音声 PID {self.audio_pid.value() or self.pid.value() or "自動"} / '
                    f'入力 PID {self.input_pid.value() or self.pid.value() or "自動"}')
                summaries['hook_details'] = (
                    f'flush {self.hook_flush.value()} ms / codepage {self.hook_codepage.value()} / '
                    f'履歴 {self.hook_history.value()} / buffer {self.hook_buffer.value()}')
                summaries['ocr_details'] = (
                    f'{self._short_path(self.ocr_exe.text())} / {self.ocr_lang.text().strip() or "言語未設定"} / '
                    f'安定 {self.ocr_stability.value()}回')
                summaries['audio_details'] = self.audio_environment_summary.text()
                summaries['advance_details'] = (
                    f'送りキー {self.input_key.value()} / 次台詞待ち {self.next_line_timeout.value()} ms / '
                    f'重複 {self.repeat.currentText()}')
                summaries['extraction'] = f'抽出規則 {len(profile.structure_rules)}件'
                summaries['replacement'] = f'置換規則 {len(profile.replacements)}件'
            common = self.catalog.common
            common_mode = '音声判断' if common.advance_mode == AdvanceMode.AUDIO else '文字のみ判断'
            summaries['common_advance'] = (
                f'{common_mode} / 1文字 {common.per_character_wait_ms} ms / '
                f'最低表示 {common.reading_ms} ms / 不確実時フォールバック '
                f'{"ON" if common.uncertain_text_fallback else "OFF"}')
            summaries['common_audio'] = (
                f'helper {self._short_path(common.native_helper)} / '
                f'ASR {self._short_path(common.asr_model)} / tokens {self._short_path(common.asr_tokens)}')
            summaries['diagnostics'] = ('最新: ' + self._last_note.replace('\n', ' ')[:180]
                                        if getattr(self, '_last_note', '') else '問題があるときだけ開いて詳細ログを確認できます')
            for key, label in self.section_summaries.items():
                label.setText(summaries.get(key, '現在値は上の基本設定から参照できます'))

        def refresh_inherited_control(self, key: str):
            """Show the effective value and its owner beside the setting it affects."""
            checkbox = self.inherit_controls[key]
            control = self.profile_field_controls[key]
            inherited = checkbox.isChecked()
            source = self.inherit_source_labels.get(key)
            if key == 'advance_mode':
                for button in (self.advance_text_button, self.advance_audio_button):
                    button.setEnabled(not inherited)
                if inherited:
                    self.set_advance_mode(self.catalog.common.advance_mode)
                value_text = '音声判断' if self.current_advance_mode() == AdvanceMode.AUDIO else '文字のみ判断'
            else:
                control.setEnabled(not inherited)
                if inherited:
                    value = getattr(self.catalog.common, key)
                    was_blocked = control.blockSignals(True)
                    try:
                        if isinstance(control, QCheckBox):
                            control.setChecked(value)
                        elif isinstance(control, QSpinBox):
                            control.setValue(value)
                        elif isinstance(control, QComboBox):
                            control.setEditText(value)
                        else:
                            control.setText(value)
                    finally:
                        control.blockSignals(was_blocked)
                if isinstance(control, QCheckBox):
                    value_text = 'ON' if control.isChecked() else 'OFF'
                elif isinstance(control, QSpinBox):
                    value_text = str(control.value())
                elif isinstance(control, QComboBox):
                    value_text = control.currentText()
                else:
                    value_text = control.text()
            if source is not None:
                source.setText(('共通: ' if inherited else '作品: ') + value_text)
            self.update_audio_environment_summary()
            self.refresh_rule_preview()

        def choose_profile(self, *_):
            self._resume_monitor_requested = False
            self.stop_session()
            self.profile_id = self.profile_choice.currentData() or ''
            profile = self.current_profile()
            self._loading_profile = True
            if not profile:
                self.context_banner.setText('編集対象: 作品とプロファイルを選択してください')
                for table in (self.speaker_table, self.structure_table,
                              self.replacement_table, self.reading_table):
                    table.setRowCount(0)
                self._loading_profile = False
                self._profile_dirty = False
                if hasattr(self, 'profile_save_state'):
                    self.profile_save_state.setText('作品を選択してください')
                self.refresh_rule_preview()
                self.refresh_section_summaries()
                return
            game = self.catalog.games[self.game_id]
            self.context_banner.setText(f'編集対象: {game.name} / {profile.name}  ―  この作品の設定を編集中')
            effective = self.catalog.effective_profile(profile)
            for key, widget in self.inherit_controls.items():
                widget.setChecked(key in profile.inherit_common)
            self.default_tts.setChecked(effective.default_tts)
            self.set_advance_mode(effective.advance_mode)
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
            self.set_source_mode(profile.source)
            self.ocr_exe.setText(profile.ocr.executable)
            self.ocr_lang.setText(profile.ocr.language)
            self.ocr_region.setText(','.join(map(str, profile.ocr.region)))
            self.ocr_stability.setValue(profile.ocr.stable_samples)
            self.native_helper.setText(effective.native_helper)
            self.asr_model.setText(effective.asr_model)
            self.asr_tokens.setText(effective.asr_tokens)
            self.next_line_timeout.setValue(effective.next_line_timeout_ms)
            self._override_drafts = {field: getattr(profile, field) for field in COMMON_FIELDS}
            for field in COMMON_FIELDS:
                self.refresh_inherited_control(field)
            self.refresh_rules()
            self.refresh_selected_hooks()
            self.refresh_speaker_choices()
            # Editors must never carry the previous game's unsaved draft.
            self.clear_structure_editor()
            self.clear_replacement_editor()
            self._preview_edit_kind = None
            self.refresh_rule_preview()
            self._loading_profile = False
            self._profile_dirty = False
            if hasattr(self, 'profile_save_state'):
                self.profile_save_state.setText('保存済み')
            self.refresh_section_summaries()

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
                self.note('先に作品を登録してください')
                return False
            try:
                updated = copy.deepcopy(p)
                from .model import OCRSettings
                updated.source = self.current_source_mode()
                updated.ocr = OCRSettings(self.ocr_exe.text().strip(), self.ocr_lang.text().strip(),
                    tuple(int(x.strip()) for x in self.ocr_region.text().split(',')),
                    stable_samples=self.ocr_stability.value())
                updated.native_helper = self.native_helper.text().strip()
                updated.asr_model = self.asr_model.text().strip()
                updated.asr_tokens = self.asr_tokens.text().strip()
                updated.next_line_timeout_ms = self.next_line_timeout.value()
                updated.default_tts = self.default_tts.isChecked()
                updated.advance_mode = self.current_advance_mode()
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
                    setattr(updated, field, self._override_drafts.get(field, getattr(p, field)))
                updated.validate()
                game = self.catalog.games[self.game_id]
                previous_executables = (game.executable, game.audio_executable, game.input_executable)
                game.profiles[updated.id] = updated
                game.executable = self.game_exe.text().strip()
                game.audio_executable = self.audio_exe.text().strip()
                game.input_executable = self.input_exe.text().strip()
                try:
                    self.persist()
                except Exception:
                    game.profiles[p.id] = p
                    game.executable, game.audio_executable, game.input_executable = previous_executables
                    raise
                self._profile_dirty = False
                if hasattr(self, 'profile_save_state'):
                    self.profile_save_state.setText('保存済み')
                self.refresh_section_summaries()
                active = self.controller.profile
                requires_restart = bool(active and active.id == updated.id and
                                        not self.runtime_capture_matches(self.catalog.effective_profile(updated)))
                if (self.attached_pid and self.host_generation and self.hook_host and
                        updated.source == 'hook' and
                        (updated.hook_flush_ms, updated.hook_codepage, updated.hook_max_buffer, updated.hook_max_history) !=
                        (p.hook_flush_ms, p.hook_codepage, p.hook_max_buffer, p.hook_max_history)):
                    try:
                        self.hook_host.command('settings', flush_ms=updated.hook_flush_ms,
                            codepage=updated.hook_codepage, max_buffer=updated.hook_max_buffer,
                            max_history=updated.hook_max_history)
                    except Exception as exc:
                        self.note('作品設定は保存しましたが、現在のLunaHostへ取得条件を反映できませんでした: ' + str(exc))
                if active and active.id == updated.id:
                    if requires_restart:
                        self.defer_capture_transition()
                        self.note('作品設定を保存しました。現在の台詞を維持し、次の台詞境界で取得経路を切り替えます。')
                    else:
                        self.controller.stage_profile(self.catalog.effective_profile(updated))
                        self.note('作品設定を保存しました。現在の台詞は維持し、次の台詞から適用します。')
                else:
                    self.note('作品設定を保存しました。次の監視開始から適用します。')
                return True
            except Exception as exc:
                self.note('作品設定を保存できません: ' + str(exc))
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
                if self.controller.profile and self.controller.profile.id == updated.id:
                    effective = self.catalog.effective_profile(updated)
                    if self.runtime_capture_matches(effective):
                        self.controller.stage_profile(effective)
                    else:
                        self.defer_capture_transition()
                self.note(message + '。現在の台詞は固定し、次の台詞から適用します。')
                return True
            except Exception as exc:
                self.note('設定を保存できません: ' + str(exc))
                return False

        def add_speaker_inline(self):
            name = self.new_speaker_name.text().strip()
            if not name:
                self.note('話者名を入力してください')
                return
            p = self.current_profile()
            if p and (name in p.speaker_tts or name in p.speaker_voices):
                self.note('同名の話者が既にあります。該当行を直接編集してください')
                return
            enabled = self.new_speaker_tts.isChecked()
            voice = self.new_speaker_voice.currentText().strip()
            def change(profile):
                profile.speaker_tts[name] = enabled
                profile.speaker_voices[name] = voice
            if self.commit_rule_edit(change, '話者設定を追加しました'):
                self.new_speaker_name.clear()
                self.new_speaker_voice.setEditText('')
                self.refresh_speaker_choices(name)

        def speaker_row_item_changed(self, item):
            if getattr(self, '_refreshing_speakers', False):
                return
            row = item.row()
            if row < 0 or row >= self.speaker_table.rowCount():
                return
            name_item = self.speaker_table.item(row, 0)
            if name_item is None:
                return
            name = name_item.text().strip()
            old = name_item.data(Qt.ItemDataRole.UserRole) or name
            p = self.current_profile()
            if not p or not name:
                self.refresh_speaker_choices(old if p else None)
                return
            if item.column() == 0 and name != old:
                existing = set(p.speaker_tts) | set(p.speaker_voices)
                if name in existing:
                    self.note('同名の話者が既にあります')
                    self.refresh_speaker_choices(old)
                    return
                def rename(profile):
                    tts = profile.speaker_tts.pop(old, profile.default_tts)
                    voice = profile.speaker_voices.pop(old, '')
                    profile.speaker_tts[name] = tts
                    profile.speaker_voices[name] = voice
                if self.commit_rule_edit(rename, '話者名を変更しました'):
                    self.refresh_speaker_choices(name)
                else:
                    self.refresh_speaker_choices(old)
            elif item.column() == 1:
                enabled = item.checkState() == Qt.CheckState.Checked
                if self.commit_rule_edit(lambda profile: profile.speaker_tts.__setitem__(name, enabled),
                                         '話者のTTS設定を変更しました'):
                    self.refresh_speaker_choices(name)

        def speaker_voice_changed(self, name: str, voice: str):
            if getattr(self, '_refreshing_speakers', False):
                return
            p = self.current_profile()
            if not p or name not in set(p.speaker_tts) | set(p.speaker_voices):
                return
            if p.speaker_voices.get(name, '') == voice.strip():
                return
            if self.commit_rule_edit(lambda profile: profile.speaker_voices.__setitem__(name, voice.strip()),
                                     '話者のVoiceを変更しました'):
                self.refresh_speaker_choices(name)

        def delete_speaker_name(self, name: str):
            p = self.current_profile()
            if not p:
                return
            def change(profile):
                profile.speaker_tts.pop(name, None)
                profile.speaker_voices.pop(name, None)
            if self.commit_rule_edit(change, '話者を削除しました'):
                self.refresh_speaker_choices()

        def audition_voice_id(self, voice: str):
            voice = voice.strip() or self.default_voice.currentText().strip()
            try:
                if not voice:
                    raise ValueError('Voice IDを選択してください')
                self.preview_jobs.start(Token(-1, -1), self.catalog.irodori_url,
                                        self.audition_text.text(), voice)
            except Exception as exc:
                self.note('試聴できません: ' + str(exc))

        def refresh_speaker_choices(self, selected=None):
            p = self.current_profile()
            names = sorted(set(p.speaker_tts) | set(p.speaker_voices)) if p else []
            effective = self.catalog.effective_profile(p) if p else None
            self._refreshing_speakers = True
            self.speaker_table.blockSignals(True)
            try:
                self.speaker_table.setRowCount(len(names))
                for row, name in enumerate(names):
                    name_item = QTableWidgetItem(name)
                    name_item.setData(Qt.ItemDataRole.UserRole, name)
                    self.speaker_table.setItem(row, 0, name_item)
                    tts_item = QTableWidgetItem('')
                    tts_item.setFlags(tts_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    tts_item.setCheckState(Qt.CheckState.Checked if p.speaker_tts.get(name, effective.default_tts)
                                           else Qt.CheckState.Unchecked)
                    self.speaker_table.setItem(row, 1, tts_item)
                    voice = QComboBox()
                    voice.setEditable(True)
                    choices = [self.default_voice.itemText(i) for i in range(self.default_voice.count())]
                    current = p.speaker_voices.get(name, '')
                    for value in choices:
                        if value and voice.findText(value) < 0:
                            voice.addItem(value)
                    voice.setEditText(current)
                    voice.currentTextChanged.connect(lambda value, n=name: self.speaker_voice_changed(n, value))
                    self.speaker_table.setCellWidget(row, 2, voice)
                    audition = QPushButton('試聴')
                    audition.clicked.connect(lambda _=False, c=voice: self.audition_voice_id(c.currentText()))
                    self.speaker_table.setCellWidget(row, 3, audition)
                    remove = QPushButton('削除')
                    remove.clicked.connect(lambda _=False, n=name: self.delete_speaker_name(n))
                    self.speaker_table.setCellWidget(row, 4, remove)
                self.speaker_table.clearSelection()
                if selected in names:
                    self.speaker_table.selectRow(names.index(selected))
            finally:
                self.speaker_table.blockSignals(False)
                self._refreshing_speakers = False
            self.refresh_rule_preview()

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
            self.refresh_reading_table()
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

        def reading_row_item_changed(self, item):
            if getattr(self, '_refreshing_readings', False):
                return
            row = item.row()
            word_item = self.reading_table.item(row, 0)
            reading_item = self.reading_table.item(row, 1)
            if not word_item or not reading_item:
                return
            word = word_item.text().strip()
            reading = reading_item.text().strip()
            old = word_item.data(Qt.ItemDataRole.UserRole) or word
            p = self.current_profile()
            if not p or not word or not reading:
                self.refresh_reading_table(old if p else None)
                return
            if word != old and word in p.name_readings:
                self.note('同じ表記が既にあります')
                self.refresh_reading_table(old)
                return
            def change(profile):
                if old != word:
                    profile.name_readings.pop(old, None)
                profile.name_readings[word] = reading
            if self.commit_rule_edit(change, '読みを変更しました'):
                self.refresh_reading_table(word)
            else:
                self.refresh_reading_table(old)

        def add_reading_inline(self):
            word = self.new_reading_word.text().strip()
            reading = self.new_reading_value.text().strip()
            if not word or not reading:
                self.note('表記と読みの両方を入力してください')
                return
            p = self.current_profile()
            if p and word in p.name_readings:
                self.note('同じ表記が既にあります。該当行を直接編集してください')
                return
            if self.commit_rule_edit(lambda profile: profile.name_readings.__setitem__(word, reading),
                                     '読みを追加しました'):
                self.new_reading_word.clear()
                self.new_reading_value.clear()
                self.refresh_reading_table(word)

        def delete_reading_word(self, word: str):
            if self.current_profile() and self.commit_rule_edit(
                    lambda profile: profile.name_readings.pop(word, None), '読みを削除しました'):
                self.refresh_reading_table()

        def refresh_reading_table(self, selected=None):
            p = self.current_profile()
            words = list(p.name_readings) if p else []
            self._refreshing_readings = True
            self.reading_table.blockSignals(True)
            try:
                self.reading_table.setRowCount(len(words))
                for row, word in enumerate(words):
                    word_item = QTableWidgetItem(word)
                    word_item.setData(Qt.ItemDataRole.UserRole, word)
                    self.reading_table.setItem(row, 0, word_item)
                    self.reading_table.setItem(row, 1, QTableWidgetItem(p.name_readings[word]))
                    remove = QPushButton('削除')
                    remove.clicked.connect(lambda _=False, w=word: self.delete_reading_word(w))
                    self.reading_table.setCellWidget(row, 2, remove)
                self.reading_table.clearSelection()
                if selected in words:
                    self.reading_table.selectRow(words.index(selected))
            finally:
                self.reading_table.blockSignals(False)
                self._refreshing_readings = False
            self.refresh_rule_preview()

        def audition_voice(self):
            self.audition_voice_id(self.default_voice.currentText())

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
            self.refresh_rule_preview()

        def refresh_rule_preview(self, *_):
            if not hasattr(self, 'example_raw') or not hasattr(self, 'editor_preview'):
                return
            p = self.current_profile()
            if p is None:
                self.editor_preview.setPlainText('作品を選択してください。')
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
                self._preview_utterance = u
                self.btn_edit_detected_speaker.setEnabled(bool(u.speaker))
                self.preview_state.setText('保存済み設定の結果' if kind is None else
                    '編集中の複雑な規則を仮適用しています。保存すると次の台詞から反映します。')
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
            self.tabs.setCurrentIndex(self.text_tab_index)
            self.page_scrollers['tune'].ensureWidgetVisible(self.ui_sections['speakers'])
            for row in range(self.speaker_table.rowCount()):
                if self.speaker_table.item(row, 0).text() == u.speaker:
                    self.speaker_table.selectRow(row)
                    self.speaker_table.scrollToItem(self.speaker_table.item(row, 0))
                    return
            self.new_speaker_name.setText(u.speaker)
            self.new_speaker_name.setFocus()

        def edit_preview_reading(self):
            chosen = self.example_raw.textCursor().selectedText().strip()
            self.show_task_section('reading')
            if chosen:
                p = self.current_profile()
                if p and chosen in p.name_readings:
                    self.refresh_reading_table(chosen)
                else:
                    self.new_reading_word.setText(chosen)
                    self.new_reading_value.setFocus()


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
                    'このプロファイルと設定を削除しますか？') == QMessageBox.StandardButton.Yes:
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
                self.note('既定プロファイルを保存しました')

        def import_luna(self):
            path, _ = QFileDialog.getOpenFileName(self, 'Lunaのsavegamedata JSONを選択', '', 'JSON (*.json)')
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
                if QMessageBox.question(self, 'Luna設定の取り込み確認',
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
            path, _ = QFileDialog.getOpenFileName(self, '旧AutoAdv v2設定を選択', '', 'JSON (*.json)')
            if not path:
                return
            try:
                entries = inspect_legacy(Path(path))
                proposed = add_legacy_games(self.catalog, entries)
                summary = '\n'.join(f'{row.game.name}: {len(row.game.profiles)} profiles' +
                                    (f'\n  Requires manual review: {"; ".join(row.unmapped)}' if row.unmapped else '')
                                    for row in entries)
                if QMessageBox.question(self, '旧AutoAdv設定の取り込み確認',
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
                self.note('接続が完了していません。接続状態と診断を確認してください。')
                return
            try:
                game = self.catalog.games.get(self.game_id)
                module = Path(game.executable).name if game and game.executable else ''
                # Search is an alternate way to *acquire the same dialogue*, not
                # another UI category. Limit upstream records so automatic trials
                # cannot flood the target with unlimited inserts.
                self.hook_host.command('find_hooks', pid=self.pid.value(), boundary_module=module,
                                       parameters={'maxRecords': 64})
                self.note('追加の取得元を探索しています。台詞を表示すると、取得できた本文が同じ一覧に現れます。')
            except Exception as exc:
                self.note('HOOK探索を開始できません: ' + str(exc))

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
            self.server_status.setText('Irodori: 接続中…')
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
                self.refresh_section_summaries()
                return
            self.server_status.setText(f'Irodori: {result.status} ({result.origin}); {result.message}')
            self.overview_voice.setText('音声: ' + ('接続中' if result.status == 'connected' else result.status))
            if result.status == 'connected':
                for control in (self.default_voice, self.new_speaker_voice):
                    existing = control.currentText()
                    blocked = control.blockSignals(True)
                    try:
                        control.clear()
                        control.addItems(list(result.voices))
                        control.setEditText(existing)
                    finally:
                        control.blockSignals(blocked)
                self.refresh_speaker_choices()
            self.refresh_section_summaries()

        def start_host(self):
            self.stop_session()
            self.persist()
            # Signals from the old Host can arrive after a new process was
            # selected. They must not reset or populate the current session.
            self._host_epoch += 1
            epoch = self._host_epoch
            if self.hook_host:
                self.hook_host.close()
            self.host_generation = ''
            self.connection_generation = 0
            self.attached_pid = 0
            self._connecting_pid = 0
            self.clear_hook_observations()
            try:
                runtime = adopted_runtime()
                self.hook_host = HookHost(runtime.host,
                    lambda event: self.bridge.hook.emit(epoch, event),
                    lambda reason: self.bridge.closed.emit(epoch, reason),
                    lambda message: self.bridge.host_error.emit(epoch, message))
                self.hook_host.start()
                self.host_status.setText('LunaHook Host: 起動済み')
            except Exception as exc:
                self.hook_host = None
                self.host_status.setText(f'LunaHook Host: {exc}')

        def launch_game(self):
            game = self.catalog.games.get(self.game_id)
            if not game or not game.executable.strip():
                self.note('ゲーム実行ファイルを登録してください')
                return
            try:
                target = Path(game.executable)
                if not target.is_file():
                    raise FileNotFoundError(target)
                process = subprocess.Popen([str(target)], cwd=str(target.parent))
                self._launched_game_pid = process.pid
                self._launch_watch_executable = str(target)
                self._launch_watch_attempts = 0
                if getattr(self, '_launch_watch_timer', None) is None:
                    self._launch_watch_timer = QTimer(self)
                    self._launch_watch_timer.setInterval(300)
                    self._launch_watch_timer.timeout.connect(self.poll_launched_game)
                self._launch_watch_timer.start()
                self.note('ゲームを起動しました。対象ウィンドウを自動識別して接続します。')
            except Exception as exc:
                self.note('ゲームを起動できません: ' + str(exc))

        def poll_launched_game(self):
            executable = getattr(self, '_launch_watch_executable', '')
            if not executable:
                return
            self._launch_watch_attempts = getattr(self, '_launch_watch_attempts', 0) + 1
            try:
                windows = find_game_windows(executable)
            except Exception as exc:
                if self._launch_watch_attempts >= 50:
                    self._launch_watch_timer.stop()
                    self.note('起動後のウィンドウ識別に失敗しました: ' + str(exc))
                return
            direct = [w for w in windows if w.pid == getattr(self, '_launched_game_pid', 0)]
            chosen = direct[0] if len(direct) == 1 else (windows[0] if len(windows) == 1 else None)
            if chosen is not None:
                self._launch_watch_timer.stop()
                self.use_running_target(chosen)
                return
            if self._launch_watch_attempts >= 50:
                self._launch_watch_timer.stop()
                if windows:
                    self.note('起動した作品に複数の候補があります。実行中ゲーム一覧から対象を選択してください。')
                else:
                    self.note('起動したゲームのウィンドウを自動識別できませんでした。実行中ゲーム一覧から選択してください。')


        def pick_running_game(self):
            """A first-use flow independent of saved EXE, PID or known HOOK code."""
            target = choose_running_target(self)
            if target is None:
                return
            self.use_running_target(target)

        def use_running_target(self, target: WindowCandidate):
            if not target.executable or target.pid <= 0:
                raise ValueError('実行ファイルとPIDを確認できる対象が必要です')
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
            if not target.hwnd:
                self.note(f'PID {target.pid}: 自動送りにはゲームウィンドウの指定が必要です。')
            self.tabs.setCurrentIndex(1)
            self.connect_game()

        def detect_game(self):
            game = self.catalog.games.get(self.game_id)
            if not game:
                return
            try:
                windows = find_game_windows(game.executable)
                if not windows:
                    self.note('保存済み実行ファイルに一致するウィンドウがありません。実行中ゲームを選択するか起動してください')
                    return
                options = [f'{w.title or "(untitled)"} — PID {w.pid}, HWND {hex(w.hwnd)}'
                           for w in windows]
                if len(windows) == 1:
                    index = 0
                else:
                    selected, accepted = QInputDialog.getItem(self, 'ゲームウィンドウを選択',
                        '同じ実行ファイルのウィンドウが複数あります。台詞を表示するウィンドウを選択してください', options, 0, False)
                    if not accepted:
                        return
                    index = options.index(selected)
                # Re-selecting an already attached window is a no-op; changing
                # PID is handled by connect_game's single Host transition.
                self.pid.setValue(windows[index].pid)
                self.hwnd.setText(hex(windows[index].hwnd))
                self.hook_target.setText(options[index])
                self.connect_game()
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
                    self.note('保存済み実行ファイルに一致する実行中ゲームがありません')
                    return
                labels = [f'{self.catalog.games[gid].name}: {window.title or "(untitled)"} '
                          f'PID {window.pid}, HWND {hex(window.hwnd)}' for gid, window in candidates]
                if len(labels) > 1:
                    choice, ok = QInputDialog.getItem(self, '登録済みゲームを選択',
                        '候補が複数あります。対象ゲームと台詞ウィンドウを選択してください', labels, 0, False)
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
                self.connect_game()
            except Exception as exc:
                self.note('Registered-game discovery failed: ' + str(exc))

        def detect_associated_target(self, role: str):
            game = self.catalog.games.get(self.game_id)
            if not game:
                return
            executable = self.audio_exe.text().strip() if role == 'audio' else self.input_exe.text().strip()
            if not executable:
                self.note('別プロセスを使用する場合は、作品設定で実行ファイルを指定してください')
                return
            try:
                candidates = (find_game_processes(executable) if role == 'audio'
                              else find_game_windows(executable))
                if not candidates:
                    self.note('一致するプロセスまたはウィンドウを検出できませんでした')
                    return
                choices = ([f'PID {pid} — {executable}' for pid in candidates] if role == 'audio'
                           else [f'{w.title or "(untitled)"} — PID {w.pid}, HWND {hex(w.hwnd)}'
                                 for w in candidates])
                if len(candidates) > 1:
                    choice, accepted = QInputDialog.getItem(self, 'Choose ' + role + ' process',
                                                           '一致する候補', choices, 0, False)
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
                self.note(('音声' if role == 'audio' else '入力') + '対象を選択しました: ' + choices[index])
            except Exception as exc:
                self.note('対象プロセスの検出に失敗しました: ' + str(exc))

        def session_target(self) -> Target:
            input_pid = self.input_pid.value() or self.pid.value()
            hook_pid = self.pid.value() or input_pid
            return Target(input_pid,
                          int(self.hwnd.text().strip(), 0),
                          hook_pid=hook_pid, audio_pid=self.audio_pid.value() or hook_pid)


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
                updated.source = self.current_source_mode()
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
            profile = self.current_profile()
            if not profile:
                self.note('作品を登録してください')
                return
            self._resume_monitor_requested = bool(profile.source == 'hook' and any(h.role == 'text' for h in profile.hooks))
            if profile.source == 'ocr':
                self.host_status.setText('取得方法: OCR（LunaHook接続は不要）')
                if self.hwnd.text().strip():
                    QTimer.singleShot(0, lambda: self.begin_monitor(save_draft=False))
                return
            if (self.hook_host and self.hook_host.child is not None and
                    self.hook_host.child.poll() is None and
                    (self.attached_pid == self.pid.value() and self.host_generation or
                     self._connecting_pid == self.pid.value())):
                if self.attached_pid == self.pid.value() and self.host_generation:
                    self.start_saved_monitor_when_ready()
                return
            # The capture settings alone are saved at connection time. Neither
            # voice nor advancement drafts are committed or auto-authorized.
            if not self.save_acquisition_settings():
                return
            if (not self.hook_host or self.hook_host.child is None or
                    self.hook_host.child.poll() is not None or
                    (self.attached_pid and self.attached_pid != self.pid.value()) or
                    (self._connecting_pid and self._connecting_pid != self.pid.value())):
                self.start_host()
            if not self.hook_host or self.hook_host.child is None:
                self.note('LunaHook Hostの起動に失敗しました')
                return
            try:
                p = self.current_profile()
                if p is None:
                    raise ValueError('作品とプロファイルを選択してください')
                if self.attached_pid == self.pid.value() and self.host_generation:
                    self.note('このゲームには既にLunaHookが接続されています。')
                    return
                self.attached_pid = 0
                self.host_generation = ''
                self.connection_generation = 0
                self.hook_host.command('settings', flush_ms=p.hook_flush_ms,
                    codepage=p.hook_codepage, max_buffer=p.hook_max_buffer, max_history=p.hook_max_history)
                self.hook_host.command('connect', pid=self.pid.value())
                self._connecting_pid = self.pid.value()
                self.host_status.setText('LunaHook: 接続中…')
                self.note('ゲームを選択しました。LunaHook接続を開始しています。')
            except Exception as exc:
                self._connecting_pid = 0
                self.note('LunaHook接続失敗: ' + str(exc))

        def clear_hook_observations(self):
            self.sources.clear()
            self.source_samples.clear()
            self.ambiguous_sources.clear()
            self.source_rows.clear()
            self._searched_codes.clear()
            self._trial_hook_codes.clear()
            self.source_table.setRowCount(0)
            self.hook_preview.clear()
            self.hook_technical_info.setText('取得元を選択すると識別情報を表示します')
            self.refresh_candidate_actions()
            self.hook_capture_status.setText('取得: 未接続または本文未受信')
            self.refresh_hook_connection()

        def source_from_candidate_row(self, row: int):
            if row < 0 or row >= self.source_table.rowCount():
                return None
            item = self.source_table.item(row, 4)
            identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if not isinstance(identity, (tuple, list)) or len(identity) != 2:
                return None
            key, address = identity
            source = self.sources.get(key)
            return source if source and source.address == address and key not in self.ambiguous_sources else None

        def chosen_source(self):
            return self.source_from_candidate_row(self.source_table.currentRow())

        def remove_candidate_row(self, row: int):
            self.source_table.removeRow(row)
            self.source_rows = {key: index - (index > row) for key, index in self.source_rows.items()
                                if index != row}

        def try_discovered_hook(self, code: str):
            """Trial a discovered Luna code; only real output can become a visible row."""
            if not code or code in self._searched_codes:
                return
            self._searched_codes.add(code)
            if any(src.hook_code == code for src in self.sources.values()):
                return
            if (not self.hook_host or not self.host_generation or
                    self.attached_pid != self.pid.value()):
                return
            try:
                self.hook_host.command('insert_hook', pid=self.attached_pid, code=code)
                self._trial_hook_codes.add(code)
            except (RuntimeError, OSError, ValueError) as exc:
                self.note('探索したHOOKを試用できません: ' + str(exc))

        def refresh_capture_status(self):
            if self.attached_pid and self.host_generation:
                received = sum(bool(values) for values in self.source_samples.values())
                self.hook_capture_status.setText(
                    f'接続中: 本文受信済み {received}件 / 未受信 {len(self.sources) - received}件')
            else:
                self.hook_capture_status.setText('未接続: プレイ画面で実行中ゲームを選択してください')
            if hasattr(self, 'overview_capture'):
                self.overview_capture.setText('台詞取得: ' + ('接続中' if self.attached_pid and self.host_generation
                                              else '未接続'))
            self.refresh_section_summaries()

        def refresh_candidate_actions(self, *_):
            source = self.chosen_source()
            history = self.source_samples.get(source.identity, ()) if source else ()
            self.btn_test_hook_text.setEnabled(bool(history))

        @staticmethod
        def hook_for_source(profile: Profile | None, source: ThreadSource | None):
            if not profile or not source:
                return None
            return next((hook for hook in profile.hooks if hook.matches(source)), None)

        def candidate_row_for_source(self, source: ThreadSource):
            return self.source_rows.get((source.identity, source.address), -1)

        def update_candidate(self, source: ThreadSource, sample: str | None):
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
            hook = self.hook_for_source(profile, source)
            ambiguous = source.identity in self.ambiguous_sources
            self._refreshing_hook_rows = True
            self.source_table.blockSignals(True)
            try:
                use_item = self.source_table.item(row, 0) or QTableWidgetItem('')
                flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
                if not ambiguous:
                    flags |= Qt.ItemFlag.ItemIsEnabled
                use_item.setFlags(flags)
                use_item.setCheckState(Qt.CheckState.Checked if hook and not ambiguous else Qt.CheckState.Unchecked)
                use_item.setToolTip('この取得元を作品設定で使用する')
                self.source_table.setItem(row, 0, use_item)

                role = QComboBox()
                role.addItem('本文', 'text')
                role.addItem('話者', 'speaker')
                role.setCurrentIndex(max(0, role.findData(hook.role if hook else 'text')))
                role.setEnabled(not ambiguous)
                role.currentIndexChanged.connect(
                    lambda _=0, identity=source.identity, address=source.address, control=role:
                    self.hook_role_changed(identity, address, control.currentData()))
                self.source_table.setCellWidget(row, 1, role)

                priority = QSpinBox()
                priority.setRange(1, max(1, len(profile.hooks) if profile else 1))
                hook_index = profile.hooks.index(hook) if profile and hook in profile.hooks else 0
                priority.setValue(hook_index + 1)
                priority.setEnabled(bool(hook) and not ambiguous)
                priority.setToolTip('小さい番号ほど優先。変更すると保存順序を直接入れ替えます。')
                priority.valueChanged.connect(
                    lambda value, identity=source.identity, address=source.address:
                    self.hook_priority_changed(identity, address, value))
                self.source_table.setCellWidget(row, 2, priority)

                pair = QComboBox()
                pair.addItem('対応なし', '')
                if profile:
                    for speaker_hook in profile.hooks:
                        if speaker_hook.role == 'speaker':
                            pair.addItem(speaker_hook.code, speaker_hook.identity)
                if hook and hook.role == 'text':
                    pair.setCurrentIndex(max(0, pair.findData(profile.speaker_links.get(hook.identity, ''))))
                pair.setEnabled(bool(hook and hook.role == 'text') and not ambiguous)
                pair.currentIndexChanged.connect(
                    lambda _=0, identity=source.identity, address=source.address, control=pair:
                    self.hook_pair_changed(identity, address, control.currentData() or ''))
                self.source_table.setCellWidget(row, 3, pair)

                sample_item = self.source_table.item(row, 4) or QTableWidgetItem()
                sample_item.setText(latest[-1])
                sample_item.setData(Qt.ItemDataRole.UserRole, key)
                self.source_table.setItem(row, 4, sample_item)
                source_item = self.source_table.item(row, 5) or QTableWidgetItem()
                source_item.setText(source.hook_name or source.hook_code or '取得元')
                self.source_table.setItem(row, 5, source_item)
                status_item = self.source_table.item(row, 6) or QTableWidgetItem()
                status_item.setText('識別衝突' if ambiguous else '受信中')
                self.source_table.setItem(row, 6, status_item)
            finally:
                self.source_table.blockSignals(False)
                self._refreshing_hook_rows = False
            if self.source_table.currentRow() == row:
                self.show_hook_preview()
            self.refresh_candidate_actions()
            self.refresh_capture_status()

        def hook_use_changed(self, item):
            if getattr(self, '_refreshing_hook_rows', False) or item.column() != 0:
                return
            source = self.source_from_candidate_row(item.row())
            if not source:
                return
            checked = item.checkState() == Qt.CheckState.Checked
            role_control = self.source_table.cellWidget(item.row(), 1)
            role = role_control.currentData() if role_control else 'text'
            self.set_source_hook_assignment(source, role if checked else None)

        def source_by_identity_address(self, identity: str, address: int):
            source = self.sources.get(identity)
            return source if source and source.address == address and identity not in self.ambiguous_sources else None

        def hook_role_changed(self, identity: str, address: int, role: str):
            if getattr(self, '_refreshing_hook_rows', False):
                return
            source = self.source_by_identity_address(identity, address)
            hook = self.hook_for_source(self.current_profile(), source)
            if source and hook and hook.role != role:
                self.set_source_hook_assignment(source, role)

        def apply_pending_monitor_transition(self):
            if not self._pending_monitor_apply:
                return
            self._pending_monitor_apply = False
            profile = self.current_profile()
            if not profile:
                self.stop_session()
                return
            if profile.source == 'hook':
                if not any(h.role == 'text' for h in profile.hooks):
                    self.stop_session()
                    self.note('本文HOOKが未選択のため監視を停止しました。')
                    return
                if not self.attached_pid or not self.host_generation:
                    self.stop_session()
                    self.connect_game()
                    return
            self.begin_monitor(save_draft=False)

        def commit_hook_profile(self, original: Profile, updated: Profile, label: str) -> bool:
            try:
                updated.validate()
                self.catalog.games[self.game_id].profiles[updated.id] = updated
                try:
                    self.persist()
                except Exception:
                    self.catalog.games[self.game_id].profiles[original.id] = original
                    raise
                active = self.controller.profile and self.controller.profile.id == updated.id
                if active:
                    # Capture ownership edits revoke input authority but do not
                    # rewrite the Utterance currently being presented.
                    self.defer_capture_transition()
                elif any(h.role == 'text' for h in updated.hooks) and self.attached_pid and self.host_generation:
                    self._resume_monitor_requested = True
                    self.start_saved_monitor_when_ready()
                self.refresh_selected_hooks()
                self.refresh_status()
                self.note(label + 'を保存しました。自動送り権限は停止したまま、現在の台詞を維持して取得設定を切り替えます。')
                return True
            except Exception as exc:
                self.catalog.games[self.game_id].profiles[original.id] = original
                self.note(label + 'を保存できません: ' + str(exc))
                self.refresh_selected_hooks()
                return False

        def set_source_hook_assignment(self, source: ThreadSource, role: str | None):
            original = self.current_profile()
            if original is None:
                return
            try:
                updated = copy.deepcopy(original)
                existing = next((h for h in updated.hooks if h.matches(source)), None)
                insertion_index = updated.hooks.index(existing) if existing else len(updated.hooks)
                if existing:
                    updated.hooks.remove(existing)
                    updated.speaker_links = {t: v for t, v in updated.speaker_links.items()
                                             if t != existing.identity and v != existing.identity}
                if role is not None:
                    selection = HookSelection(source.hook_code, source.context, source.context2, role)
                    updated.hooks.insert(insertion_index, selection)
                    if source.hook_code in self._trial_hook_codes and source.hook_code not in updated.inserted_hooks:
                        updated.inserted_hooks.append(source.hook_code)
                self.commit_hook_profile(original, updated, 'HOOK割当')
            except Exception as exc:
                self.catalog.games[self.game_id].profiles[original.id] = original
                self.note('HOOK割当を保存できません: ' + str(exc))
                self.refresh_selected_hooks()

        def hook_priority_changed(self, identity: str, address: int, value: int):
            if getattr(self, '_refreshing_hook_rows', False):
                return
            source = self.source_by_identity_address(identity, address)
            original = self.current_profile()
            hook = self.hook_for_source(original, source)
            if not original or not hook:
                return
            old_index = original.hooks.index(hook)
            new_index = max(0, min(len(original.hooks) - 1, value - 1))
            if old_index == new_index:
                return
            updated = copy.deepcopy(original)
            moved = updated.hooks.pop(old_index)
            updated.hooks.insert(new_index, moved)
            self.commit_hook_profile(original, updated, 'HOOK優先順位')

        def hook_pair_changed(self, identity: str, address: int, speaker_identity: str):
            if getattr(self, '_refreshing_hook_rows', False):
                return
            source = self.source_by_identity_address(identity, address)
            original = self.current_profile()
            hook = self.hook_for_source(original, source)
            if not original or not hook or hook.role != 'text':
                return
            current = original.speaker_links.get(hook.identity, '')
            if current == speaker_identity:
                return
            updated = copy.deepcopy(original)
            if speaker_identity:
                updated.speaker_links[hook.identity] = speaker_identity
            else:
                updated.speaker_links.pop(hook.identity, None)
            self.commit_hook_profile(original, updated, 'HOOK対応関係')

        def refresh_selected_hooks(self):
            profile = self.current_profile()
            hooks = profile.hooks if profile else []
            if hooks:
                parts = []
                for index, hook in enumerate(hooks):
                    label = '本文' if hook.role == 'text' else '話者'
                    link = profile.speaker_links.get(hook.identity, '') if hook.role == 'text' else ''
                    suffix = f' → 話者 {next((h.code for h in hooks if h.identity == link), link)}' if link else ''
                    parts.append(f'{index + 1}. {label} {hook.code}{suffix}')
                self.selected_hooks.setText(' / '.join(parts))
            else:
                self.selected_hooks.setText('使用するHOOKが未選択です')
            for source in tuple(self.sources.values()):
                self.update_candidate(source, None)
            self.refresh_hook_connection()

        def refresh_hook_connection(self):
            if not hasattr(self, 'hook_reconnect'):
                return
            profile = self.current_profile()
            if not profile or not profile.hooks:
                self.hook_reconnect.setText('保存済みHOOK: 未選択')
                return
            ready = [h for h in profile.hooks if h.identity in self.sources and h.identity not in self.ambiguous_sources]
            absent = [h for h in profile.hooks if h.identity not in self.sources]
            ambiguous = [h for h in profile.hooks if h.identity in self.ambiguous_sources]
            self.hook_reconnect.setText(
                f'保存済みHOOK: {len(ready)}/{len(profile.hooks)}件を確認 / 待機 {len(absent)} / 識別衝突 {len(ambiguous)}')

        def on_host_hook(self, epoch: int, event: HookEvent):
            if epoch == self._host_epoch:
                self.on_hook(event)

        def on_hook(self, event: HookEvent):
            if event.kind == 'output':
                # Low-level input is recorded synchronously. Pull it before a
                # HOOK output is consumed so a manual advance can own the
                # pre-HOOK audio interval and an identical next line.
                self.sync_manual_target_input()
            if (event.kind == 'hook_found' and event.process_id == self.attached_pid
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                self.try_discovered_hook(event.detail)
            if (event.kind == 'connected' and event.process_id == self.pid.value() and
                    (not self._connecting_pid or self._connecting_pid == event.process_id)):
                self._connecting_pid = 0
                if self.connection_generation != event.connection_generation or self.host_generation != event.host_generation:
                    self.stop_session()
                    self.clear_hook_observations()
                self.host_generation = event.host_generation
                self.connection_generation = event.connection_generation
                self.attached_pid = event.process_id
                self.validation_trace.emit('hook_connected', pid=event.process_id,
                    generation=event.host_generation, connection=event.connection_generation)
                self.host_status.setText(f'LunaHook: 接続済み PID {event.process_id}')
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
                self._connecting_pid = 0
                self.host_generation = ''
                self.connection_generation = 0
                self.clear_hook_observations()
                self.host_status.setText('LunaHook: 切断されました')
                self._resume_monitor_requested = False
            if (event.kind == 'removed' and event.source and event.process_id == self.pid.value()
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                key = event.source.identity
                if self.sources.get(key) and self.sources[key].address == event.source.address:
                    self.sources.pop(key, None)
                if self.controller.active_text_source == key:
                    self.stop_session()
                    self.note('監視中の本文HOOKが消失しました。取得候補を確認してください。')
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
                    self.note('同一識別子のHOOKが複数検出されました。衝突していない取得元を選択してください。')
                    self.update_candidate(current, None)
                    # A colliding thread has no safe text ownership; do not
                    # display its output under the first thread's identity.
                else:
                    self.sources[key] = source
                    self.update_candidate(source, event.text if event.kind == 'output' else None)
                self.refresh_hook_connection()
                self.refresh_capture_status()
                self.start_saved_monitor_when_ready()
                if event.kind == 'created':
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
                pending_profile = self.current_profile()
                if self.pending_hook_boundary(event, pending_profile):
                    # The first different output from the selected pending text
                    # source is the boundary. Rebuild the monitor first, then let
                    # this same output enter the new Controller session below.
                    self.apply_pending_monitor_transition()
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
                            self.apply_confirmed_manual_audio_boundary(result)
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
                    if event.process_id == self._connecting_pid:
                        self._connecting_pid = 0
                if event.detail == 'existing_luna_injection_detected':
                    self.host_status.setText('対象には既にLunaHookが注入されています。ゲームを再起動して再接続してください。')
                elif event.detail == 'cannot_attach_to_host_process':
                    self.host_status.setText('LunaHost自身には接続できません。ゲームを選択してください。')
                self.note('LunaHost: ' + event.detail)


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
                    raise ValueError('作品とプロファイルを選択してください')
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
                        raise ValueError('選択したHOOKの識別子が衝突しています。再接続するか別の取得元を選択してください')
                    self.controller.begin(p, target,
                                          self.host_generation, self.connection_generation,
                                          tuple(self.sources))
                    self.sync_hook_threads()
                if sys.platform == 'win32':
                    self.manual_monitor = WindowsManualMonitor(target.hwnd, target.pid)
                    self._manual_input_serial = self.manual_monitor.target_input_serial
                else:
                    self._manual_input_serial = 0
                self.start_native_audio_if_needed(p)
                self.validation_trace.emit('monitor_started', session=self.controller.session,
                    source=p.source, game_id=self.game_id, profile_id=self.profile_id,
                    hook_pid=target.hook_pid or target.pid, audio_pid=target.audio_pid or target.pid,
                    input_pid=target.pid, generation=self.controller.source_generation,
                    connection=self.controller.connection_generation)
                self.note('監視を開始しました。自動送りは停止したままです。')
            except Exception as exc:
                self.stop_session()
                self.note(f'監視を開始できません: {exc}')
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
                self.apply_confirmed_manual_audio_boundary(result)
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
            if self._pending_monitor_apply and text.strip():
                current = self.controller.utterance
                if current is None or self.controller.sent or self.controller.ocr_blank:
                    # A blank interval is OCR's verified dialogue boundary. The
                    # newly started OCR worker will re-observe the visible line.
                    self.apply_pending_monitor_transition()
                    return
            token = self.controller.observe_ocr(session=session, pid=pid, sequence=sequence, text=text)
            if token:
                self.display_utterance(token)

        def on_ocr_error(self, reason):
            self.note('OCR stopped: ' + reason)
            self.controller.pause('ocr_failure')
            self.refresh_status()

        def sync_manual_target_input(self) -> bool:
            monitor = self.manual_monitor
            profile = self.controller.profile
            if not monitor or not profile:
                return False
            events = monitor.target_inputs_since(self._manual_input_serial)
            if not events:
                return False
            self._manual_input_serial = max(item.serial for item in events)
            candidate = next((item for item in reversed(events)
                              if item.is_advance_candidate(profile.input_key)), None)
            if candidate is None:
                if self.controller.authorized:
                    self.controller.pause('manual_intervention')
                return False

            # A target-scoped advance operation is only a boundary candidate.
            # Stop presentation and revoke automatic input now, but keep native
            # capture bound to the current line until a selected HOOK actually
            # proves that a next Utterance exists. The PCM history lets us apply
            # the candidate timestamp retroactively at that point.
            self.jobs.cancel()
            if profile.source != 'hook':
                if self.controller.authorized:
                    self.controller.pause('manual_intervention')
                return False
            if self.controller.manual_advance(candidate.observed_ns):
                token = self.controller.token
                self.validation_trace.emit('manual_advance_boundary',
                    session=self.controller.session, line=token.line if token else 0,
                    observed_ns=candidate.observed_ns,
                    device='keyboard' if candidate.keyboard else 'mouse',
                    code=candidate.code)
                self.note('手動送りを検出しました。次台詞の取得を待ち、自動送りは停止したままにします。')
                return True
            return False

        def apply_confirmed_manual_audio_boundary(self, token: Token):
            observed_ns = self.controller.take_manual_audio_boundary(token)
            if observed_ns <= 0:
                return
            if self.native_audio:
                self.native_audio.boundary(observed_ns // 1_000_000)
            self.validation_trace.emit('manual_advance_confirmed',
                session=token.session, line=token.line, observed_ns=observed_ns)

        def check_intervention(self):
            if not self.manual_monitor:
                return
            if self.sync_manual_target_input():
                self.refresh_status()
                return
            if not self.manual_monitor.intervened():
                return
            if self._pending_monitor_apply:
                self.controller.pause('settings_changed')
                QTimer.singleShot(0, self.apply_pending_monitor_transition)
                self.refresh_status()
                return
            if self.controller.authorized:
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
            if self._pending_monitor_apply:
                QTimer.singleShot(0, self.apply_pending_monitor_transition)
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
                self.note('送り後に次の台詞を取得できませんでした。自動送りを停止しました。')
                self.refresh_status()

        def permit(self):
            self.validation_trace.emit('permit_requested', session=self.controller.session)
            if self.controller.manual_advance_pending:
                self.controller.permit(True)
                self.note('手動送り後の次台詞を取得してから自動送りを再許可してください。')
                self.refresh_status()
                return
            if self.manual_monitor:
                self.manual_monitor.reset_after_authorization()
                self._manual_input_serial = self.manual_monitor.target_input_serial
            self.controller.permit(True)
            self.advance_if_ready()
            self.refresh_status()

        def pause(self):
            self.validation_trace.emit('pause_requested', session=self.controller.session)
            self.controller.pause()
            self.refresh_status()

        @staticmethod
        def status_reason_label(reason: str | None) -> str:
            labels = {
                None: '',
                'not_monitoring': '監視停止',
                'session_changed': '対象切替',
                'advance_not_authorized': '自動送り停止',
                'presenting': '提示中',
                'ready': '送り可能',
                'awaiting_permission': '許可待ち',
                'text': '文字待機完了',
                'tts': 'TTS再生完了',
                'tts_and_text': 'TTS再生・文字待機完了',
                'native': '原音終了を確認',
                'uncertain_text_fallback': '音声判定不確実・文字待機で完了',
                'manual_intervention': '手動操作を検出',
                'manual_advance_waiting': '手動送り後の次台詞待ち',
                'settings_changed': '取得設定の切替待ち',
                'host_changed': 'LunaHost接続が変更',
                'ambiguous_identical_line': '同文の別台詞か判別不能',
                'ambiguous_multihook_lines': '複数本文の対応が不明',
                'ambiguous_multihook_speakers': '複数話者の対応が不明',
                'unpaired_speaker_output': '話者HOOKの対応本文が不明',
                'ocr_unverified_change': 'OCR変化の台詞境界を確認できない',
                'native_inconclusive_manual_handoff': '原音終了を確定できず手動へ移行',
                'native_audio_failure': '原音監視に失敗',
                'native_audio_unavailable_manual_handoff': '原音監視を利用できず手動へ移行',
                'tts_playback_failed': 'TTS再生に失敗',
                'stopped': '停止',
            }
            return labels.get(reason, reason or '')

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
            targets = (f'HOOK {c.target.hook_pid or c.target.pid} / 音声 {c.target.audio_pid or c.target.pid} / '
                       f'入力 {c.target.pid}:{hex(c.target.hwnd)}') if c.target else '対象未設定'
            monitoring = '監視中' if c.profile else '停止'
            completion = self.status_reason_label(c.presentation_reason) if c.presentation_reason else '判定待ち'
            pause_reason = f' / 停止理由: {self.status_reason_label(c.pause_reason)}' if c.pause_reason else ''
            self.status.setText(
                f'監視: {monitoring} / 自動送り: {"許可" if c.authorized else "停止"} / '
                f'台詞: {c.line_no} / 入力: {"送信済み" if c.sent else "未送信"} / '
                f'提示完了: {completion}{pause_reason}\n{targets}')
            if hasattr(self, 'advance_authorization'):
                previous = self.advance_authorization.blockSignals(True)
                self.advance_authorization.setChecked(bool(c.authorized))
                self.advance_authorization.setText('自動送り: 許可中' if c.authorized else '自動送り: 停止')
                self.advance_authorization.blockSignals(previous)
            self.overview_advance.setText('自動送り: ' +
                ('許可中' if c.authorized else '停止') +
                (' ― ' + self.status_reason_label(c.pause_reason) if c.pause_reason else ''))
            if c.profile and c.utterance:
                self.overview_capture.setText('台詞: ' +
                    (c.utterance.speaker or '話者未設定') + ' / ' + c.utterance.text[:35])
            elif not self.attached_pid:
                self.overview_capture.setText('台詞取得: 未接続')
            if c.profile and c.profile.source == 'hook':
                self.active_hook_status.setText('監視中の本文HOOK: ' +
                    (c.active_text_source or '選択HOOKの出力待ち'))
            else:
                self.active_hook_status.setText('監視中の本文HOOK: なし')

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
            self._manual_input_serial = 0
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

        def on_host_error_from_host(self, epoch: int, message: str):
            if epoch == self._host_epoch:
                self.on_host_error(message)

        def on_host_error(self, message):
            self.note('LunaHostネイティブエラー: ' + message)

        def on_host_closed(self, epoch: int, reason: str):
            if epoch == self._host_epoch:
                self.on_closed(reason)

        def on_closed(self, reason):
            self._resume_monitor_requested = False
            self.stop_session()
            self.attached_pid = 0
            self._connecting_pid = 0
            self.host_generation = ''
            self.connection_generation = 0
            self.host_status.setText(f'LunaHook Host: 停止 ({reason})')

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
