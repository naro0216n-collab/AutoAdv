"""A single GUI integrating game profiles, LunaHook, Irodori, and advancement."""
from __future__ import annotations
import ctypes
import os
import sys
import subprocess
import threading
import copy
import ntpath
from pathlib import Path
import re

from .conversation import Target, Token
from .hook_protocol import HookEvent, ThreadSource
from .model import AdvanceMode, Profile, StructureRule, ReplacementRule, COMMON_FIELDS, interpret
from .storage import load, save
from .import_luna import inspect_luna, add_luna_games
from .import_legacy import inspect_legacy, add_legacy_games
from .game_windows import find_game_windows, find_game_processes, WindowCandidate
from .running_target_dialog import choose_running_target
from .windows_input import WindowsAdvancementInput
from .validation_trace import ValidationTrace, text_digest
from .application import AutoAdvApplication


def run_gui(session_driver=None) -> int:
    from PySide6.QtCore import QObject, Signal, QTimer, Qt
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                                   QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
                                   QLabel, QPushButton, QComboBox, QFileDialog,
                                   QSpinBox, QGroupBox, QMessageBox, QCheckBox, QScrollArea, QInputDialog,
                                   QTableWidget, QTableWidgetItem, QAbstractItemView, QTabWidget, QHeaderView, QToolButton, QMenu, QButtonGroup)

    class SignalBridge(QObject):
        hook = Signal(object)
        closed = Signal(str)
        host_error = Signal(str)
        voice = Signal(object, bool, str)
        server = Signal(object)
        ocr = Signal(int, int, int, str)
        ocr_error = Signal(str)
        native = Signal(object, str, str)
        native_ready = Signal(int, bool, str)
        native_pcm = Signal(object, int)
        preview = Signal(bool, str)

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('AutoAdv')
            self.resize(1180, 900)
            self.catalog = load()
            self.application = AutoAdvApplication(
                self.catalog, self.persist, input_send=self.send_input, preflight=self.preflight)
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
            self.validation_trace = ValidationTrace()
            self.application.configure_voice_jobs(
                on_result=lambda token, done, reason: self.bridge.voice.emit(token, done, reason),
                on_preview=lambda done, reason: self.bridge.preview.emit(done, reason),
                on_timing=(lambda token, stage, duration_ms, audio_ms:
                    self.validation_trace.emit('tts_timing', session=token.session,
                        line=token.line, stage=stage, duration_ms=duration_ms, audio_ms=audio_ms))
                if os.environ.get('AUTOADV_VALIDATION_TRACE') else None)
            self.host_generation = ''
            self.connection_generation = 0
            self.attached_pid = 0
            self._resume_monitor_requested = False
            self._pending_monitor_apply = False
            self._loading_profile = False
            self.source_rows: dict[tuple[str, int], int] = {}
            self._connecting_pid = 0
            self.game_id = ''
            self.profile_id = ''
            self._synced_sources: dict[tuple[int, int, int, int], ThreadSource] = {}
            self._output_group: list[HookEvent] = []
            self._output_timer = QTimer(self)
            self._output_timer.setSingleShot(True)
            self._output_timer.timeout.connect(self.flush_selected_outputs)
            self._capture_output_group: list[HookEvent] = []
            self._capture_output_timer = QTimer(self)
            self._capture_output_timer.setSingleShot(True)
            self._capture_output_timer.timeout.connect(self.flush_capture_outputs)
            self._capture_role_buttons = {}
            self.profile_reset_buttons = {}

            root = QWidget()
            root_layout = QVBoxLayout(root)
            root_layout.setContentsMargins(14, 12, 14, 12)
            root_layout.setSpacing(10)
            self.setCentralWidget(root)
            root.setStyleSheet('''
                QGroupBox { font-weight: 600; margin-top: 10px; padding-top: 10px; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
                QPushButton, QToolButton, QComboBox, QLineEdit, QSpinBox { min-height: 28px; }
                QPushButton { padding: 4px 10px; }
                QPushButton[primary="true"] { font-weight: 700; border: 2px solid palette(highlight); }
                QPushButton:checked, QToolButton:checked { background: palette(highlight); color: palette(highlighted-text); }
                QTableWidget { gridline-color: palette(mid); selection-background-color: palette(highlight); }
                QHeaderView::section { font-weight: 600; padding: 5px; }
                QLabel[problem="true"] { padding: 8px; border: 1px solid palette(mid); border-radius: 6px; }
            ''')

            context = QHBoxLayout()
            self.game_choice = QComboBox()
            self.game_choice.setMinimumWidth(240)
            self.game_choice.currentIndexChanged.connect(self.choose_game)
            context.addWidget(self.game_choice, 2)
            self.profile_choice = QComboBox()
            self.profile_choice.setMinimumWidth(170)
            self.profile_choice.currentIndexChanged.connect(self.choose_profile)
            context.addWidget(self.profile_choice, 1)
            self.btn_pick_running = QPushButton('実行中のゲームを選ぶ')
            self.btn_pick_running.clicked.connect(self.pick_running_game)
            context.addWidget(self.btn_pick_running)
            self.btn_launch = QPushButton('ゲームを起動')
            self.btn_launch.clicked.connect(self.launch_game)
            context.addWidget(self.btn_launch)
            root_layout.addLayout(context)

            self.tabs = QTabWidget()
            root_layout.addWidget(self.tabs, 1)
            pages = {}
            self.page_scrollers = {}
            for key, title in (('home', 'プレイ'), ('game', 'ゲーム'), ('settings', '設定')):
                scroller = QScrollArea()
                scroller.setWidgetResizable(True)
                panel = QWidget()
                layout = QVBoxLayout(panel)
                layout.setSpacing(12)
                scroller.setWidget(panel)
                self.tabs.addTab(scroller, title)
                pages[key] = layout
                self.page_scrollers[key] = scroller
            self.text_tab_index = 1
            self.ui_sections = {}
            self.section_toggles = {}

            def group(title, *, page='game', section=None, detail=False):
                box = QGroupBox(title)
                form = QFormLayout(box)
                form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
                widget = box
                if detail:
                    wrapper = QWidget()
                    outer = QVBoxLayout(wrapper)
                    outer.setContentsMargins(0, 0, 0, 0)
                    toggle = QToolButton()
                    toggle.setText(title)
                    toggle.setCheckable(True)
                    toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                    toggle.setArrowType(Qt.ArrowType.RightArrow)
                    toggle.toggled.connect(box.setVisible)
                    toggle.toggled.connect(lambda checked, button=toggle:
                        button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow))
                    box.setVisible(False)
                    outer.addWidget(toggle)
                    outer.addWidget(box)
                    widget = wrapper
                    if section:
                        self.section_toggles[section] = toggle
                pages[page].addWidget(widget)
                if section:
                    self.ui_sections[section] = widget
                return form

            def setting_row(key, control):
                """Put an effective value and the only relevant inheritance action together."""
                wrapper = QWidget()
                layout = QHBoxLayout(wrapper)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(control, 1)
                reset = QToolButton()
                reset.setText('共通に戻す')
                reset.clicked.connect(lambda _=False, name=key: self.reset_profile_setting(name))
                reset.hide()
                layout.addWidget(reset)
                self.profile_reset_buttons[key] = reset
                return wrapper

            live_dialogue = group('現在の会話', page='home', section='current_dialogue')
            self.preview = QPlainTextEdit()
            self.preview.setReadOnly(True)
            self.preview.setPlaceholderText('ゲームから会話を取得すると、ここに表示されます。')
            self.preview.setMinimumHeight(180)
            live_dialogue.addRow(self.preview)
            self.play_issue = QLabel('')
            self.play_issue.setWordWrap(True)
            self.play_issue.setProperty('problem', True)
            self.play_issue.hide()
            live_dialogue.addRow(self.play_issue)
            self.play_issue_action = QPushButton('')
            self.play_issue_action.clicked.connect(self.open_play_issue_action)
            self.play_issue_action.hide()
            live_dialogue.addRow(self.play_issue_action)
            self._play_issue_action = None
            self.status = self.play_issue
            self.btn_edit_current = QPushButton('この会話の設定を調整')
            self.btn_edit_current.clicked.connect(self.edit_current_dialogue)
            live_dialogue.addRow(self.btn_edit_current)

            advance = group('自動送り', page='home', section='advance')
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
            advance.addRow(setting_row('advance_mode', self.advance_mode_widget))
            self.advance_authorization = QPushButton('自動送り OFF')
            self.advance_authorization.setCheckable(True)
            self.advance_authorization.setProperty('primary', True)
            self.advance_authorization.toggled.connect(self.set_advance_authorization)
            advance.addRow(self.advance_authorization)

            capture_box = QGroupBox('会話取得')
            capture_outer = QVBoxLayout(capture_box)
            self.capture_hint = QLabel('ゲームで台詞を進めると、取得候補が自動的に追加されます。')
            self.capture_hint.setWordWrap(True)
            capture_outer.addWidget(self.capture_hint)
            split = QHBoxLayout()
            left = QVBoxLayout()
            self.source_table = QTableWidget(0, 2)
            self.source_table.setHorizontalHeaderLabels(['用途', '取得されたテキスト'])
            self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.source_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_table.setMinimumHeight(280)
            self.source_table.verticalHeader().setVisible(False)
            self.source_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.source_table.setColumnWidth(0, 180)
            self.source_table.itemSelectionChanged.connect(self.show_hook_preview)
            left.addWidget(self.source_table)
            split.addLayout(left, 3)
            right = QVBoxLayout()
            right.addWidget(QLabel('完成する会話'))
            self.capture_dialogue_preview = QPlainTextEdit()
            self.capture_dialogue_preview.setReadOnly(True)
            self.capture_dialogue_preview.setPlaceholderText('左の候補を台詞・話者として選ぶと、実際の解釈結果をここで確認できます。')
            self.capture_dialogue_preview.setMinimumHeight(240)
            right.addWidget(self.capture_dialogue_preview, 1)
            self.route_table = QTableWidget(0, 3)
            self.route_table.setHorizontalHeaderLabels(['台詞ソース', '話者ソース', '優先'])
            self.route_table.verticalHeader().setVisible(False)
            self.route_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.route_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.route_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.route_table.setMaximumHeight(155)
            self.route_table.hide()
            right.addWidget(self.route_table)
            self.capture_problem = QLabel('')
            self.capture_problem.setWordWrap(True)
            self.capture_problem.setProperty('problem', True)
            self.capture_problem.hide()
            right.addWidget(self.capture_problem)
            self.capture_recovery_action = QPushButton('取得の復旧を開く')
            self.capture_recovery_action.clicked.connect(lambda: self.reveal_section('hook_details', 'game'))
            self.capture_recovery_action.hide()
            right.addWidget(self.capture_recovery_action)
            quick = QHBoxLayout()
            self.btn_edit_detected_speaker = QPushButton('この話者のVoice')
            self.btn_edit_detected_speaker.clicked.connect(self.edit_detected_speaker)
            self.btn_fix_extraction = QPushButton('話者・本文を修正')
            self.btn_fix_extraction.clicked.connect(lambda: self.show_task_section('extraction'))
            self.btn_fix_reading = QPushButton('読みを修正')
            self.btn_fix_reading.clicked.connect(self.edit_preview_reading)
            for button in (self.btn_edit_detected_speaker, self.btn_fix_extraction, self.btn_fix_reading):
                quick.addWidget(button)
            right.addLayout(quick)
            self.btn_commit_capture = QPushButton('この会話取得を使用')
            self.btn_commit_capture.setProperty('primary', True)
            self.btn_commit_capture.clicked.connect(self.commit_capture_workspace)
            self.btn_commit_capture.setEnabled(False)
            right.addWidget(self.btn_commit_capture)
            split.addLayout(right, 2)
            capture_outer.addLayout(split)
            pages['game'].addWidget(capture_box)
            self.ui_sections['capture'] = capture_box

            candidate_details = group('選択した取得元の詳細', page='game', section='candidate_details', detail=True)
            self.hook_preview = QPlainTextEdit()
            self.hook_preview.setReadOnly(True)
            self.hook_preview.setMaximumHeight(120)
            candidate_details.addRow('直近の実出力', self.hook_preview)
            self.hook_technical_info = QLabel('')
            self.hook_technical_info.setWordWrap(True)
            candidate_details.addRow('技術情報', self.hook_technical_info)

            hook_details = group('台詞を取得できない場合', page='game', section='hook_details', detail=True)
            self.btn_recovery_search = QPushButton('高度なHOOK探索を試す')
            self.btn_recovery_search.clicked.connect(self.start_recovery_hook_search)
            hook_details.addRow(self.btn_recovery_search)
            self.btn_pc_hooks = QPushButton('Win32汎用HOOKを試す')
            self.btn_pc_hooks.clicked.connect(self.insert_pc_hooks)
            hook_details.addRow(self.btn_pc_hooks)
            self.insert_pc_hooks_on_attach = QCheckBox('この作品では接続時にWin32汎用HOOKを追加する')
            hook_details.addRow(self.insert_pc_hooks_on_attach)
            self.manual_hook = QLineEdit()
            self.manual_hook.setPlaceholderText('Luna HOOKコード')
            self.btn_insert = QPushButton('手動HOOKを試す')
            self.btn_insert.clicked.connect(self.insert_hook)
            manual_row = QWidget(); manual_layout = QHBoxLayout(manual_row); manual_layout.setContentsMargins(0,0,0,0)
            manual_layout.addWidget(self.manual_hook, 1); manual_layout.addWidget(self.btn_insert)
            hook_details.addRow(manual_row)
            self.hook_flush = QSpinBox(); self.hook_flush.setRange(0, 10000)
            self.hook_codepage = QSpinBox(); self.hook_codepage.setRange(0, 65535); self.hook_codepage.setSpecialValueText('自動 (0)')
            self.hook_history = QSpinBox(); self.hook_history.setRange(1, 10000000)
            self.hook_buffer = QSpinBox(); self.hook_buffer.setRange(1, 1000000)
            hook_details.addRow('Luna出力待機 (ms)', self.hook_flush)
            hook_details.addRow('文字コードページ', self.hook_codepage)
            hook_details.addRow('履歴上限', self.hook_history)
            hook_details.addRow('文字バッファ上限', self.hook_buffer)

            source_mode = group('取得方法', page='game', section='source_mode')
            self.source_mode_widget = QWidget()
            source_mode_layout = QHBoxLayout(self.source_mode_widget); source_mode_layout.setContentsMargins(0,0,0,0)
            self.source_mode_group = QButtonGroup(self); self.source_mode_group.setExclusive(True)
            self.source_hook_button = QPushButton('LunaHook'); self.source_ocr_button = QPushButton('OCR')
            for button, value in ((self.source_hook_button, 'hook'), (self.source_ocr_button, 'ocr')):
                button.setCheckable(True); button.setProperty('sourceMode', value); self.source_mode_group.addButton(button); source_mode_layout.addWidget(button, 1)
            self.source_hook_button.setChecked(True)
            source_mode.addRow(self.source_mode_widget)
            ocr_details = group('OCR設定', page='game', section='ocr_details', detail=True)
            self.ocr_exe = QLineEdit('tesseract'); self.ocr_lang = QLineEdit('jpn'); self.ocr_region = QLineEdit('0,0,640,180')
            self.ocr_stability = QSpinBox(); self.ocr_stability.setRange(1,10)
            ocr_details.addRow('Tesseract', self.ocr_exe); ocr_details.addRow('言語', self.ocr_lang); ocr_details.addRow('領域 x,y,幅,高さ', self.ocr_region); ocr_details.addRow('安定確認回数', self.ocr_stability)
            self.source_ocr_button.toggled.connect(lambda checked: checked and self.reveal_section('ocr_details', 'game'))

            speech = group('話者とVoice', page='game', section='speakers')
            self.default_tts = QCheckBox('未登録話者にもTTSを使用')
            self.default_voice = QComboBox(); self.default_voice.setEditable(True)
            speech.addRow(setting_row('default_tts', self.default_tts)); speech.addRow('既定Voice', setting_row('default_voice', self.default_voice))
            self.speaker_table = QTableWidget(0,5)
            self.speaker_table.setHorizontalHeaderLabels(['話者','TTS','Voice','試聴','削除'])
            self.speaker_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.speaker_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.speaker_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
            self.speaker_table.verticalHeader().setVisible(False)
            self.speaker_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
            self.speaker_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
            self.speaker_table.itemChanged.connect(self.speaker_row_item_changed)
            speech.addRow(self.speaker_table)
            add_speaker = QWidget(); add_speaker_layout = QHBoxLayout(add_speaker); add_speaker_layout.setContentsMargins(0,0,0,0)
            self.new_speaker_name = QLineEdit(); self.new_speaker_name.setPlaceholderText('話者')
            self.new_speaker_tts = QCheckBox('TTS'); self.new_speaker_tts.setChecked(True)
            self.new_speaker_voice = QComboBox(); self.new_speaker_voice.setEditable(True)
            self.btn_add_speaker = QPushButton('追加'); self.btn_add_speaker.clicked.connect(self.add_speaker_inline)
            for w, stretch in ((self.new_speaker_name,2),(self.new_speaker_tts,0),(self.new_speaker_voice,2),(self.btn_add_speaker,0)): add_speaker_layout.addWidget(w, stretch)
            speech.addRow(add_speaker)
            self.audition_text = QLineEdit('音声の試聴です。')
            self.btn_audition = QPushButton('試聴'); self.btn_audition.clicked.connect(self.audition_voice)
            self.btn_stop_audition = QPushButton('停止'); self.btn_stop_audition.clicked.connect(self.application.cancel_voice_preview)
            audition_row=QWidget(); audition_layout=QHBoxLayout(audition_row); audition_layout.setContentsMargins(0,0,0,0); audition_layout.addWidget(self.audition_text,1); audition_layout.addWidget(self.btn_audition); audition_layout.addWidget(self.btn_stop_audition)
            speech.addRow(audition_row)

            reading = group('読みの修正', page='game', section='reading')
            self.reading_table = QTableWidget(0,3); self.reading_table.setHorizontalHeaderLabels(['表記','読み','削除'])
            self.reading_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.reading_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.reading_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
            self.reading_table.verticalHeader().setVisible(False); self.reading_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch); self.reading_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
            self.reading_table.itemChanged.connect(self.reading_row_item_changed); reading.addRow(self.reading_table)
            add_reading=QWidget(); add_reading_layout=QHBoxLayout(add_reading); add_reading_layout.setContentsMargins(0,0,0,0)
            self.new_reading_word=QLineEdit(); self.new_reading_word.setPlaceholderText('表記'); self.new_reading_value=QLineEdit(); self.new_reading_value.setPlaceholderText('読み')
            self.btn_add_reading=QPushButton('追加'); self.btn_add_reading.clicked.connect(self.add_reading_inline)
            add_reading_layout.addWidget(self.new_reading_word,2); add_reading_layout.addWidget(self.new_reading_value,2); add_reading_layout.addWidget(self.btn_add_reading)
            reading.addRow(add_reading)

            rule_preview = group('選択した会話の処理詳細', page='game', section='live_preview', detail=True)
            self.example_raw = QPlainTextEdit(); self.example_raw.setPlaceholderText('取得候補を選ぶと実際の原文が入ります'); self.example_raw.setMaximumHeight(80)
            self.example_raw.textChanged.connect(self.refresh_rule_preview); rule_preview.addRow('原文', self.example_raw)
            self.editor_preview = QPlainTextEdit(); self.editor_preview.setReadOnly(True); self.editor_preview.setMinimumHeight(110); rule_preview.addRow(self.editor_preview)

            extraction = group('話者・本文の切り分け詳細', page='game', section='extraction', detail=True)
            self.structure_table=QTableWidget(0,4); self.structure_table.setHorizontalHeaderLabels(['優先','有効','適用条件','抽出パターン']); self.configure_editor_table(self.structure_table); self.structure_table.itemSelectionChanged.connect(self.show_structure_rule); extraction.addRow(self.structure_table)
            self.structure_template=QComboBox()
            for label,value in (('よくある形式を選ぶ',''),('話者「本文」',r'^\s*(?P<speaker>[^「」]+)「(?P<body>.*?)」\s*$'),('話者：本文',r'^\s*(?P<speaker>[^：:\n]+)[：:]\s*(?P<body>.+)$'),('話者の次行に本文',r'^\s*(?P<speaker>[^\n]+)\n(?P<body>.+)$'),('「本文」のみ',r'^\s*「(?P<body>.*?)」\s*$')): self.structure_template.addItem(label,value)
            self.btn_apply_template=QPushButton('この形式を使う'); self.btn_apply_template.clicked.connect(self.use_structure_template); extraction.addRow(self.structure_template,self.btn_apply_template)
            self.structure_pattern=QLineEdit(); self.structure_condition=QLineEdit(); self.structure_enabled=QCheckBox('有効'); self.structure_enabled.setChecked(True)
            extraction.addRow('抽出規則',self.structure_pattern); extraction.addRow('適用条件',self.structure_condition); extraction.addRow(self.structure_enabled)
            row=QWidget(); lay=QHBoxLayout(row); lay.setContentsMargins(0,0,0,0)
            self.btn_structure=QPushButton('プレビューに反映'); self.btn_structure.clicked.connect(self.add_structure_rule); self.btn_structure_new=QPushButton('新規'); self.btn_structure_new.clicked.connect(self.clear_structure_editor); self.btn_structure_up=QPushButton('↑'); self.btn_structure_up.clicked.connect(self.raise_structure_rule); self.btn_structure_down=QPushButton('↓'); self.btn_structure_down.clicked.connect(self.lower_structure_rule); self.btn_structure_remove=QPushButton('削除'); self.btn_structure_remove.clicked.connect(self.remove_structure_rule)
            for b in (self.btn_structure,self.btn_structure_new,self.btn_structure_up,self.btn_structure_down,self.btn_structure_remove): lay.addWidget(b)
            extraction.addRow(row)

            replacement = group('読み上げ・原音照合の詳細置換', page='game', section='replacement', detail=True)
            self.replacement_table=QTableWidget(0,5); self.replacement_table.setHorizontalHeaderLabels(['有効','一致対象','置換後','適用先','正規表現']); self.configure_editor_table(self.replacement_table); self.replacement_table.itemSelectionChanged.connect(self.show_replacement_rule); replacement.addRow(self.replacement_table)
            self.replace_pattern=QLineEdit(); self.replace_value=QLineEdit(); self.replace_target=QComboBox(); self.replace_target.addItem('読み上げ(TTS)','tts'); self.replace_target.addItem('原音照合(ASR)','asr'); self.replace_target.addItem('両方','both'); self.replace_regex=QCheckBox('正規表現'); self.replace_enabled=QCheckBox('有効'); self.replace_enabled.setChecked(True)
            replacement.addRow('対象',self.replace_pattern); replacement.addRow('置換後',self.replace_value); replacement.addRow('適用先',self.replace_target); replacement.addRow(self.replace_regex); replacement.addRow(self.replace_enabled)
            row=QWidget(); lay=QHBoxLayout(row); lay.setContentsMargins(0,0,0,0)
            self.btn_replace=QPushButton('保存'); self.btn_replace.clicked.connect(self.add_replacement); self.btn_replace_new=QPushButton('新規'); self.btn_replace_new.clicked.connect(self.clear_replacement_editor); self.btn_replacement_remove=QPushButton('削除'); self.btn_replacement_remove.clicked.connect(self.remove_replacement); self.btn_replace_up=QPushButton('↑'); self.btn_replace_up.clicked.connect(lambda: self.move_replacement_rule(-1)); self.btn_replace_down=QPushButton('↓'); self.btn_replace_down.clicked.connect(lambda: self.move_replacement_rule(1))
            for b in (self.btn_replace,self.btn_replace_new,self.btn_replacement_remove,self.btn_replace_up,self.btn_replace_down): lay.addWidget(b)
            replacement.addRow(row)

            game_settings = group('この作品の進行設定', page='game', section='advance_details', detail=True)
            self.uncertain_text_fallback=QCheckBox('音声判定が不確かなとき文字数時間で進む')
            self.per_character_wait_ms=QSpinBox(); self.per_character_wait_ms.setRange(1,600000); self.per_character_wait_ms.setSuffix(' ms / 文字')
            self.reading_ms=QSpinBox(); self.reading_ms.setRange(1,600000); self.reading_ms.setSuffix(' ms')
            self.wait_preview=QLabel('')
            self.input_key=QSpinBox(); self.input_key.setRange(1,254)
            self.next_line_timeout=QSpinBox(); self.next_line_timeout.setRange(1000,120000)
            self.repeat=QComboBox(); self.repeat.addItem('連続同文は重複として扱う','distinct_only'); self.repeat.addItem('送り後の同文は別台詞','each_after_input')
            game_settings.addRow(setting_row('uncertain_text_fallback', self.uncertain_text_fallback)); game_settings.addRow('1文字待機',setting_row('per_character_wait_ms', self.per_character_wait_ms)); game_settings.addRow('最低表示時間',setting_row('reading_ms', self.reading_ms)); game_settings.addRow('送りキー',setting_row('input_key', self.input_key)); game_settings.addRow('次台詞待ち',setting_row('next_line_timeout_ms', self.next_line_timeout)); game_settings.addRow('同文',self.repeat); game_settings.addRow(self.wait_preview)

            target_details = group('別プロセスを使う作品', page='game', section='target_details', detail=True)
            self.game_exe=QLineEdit(); self.audio_exe=QLineEdit(); self.input_exe=QLineEdit()
            target_details.addRow('ゲームEXE',self.game_exe); target_details.addRow('音声EXE',self.audio_exe); target_details.addRow('入力EXE',self.input_exe)
            self.pid=QSpinBox(); self.pid.setRange(0,2147483647); self.audio_pid=QSpinBox(); self.audio_pid.setRange(0,2147483647); self.input_pid=QSpinBox(); self.input_pid.setRange(0,2147483647); self.hwnd=QLineEdit()
            target_details.addRow('現在のHOOK PID',self.pid); target_details.addRow('現在の音声PID',self.audio_pid); target_details.addRow('現在の入力PID',self.input_pid); target_details.addRow('現在のHWND',self.hwnd)

            audio_details = group('音声判断環境', page='game', section='audio_details', detail=True)
            self.native_helper=QLineEdit(); self.asr_model=QLineEdit(); self.asr_tokens=QLineEdit()
            audio_details.addRow('Process Loopback helper',setting_row('native_helper', self.native_helper)); audio_details.addRow('ASRモデル',setting_row('asr_model', self.asr_model)); audio_details.addRow('ASR語彙',setting_row('asr_tokens', self.asr_tokens))

            environment = group('Irodori', page='settings', section='irodori')
            self.irodori_path=QLineEdit(self.catalog.irodori_root); self.irodori_url=QLineEdit(self.catalog.irodori_url)
            environment.addRow('サーバーフォルダ',self.irodori_path); environment.addRow('接続先',self.irodori_url)
            self.server_status=QLabel(''); environment.addRow(self.server_status)
            self.btn_server=QPushButton('接続を再試行'); self.btn_server.clicked.connect(self.start_server); self.btn_server.hide(); environment.addRow(self.btn_server)

            common = group('共通の既定値', page='settings', section='common_advance')
            self.common_controls={}
            common_titles={'advance_mode':'自動進行方式','uncertain_text_fallback':'音声判定が不確かなとき文字数時間で進む','default_tts':'未知話者にもIrodori TTSを使用','default_voice':'既定Voice','reading_ms':'最低表示時間 (ms)','per_character_wait_ms':'1文字あたり待機時間 (ms)','native_helper':'Process Loopbackヘルパー','asr_model':'日本語ASRモデル','asr_tokens':'ASR語彙ファイル','input_key':'送りキーコード','next_line_timeout_ms':'次台詞待ち (ms)'}
            for key in COMMON_FIELDS:
                value=getattr(self.catalog.common,key)
                if key=='advance_mode':
                    control=QWidget(); lay=QHBoxLayout(control); lay.setContentsMargins(0,0,0,0); mode_group=QButtonGroup(self); mode_group.setExclusive(True); text_button=QPushButton('文字のみ判断'); audio_button=QPushButton('音声判断')
                    for button,mode in ((text_button,AdvanceMode.TEXT),(audio_button,AdvanceMode.AUDIO)): button.setCheckable(True); button.setProperty('advanceMode',mode.value); mode_group.addButton(button); lay.addWidget(button,1)
                    (audio_button if value==AdvanceMode.AUDIO else text_button).setChecked(True); self.common_advance_mode_group=mode_group; self.common_advance_text_button=text_button; self.common_advance_audio_button=audio_button
                elif isinstance(value,bool): control=QCheckBox('有効'); control.setChecked(value)
                elif isinstance(value,int): control=QSpinBox(); control.setRange(1,254 if key=='input_key' else 120000 if key=='next_line_timeout_ms' else 600000); control.setValue(value)
                else: control=QLineEdit(value)
                self.common_controls[key]=control; common.addRow(common_titles[key],control)
            for key, control in self.common_controls.items():
                if key == 'advance_mode':
                    self.common_advance_text_button.toggled.connect(
                        lambda checked, name=key: checked and self.common_setting_changed(name))
                    self.common_advance_audio_button.toggled.connect(
                        lambda checked, name=key: checked and self.common_setting_changed(name))
                elif isinstance(control, QCheckBox):
                    control.toggled.connect(lambda _=False, name=key: self.common_setting_changed(name))
                elif isinstance(control, QSpinBox):
                    control.valueChanged.connect(lambda _=0, name=key: self.common_setting_changed(name))
                elif isinstance(control, QComboBox):
                    control.currentTextChanged.connect(lambda _='', name=key: self.common_setting_changed(name))
                else:
                    control.editingFinished.connect(lambda name=key: self.common_setting_changed(name))
            self.irodori_path.editingFinished.connect(self.start_server)
            self.irodori_url.editingFinished.connect(self.start_server)

            management = group('作品とプロファイル', page='settings', section='game_details', detail=True)
            self.new_game=QLineEdit(); self.new_game.setPlaceholderText('作品名'); self.btn_game=QPushButton('作品を追加'); self.btn_game.clicked.connect(self.add_game)
            row=QWidget(); lay=QHBoxLayout(row); lay.setContentsMargins(0,0,0,0); lay.addWidget(self.new_game,1); lay.addWidget(self.btn_game); management.addRow(row)
            self.new_profile=QLineEdit(); self.new_profile.setPlaceholderText('プロファイル名'); self.btn_profile=QPushButton('追加'); self.btn_profile.clicked.connect(self.add_profile)
            row=QWidget(); lay=QHBoxLayout(row); lay.setContentsMargins(0,0,0,0); lay.addWidget(self.new_profile,1); lay.addWidget(self.btn_profile); management.addRow(row)
            self.btn_dup=QPushButton('プロファイルを複製'); self.btn_dup.clicked.connect(self.duplicate_profile); self.btn_rename=QPushButton('名前を変更'); self.btn_rename.clicked.connect(self.rename_profile); self.btn_delete=QPushButton('削除'); self.btn_delete.clicked.connect(self.delete_profile); self.btn_default=QPushButton('既定にする'); self.btn_default.clicked.connect(self.set_default_profile)
            row=QWidget(); lay=QHBoxLayout(row); lay.setContentsMargins(0,0,0,0)
            for b in (self.btn_dup,self.btn_rename,self.btn_delete,self.btn_default): lay.addWidget(b)
            management.addRow(row)
            self.btn_import=QPushButton('Luna設定を取り込む'); self.btn_import.clicked.connect(self.import_luna); self.btn_import_legacy=QPushButton('旧AutoAdv設定を取り込む'); self.btn_import_legacy.clicked.connect(self.import_legacy)
            management.addRow(self.btn_import); management.addRow(self.btn_import_legacy)

            diagnostics = group('診断', page='settings', section='diagnostics', detail=True)
            self.log=QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(220); diagnostics.addRow(self.log)
            self.host_status=QLabel(''); self.hook_target=QLabel(''); self.hook_capture_status=QLabel(''); self.selected_hooks=QLabel(''); self.hook_reconnect=QLabel(''); self.active_hook_status=QLabel('')
            self.btn_host=QPushButton('LunaHook Hostを再起動'); self.btn_host.clicked.connect(self.start_host)
            diagnostics.addRow(self.btn_host); diagnostics.addRow(self.host_status); diagnostics.addRow(self.hook_target); diagnostics.addRow(self.hook_capture_status); diagnostics.addRow(self.selected_hooks); diagnostics.addRow(self.hook_reconnect); diagnostics.addRow(self.active_hook_status)

            self.profile_field_controls={'advance_mode':self.advance_mode_widget,'uncertain_text_fallback':self.uncertain_text_fallback,'default_tts':self.default_tts,'default_voice':self.default_voice,'reading_ms':self.reading_ms,'per_character_wait_ms':self.per_character_wait_ms,'native_helper':self.native_helper,'asr_model':self.asr_model,'asr_tokens':self.asr_tokens,'input_key':self.input_key,'next_line_timeout_ms':self.next_line_timeout}

            for button in (self.advance_text_button,self.advance_audio_button): button.toggled.connect(lambda checked,key='advance_mode': checked and self.profile_setting_changed(key))
            for button in (self.source_hook_button,self.source_ocr_button): button.toggled.connect(lambda checked: checked and self.profile_setting_changed('source'))
            for key,control in self.profile_field_controls.items():
                if key=='advance_mode': continue
                if isinstance(control,QCheckBox): control.toggled.connect(lambda _=False,name=key: self.profile_setting_changed(name))
                elif isinstance(control,QSpinBox): control.valueChanged.connect(lambda _=0,name=key: self.profile_setting_changed(name))
                elif isinstance(control,QComboBox): control.currentTextChanged.connect(lambda _='',name=key: self.profile_setting_changed(name))
                elif isinstance(control,QLineEdit): control.editingFinished.connect(lambda name=key: self.profile_setting_changed(name))
            for control in (self.hook_flush,self.hook_codepage,self.hook_history,self.hook_buffer,self.ocr_stability,self.insert_pc_hooks_on_attach):
                signal = (control.valueChanged if isinstance(control, QSpinBox) else control.toggled)
                signal.connect(lambda _=None: self.save_capture_environment())
            for control in (self.ocr_exe,self.ocr_lang,self.ocr_region):
                control.editingFinished.connect(self.save_capture_environment)
            self.repeat.currentIndexChanged.connect(self.repeat_setting_changed)
            for control in (self.game_exe,self.audio_exe,self.input_exe):
                control.editingFinished.connect(self.game_executables_changed)
            for w in (self.structure_pattern,self.structure_condition,self.replace_pattern,self.replace_value): w.textChanged.connect(lambda _=None,control=w:self.preview_from_control(control))
            for w in (self.structure_enabled,self.replace_regex,self.replace_enabled,self.default_tts): w.toggled.connect(lambda _=None,control=w:self.preview_from_control(control))
            for w in (self.replace_target,self.default_voice): w.currentTextChanged.connect(lambda _='',control=w:self.preview_from_control(control))

            self.confirm_migration_if_needed()
            self.fill_games()
            if self.irodori_url.text().strip(): QTimer.singleShot(0,self.start_server)
            for layout in pages.values(): layout.addStretch(1)

        def note(self, message):
            self.log.appendPlainText(message)
            self._last_note = message
            actionable = ('失敗', 'できません', 'できない', '必要', '選択してください',
                          '保留', '衝突', '停止', '利用でき')
            if any(word in message for word in actionable):
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

        def common_control_value(self, key: str):
            if key == 'advance_mode':
                return AdvanceMode.AUDIO if self.common_advance_audio_button.isChecked() else AdvanceMode.TEXT
            widget = self.common_controls[key]
            if isinstance(widget, QComboBox):
                return widget.currentText().strip()
            if isinstance(widget, QCheckBox):
                return widget.isChecked()
            if isinstance(widget, QSpinBox):
                return widget.value()
            return widget.text().strip()

        def common_setting_changed(self, key: str):
            if self._loading_profile:
                return
            try:
                self.application.set_common_setting(key, self.common_control_value(key))
                profile = self.current_profile()
                if profile and key in profile.inherit_common:
                    self.refresh_inherited_control(key)
                    if self.application.runtime_state().profile and self.application.runtime_state().profile.id == profile.id:
                        effective = self.catalog.effective_profile(profile)
                        if self.runtime_capture_matches(effective):
                            self.application.stage_runtime_profile(effective)
                        else:
                            self.defer_capture_transition()
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('共通設定を保存できません: ' + str(exc))

        def persist(self):
            save(self.catalog)

        def fill_games(self):
            self.game_choice.blockSignals(True)
            self.game_choice.clear()
            for gid, game in self.catalog.games.items():
                self.game_choice.addItem(game.name, gid)
            self.game_choice.blockSignals(False)
            self.choose_game()

        def current_profile(self) -> Profile | None:
            return self.application.current_profile()

        def runtime_capture_matches(self, profile: Profile) -> bool:
            """Whether profile edits can be staged without rebuilding capture."""
            active = self.application.runtime_state().profile
            if not active or active.id != profile.id:
                return False
            if not (active.source == profile.source and active.hooks == profile.hooks and
                    active.speaker_links == profile.speaker_links and
                    not (profile.source == 'ocr' and active.ocr != profile.ocr)):
                return False
            if active.advance_mode != profile.advance_mode:
                return False
            if active.advance_mode == AdvanceMode.AUDIO or profile.advance_mode == AdvanceMode.AUDIO:
                return (active.native_helper == profile.native_helper and
                        active.asr_model == profile.asr_model and active.asr_tokens == profile.asr_tokens)
            return True

        def defer_capture_transition(self):
            """Keep the current Utterance intact and replace capture at a proven boundary."""
            self.application.hold_runtime('settings_changed')
            self._pending_monitor_apply = True
            self.refresh_status()
            if self.application.runtime_state().utterance is None or self.application.runtime_state().sent:
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
            current = self.application.runtime_state().utterance
            return current is None or self.application.runtime_state().sent or event.text != current.raw

        def choose_game(self, *_):
            self._resume_monitor_requested = False
            self.stop_session()
            self.application.close_hook_host()
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

        def choose_window_candidate(self, windows, *, title: str, message: str):
            if not windows:
                return None
            if len(windows) == 1:
                return windows[0]
            choices = [f'{w.title or "(タイトルなし)"} — PID {w.pid}' for w in windows]
            choice, ok = QInputDialog.getItem(self, title, message, choices, 0, False)
            return windows[choices.index(choice)] if ok else None

        def auto_attach_selected_game(self):
            if sys.platform != 'win32' or self.attached_pid or self._connecting_pid:
                return
            game = self.catalog.games.get(self.game_id)
            if not game or not game.executable.strip():
                return
            try:
                windows = find_game_windows(game.executable)
            except OSError:
                return
            chosen = self.choose_window_candidate(
                windows, title='ゲームウィンドウを選択',
                message='この作品に一致する実行中ウィンドウが複数あります')
            if chosen is not None:
                self.use_running_target(chosen)

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

        def set_advance_authorization(self, enabled: bool):
            if enabled:
                self.permit()
            else:
                self.pause()

        def _after_profile_update(self, previous: Profile, updated: Profile) -> None:
            """Apply a persisted user setting to the running session at a safe boundary."""
            effective = self.catalog.effective_profile(updated)
            if (self.attached_pid and self.host_generation and self.application.hook_host_running and
                    updated.source == 'hook' and
                    (updated.hook_flush_ms, updated.hook_codepage, updated.hook_max_buffer, updated.hook_max_history) !=
                    (previous.hook_flush_ms, previous.hook_codepage, previous.hook_max_buffer, previous.hook_max_history)):
                try:
                    self.application.hook_command('settings', flush_ms=updated.hook_flush_ms,
                        codepage=updated.hook_codepage, max_buffer=updated.hook_max_buffer,
                        max_history=updated.hook_max_history)
                except Exception as exc:
                    self.note('取得条件を現在のゲームへ反映できません: ' + str(exc))
            if self.application.runtime_state().profile and self.application.runtime_state().profile.id == updated.id:
                if self.runtime_capture_matches(effective):
                    self.application.stage_runtime_profile(effective)
                else:
                    self.defer_capture_transition()
            self.refresh_capture_workspace()
            self.refresh_rule_preview()

        def profile_setting_changed(self, key: str):
            if self._loading_profile:
                return
            current = self.current_profile()
            if current is None:
                return
            try:
                previous = copy.deepcopy(current)
                value = self.current_source_mode() if key == 'source' else self.profile_control_value(key)
                updated = self.application.set_profile_setting(key, value)
                self._after_profile_update(previous, updated)
                if key in COMMON_FIELDS:
                    self.refresh_inherited_control(key)
            except Exception as exc:
                self.note('設定を保存できません: ' + str(exc))
                self.choose_profile()

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

        def reset_profile_setting(self, key: str):
            if self._loading_profile:
                return
            current = self.current_profile()
            if current is None:
                return
            try:
                previous = copy.deepcopy(current)
                updated = self.application.reset_profile_setting(key)
                self.refresh_inherited_control(key)
                self._after_profile_update(previous, updated)
            except Exception as exc:
                self.note('共通設定へ戻せません: ' + str(exc))

        def save_capture_environment(self):
            """Persist the related acquisition environment, never unrelated UI fields."""
            if self._loading_profile:
                return
            current = self.current_profile()
            if current is None:
                return
            try:
                from .model import OCRSettings
                ocr = OCRSettings(
                    self.ocr_exe.text().strip(), self.ocr_lang.text().strip(),
                    tuple(int(x.strip()) for x in self.ocr_region.text().split(',')),
                    stable_samples=self.ocr_stability.value())
                previous = copy.deepcopy(current)
                def change(profile):
                    profile.ocr = ocr
                    profile.insert_pc_hooks = self.insert_pc_hooks_on_attach.isChecked()
                    profile.hook_flush_ms = self.hook_flush.value()
                    profile.hook_codepage = self.hook_codepage.value()
                    profile.hook_max_buffer = self.hook_buffer.value()
                    profile.hook_max_history = self.hook_history.value()
                updated = self.application.update_profile(change)
                self._after_profile_update(previous, updated)
            except Exception as exc:
                self.note('取得設定を保存できません: ' + str(exc))

        def repeat_setting_changed(self, *_):
            if self._loading_profile:
                return
            current = self.current_profile()
            if current is None:
                return
            try:
                previous = copy.deepcopy(current)
                updated = self.application.set_profile_setting('repeat_rule', self.repeat.currentData())
                self._after_profile_update(previous, updated)
            except Exception as exc:
                self.note('同文設定を保存できません: ' + str(exc))

        def game_executables_changed(self):
            if self._loading_profile or not self.game_id:
                return
            try:
                self.application.update_game_executables(
                    self.game_exe.text(), self.audio_exe.text(), self.input_exe.text())
            except Exception as exc:
                self.note('作品の実行対象を保存できません: ' + str(exc))

        @staticmethod
        def _short_path(value: str) -> str:
            value = (value or '').strip()
            if not value:
                return '未設定'
            return ntpath.basename(value.rstrip('/\\')) or value

        def refresh_inherited_control(self, key: str):
            if key not in self.profile_field_controls or self.current_profile() is None:
                return
            value, source = self.application.effective_setting(key)
            self.set_profile_control_value(key, value)
            reset = self.profile_reset_buttons.get(key)
            if reset is not None:
                reset.setVisible(source == 'game')
            self.refresh_rule_preview()

        def choose_profile(self, *_):
            self._resume_monitor_requested = False
            self.stop_session()
            self.profile_id = self.profile_choice.currentData() or ''
            profile = self.application.select_profile(self.game_id, self.profile_id)
            self._loading_profile = True
            if not profile:
                for table in (self.speaker_table, self.structure_table,
                              self.replacement_table, self.reading_table):
                    table.setRowCount(0)
                self._loading_profile = False
                self.refresh_rule_preview()
                return
            game = self.catalog.games[self.game_id]
            effective = self.catalog.effective_profile(profile)
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
            self.refresh_capture_workspace()
            QTimer.singleShot(0, self.auto_attach_selected_game)

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
                previous = copy.deepcopy(original)
                updated = self.application.update_profile(change)
                self._after_profile_update(previous, updated)
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
                self.application.start_voice_preview(self.audition_text.text(), voice)
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

        def capture_draft_profile(self) -> Profile | None:
            return self.application.capture.profile if self.application.capture else self.current_profile()

        def edit_capture_recipe(self, change) -> bool:
            try:
                self.application.update_capture_recipe(change)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
                return True
            except Exception as exc:
                self.note('会話取得のプレビューを変更できません: ' + str(exc))
                return False

        def refresh_rules(self, focus=None, row=-1):
            p = self.current_profile()
            capture = self.capture_draft_profile()
            if not p or not capture:
                return
            self.fill_editor_table(self.structure_table, [
                (str(i + 1), '有効' if rule.enabled else '無効', rule.condition or '（常時）', rule.pattern)
                for i, rule in enumerate(capture.structure_rules)], row if focus == 'structure' else -1)
            self.fill_editor_table(self.replacement_table, [
                ('有効' if rule.enabled else '無効', rule.pattern, rule.replacement,
                 {'tts': 'TTS', 'asr': 'ASR', 'both': '両方'}[rule.target],
                 '正規表現' if rule.regex else '通常文字列')
                for rule in p.replacements], row if focus == 'replacement' else -1)
            self.refresh_reading_table()
            self.refresh_rule_preview()

        def show_structure_rule(self):
            row = self.selected_editor_row(self.structure_table)
            p = self.capture_draft_profile()
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
            p = self.capture_draft_profile()
            if p is None or row < 0 or not 0 <= row + movement < len(p.structure_rules):
                return
            def change(profile):
                profile.structure_rules[row], profile.structure_rules[row + movement] = (
                    profile.structure_rules[row + movement], profile.structure_rules[row])
            if self.edit_capture_recipe(change):
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
            if self.edit_capture_recipe(change):
                self.refresh_rules('structure', row if row >= 0 else 0)

        def remove_structure_rule(self):
            row = self.selected_editor_row(self.structure_table)
            if row < 0:
                self.note('削除する抽出規則を一覧から選んでください')
                return
            if self.edit_capture_recipe(lambda p: p.structure_rules.pop(row)):
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
            base = self.capture_draft_profile() or p
            draft = copy.deepcopy(self.catalog.effective_profile(base))
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
                tts_result = (f'TTS: Voice {u.voice or "未設定"}' if u.use_tts else 'TTS: 使用しない')
                self.editor_preview.setPlainText(
                    f'話者: {u.speaker or "（未判別）"}\n'
                    f'表示本文: {u.text}\n'
                    f'{tts_result}\n'
                    f'TTS読み上げ: {u.reading}\n'
                    f'原音照合: {u.match_text}')
                self._preview_utterance = u
                self.btn_edit_detected_speaker.setEnabled(bool(u.speaker))
            except (ValueError, re.error, IndexError) as exc:
                self._preview_utterance = None
                self.btn_edit_detected_speaker.setEnabled(False)
                self.editor_preview.setPlainText('編集中の規則は適用できません: ' + str(exc))

        def reveal_section(self, section: str, page: str = 'game'):
            """Open details beside the work result that motivated the edit."""
            page = {'hook': 'game', 'tune': 'game'}.get(page, page)
            toggle = self.section_toggles.get(section)
            if toggle:
                toggle.setChecked(True)
            page_index = {'home': 0, 'game': 1, 'settings': 2}[page]
            self.tabs.setCurrentIndex(page_index)
            widget = self.ui_sections.get(section)
            if widget is not None:
                QTimer.singleShot(0, lambda w=widget, p=page:
                    self.page_scrollers[p].ensureWidgetVisible(w, 0, 22))

        def show_task_section(self, section: str):
            self.reveal_section(section, 'game')

        def open_play_issue_action(self):
            action = self._play_issue_action
            if action == 'open_capture':
                self.reveal_section('capture', 'game')
            elif action == 'open_capture_recovery':
                self.reveal_section('hook_details', 'game')
            elif action == 'open_audio_policy':
                self.reveal_section('audio_details', 'game')
            elif action == 'open_voice':
                self.reveal_section('speakers', 'game')

        def edit_detected_speaker(self):
            u = getattr(self, '_preview_utterance', None)
            if not u or not u.speaker:
                self.show_task_section('extraction')
                return
            self.tabs.setCurrentIndex(self.text_tab_index)
            self.page_scrollers['game'].ensureWidgetVisible(self.ui_sections['speakers'])
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
            u = self.application.runtime_state().utterance
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
                self.application.replace_catalog(proposed)
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
                self.application.replace_catalog(proposed)
                self.persist()
                self.fill_games()
                self.note(f'Imported {len(entries)} previous AutoAdv games; review unmapped fields')
            except Exception as exc:
                self.note('Previous AutoAdv import not applied: ' + str(exc))

        def start_recovery_hook_search(self):
            if (not self.application.hook_host_running or self.attached_pid != self.pid.value()
                    or not self.host_generation):
                self.note('接続が完了していません。接続状態と診断を確認してください。')
                return
            try:
                game = self.catalog.games.get(self.game_id)
                module = Path(game.executable).name if game and game.executable else ''
                # Search is an alternate way to *acquire the same dialogue*, not
                # another UI category. Limit upstream records so automatic trials
                # cannot flood the target with unlimited inserts.
                self.application.hook_command('find_hooks', pid=self.pid.value(), boundary_module=module,
                                       parameters={'maxRecords': 64})
                self.note('追加の取得元を探索しています。台詞を表示すると、取得できた本文が同じ一覧に現れます。')
            except Exception as exc:
                self.note('HOOK探索を開始できません: ' + str(exc))

        def insert_pc_hooks(self):
            try:
                if not (self.host_generation and self.connection_generation and self.application.hook_host_running
                        and self.attached_pid == self.pid.value()):
                    raise RuntimeError('先にゲームへLunaHookを接続してください')
                self.application.hook_command('insert_pc_hooks', pid=self.pid.value())
                self.note('本家Lunaの標準文字API用HOOK追加を要求しました。ゲームで台詞を表示してください。')
            except (OSError, RuntimeError, ValueError) as exc:
                self.note('PC用HOOK追加に失敗: ' + str(exc))

        def insert_hook_code(self, code: str):
            p = self.current_profile()
            if not p or not code:
                return
            try:
                if (not self.application.hook_host_running or not self.host_generation or
                        self.attached_pid != self.pid.value()):
                    raise RuntimeError('先にゲームへLunaHookを接続してください')
                self.application.hook_command('insert_hook', pid=self.pid.value(), code=code)
                self.application.mark_recovery_hook_inserted(code)
            except Exception as exc:
                self.note('HOOKの追加に失敗: ' + str(exc))

        def insert_hook(self):
            self.insert_hook_code(self.manual_hook.text().strip())

        def start_server(self):
            try:
                self.application.set_irodori_environment(self.irodori_path.text(), self.irodori_url.text())
                self.server_status.clear()
                self.btn_server.hide()
                self.application.start_irodori(self.bridge.server.emit)
            except Exception as exc:
                self.server_status.setText('Irodori設定を保存できません: ' + str(exc))
                self.btn_server.show()

        def on_server(self, view):
            if view.issue:
                self.server_status.setText(view.issue)
                self.btn_server.show()
                return
            self.server_status.clear()
            self.btn_server.hide()
            for control in (self.default_voice, self.new_speaker_voice):
                existing = control.currentText()
                blocked = control.blockSignals(True)
                try:
                    control.clear()
                    control.addItems(list(view.voices))
                    control.setEditText(existing)
                finally:
                    control.blockSignals(blocked)
            self.refresh_speaker_choices()

        def start_host(self):
            self.stop_session()
            self.host_generation = ''
            self.connection_generation = 0
            self.attached_pid = 0
            self._connecting_pid = 0
            self.clear_hook_observations()
            try:
                self.application.start_hook_host(
                    on_event=self.bridge.hook.emit,
                    on_closed=self.bridge.closed.emit,
                    on_error=self.bridge.host_error.emit)
                self.host_status.setText('LunaHook Host: 起動済み')
            except Exception as exc:
                self.application.close_hook_host()
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
                chosen = self.choose_window_candidate(
                    windows, title='起動したゲームを選択',
                    message='起動した作品に一致するウィンドウが複数あります')
                if chosen is not None:
                    self.use_running_target(chosen)
                elif not windows:
                    self.note('起動したゲームのウィンドウを自動識別できませんでした。')


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
            self.resolve_associated_targets()
            self.tabs.setCurrentIndex(1)
            self.connect_game()

        def resolve_associated_targets(self):
            """Resolve saved audio/input targets automatically; ask only on ambiguity."""
            game = self.catalog.games.get(self.game_id)
            if not game:
                return
            if game.audio_executable.strip():
                try:
                    candidates = find_game_processes(game.audio_executable)
                    if len(candidates) == 1:
                        self.audio_pid.setValue(candidates[0])
                    elif len(candidates) > 1:
                        choices = [f'PID {pid}' for pid in candidates]
                        choice, ok = QInputDialog.getItem(
                            self, '音声プロセスを選択', '同じ実行ファイルが複数あります', choices, 0, False)
                        if ok:
                            self.audio_pid.setValue(candidates[choices.index(choice)])
                    else:
                        self.note('保存された音声プロセスを実行中の対象から特定できません。')
                except OSError as exc:
                    self.note('音声プロセスを確認できません: ' + str(exc))
            if game.input_executable.strip():
                try:
                    windows = find_game_windows(game.input_executable)
                    if len(windows) == 1:
                        chosen = windows[0]
                    elif len(windows) > 1:
                        choices = [f'{w.title or "(タイトルなし)"} — PID {w.pid}' for w in windows]
                        choice, ok = QInputDialog.getItem(
                            self, '入力先を選択', '同じ実行ファイルのウィンドウが複数あります', choices, 0, False)
                        chosen = windows[choices.index(choice)] if ok else None
                    else:
                        chosen = None
                        self.note('保存された入力先ウィンドウを実行中の対象から特定できません。')
                    if chosen:
                        self.input_pid.setValue(chosen.pid)
                        self.hwnd.setText(hex(chosen.hwnd))
                except OSError as exc:
                    self.note('入力先ウィンドウを確認できません: ' + str(exc))

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
            if not self.application.capture_saved_sources_ready(profile):
                return
            self._resume_monitor_requested = False
            # The output that proves a saved source is available may itself be
            # the first dialogue line. Start now so this same HookEvent can
            # continue through on_hook and enter the new runtime session.
            self.begin_monitor(save_draft=False)

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
            if (self.application.hook_host_running and
                    (self.attached_pid == self.pid.value() and self.host_generation or
                     self._connecting_pid == self.pid.value())):
                if self.attached_pid == self.pid.value() and self.host_generation:
                    self.start_saved_monitor_when_ready()
                return
            if (not self.application.hook_host_running or
                    (self.attached_pid and self.attached_pid != self.pid.value()) or
                    (self._connecting_pid and self._connecting_pid != self.pid.value())):
                self.start_host()
            if not self.application.hook_host_running:
                self.application.report_capture_problem('ゲームから台詞を取得するためのLunaHookを起動できません。')
                self.refresh_capture_workspace()
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
                self.application.hook_command('settings', flush_ms=p.hook_flush_ms,
                    codepage=p.hook_codepage, max_buffer=p.hook_max_buffer, max_history=p.hook_max_history)
                self.application.hook_command('connect', pid=self.pid.value())
                self._connecting_pid = self.pid.value()
                self.host_status.setText('LunaHook: 接続中…')
                self.note('ゲームを選択しました。LunaHook接続を開始しています。')
            except Exception as exc:
                self._connecting_pid = 0
                self.application.report_capture_problem('ゲームへLunaHookを接続できません。')
                self.refresh_capture_workspace()
                self.note('LunaHook接続失敗: ' + str(exc))

        def clear_hook_observations(self):
            self.source_rows.clear()
            self._capture_output_timer.stop()
            self._capture_output_group.clear()
            self.application.clear_capture_observations()
            self.source_table.setRowCount(0)
            self.route_table.setRowCount(0)
            self.route_table.hide()
            self.capture_dialogue_preview.clear()
            self.capture_problem.hide()
            self.hook_preview.clear()
            self.hook_technical_info.clear()
            self.refresh_capture_status()
            self.refresh_hook_connection()

        def source_from_candidate_row(self, row: int):
            if row < 0 or row >= self.source_table.rowCount():
                return None
            item = self.source_table.item(row, 1)
            identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if not isinstance(identity, (tuple, list)) or len(identity) != 2:
                return None
            key, address = identity
            return self.application.capture_source(key, address, require_unique=False)

        def chosen_source(self):
            return self.source_from_candidate_row(self.source_table.currentRow())

        def remove_candidate_row(self, row: int):
            source = self.source_from_candidate_row(row)
            if source:
                self.application.remove_capture_source(source.identity, source.address)
            self.refresh_capture_workspace()

        def try_discovered_hook(self, code: str):
            """Trial a recovery-search code; only real output becomes a candidate."""
            if not self.application.begin_recovery_hook_trial(code):
                return
            if (not self.application.hook_host_running or not self.host_generation or
                    self.attached_pid != self.pid.value()):
                return
            try:
                self.application.hook_command('insert_hook', pid=self.attached_pid, code=code)
                self.application.mark_recovery_hook_inserted(code)
            except (RuntimeError, OSError, ValueError) as exc:
                self.note('探索したHOOKを試用できません: ' + str(exc))

        def refresh_capture_status(self):
            view = self.application.capture_view()
            has_output = bool(view.candidates)
            self.capture_hint.setVisible(not has_output)
            if not has_output:
                self.capture_hint.setText(
                    'ゲームで台詞を進めると、取得候補が自動的に追加されます。')
            # Connection details are diagnostic-only.
            self.hook_capture_status.setText(
                f'LunaHook PID {self.attached_pid}' if self.attached_pid and self.host_generation
                else 'LunaHook未接続')

        def refresh_candidate_actions(self, *_):
            source = self.chosen_source()
            candidate = (self.application.capture_candidate(source.identity, source.address)
                         if source else None)
            history = candidate.history if candidate else ()
            if history and self.example_raw.toPlainText() != history[-1]:
                blocked = self.example_raw.blockSignals(True)
                self.example_raw.setPlainText(history[-1])
                self.example_raw.blockSignals(blocked)
                self.refresh_rule_preview()

        @staticmethod
        def hook_for_source(profile: Profile | None, source: ThreadSource | None):
            if not profile or not source:
                return None
            return next((hook for hook in profile.hooks if hook.matches(source)), None)

        def candidate_row_for_source(self, source: ThreadSource):
            return self.source_rows.get((source.identity, source.address), -1)

        def set_capture_role(self, identity: str, address: int, role: str | None):
            if getattr(self, '_refreshing_hook_rows', False):
                return
            try:
                self.application.set_capture_role(identity, address, role)
                self.sync_hook_threads()
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('会話取得を変更できません: ' + str(exc))
                self.refresh_capture_workspace()

        def refresh_capture_workspace(self):
            view = self.application.capture_view()
            self._refreshing_hook_rows = True
            self.source_table.blockSignals(True)
            self.source_table.setRowCount(len(view.candidates))
            self.source_rows.clear()
            self._capture_role_buttons.clear()
            try:
                for row, candidate in enumerate(view.candidates):
                    self.source_rows[(candidate.identity, candidate.address)] = row
                    role_widget = QWidget()
                    role_layout = QHBoxLayout(role_widget)
                    role_layout.setContentsMargins(0, 0, 0, 0)
                    role_layout.setSpacing(2)
                    group = QButtonGroup(role_widget)
                    group.setExclusive(True)
                    buttons = {}
                    for label, role in (('—', None), ('台詞', 'text'), ('話者', 'speaker')):
                        button = QToolButton()
                        button.setText(label)
                        button.setCheckable(True)
                        button.setEnabled(not candidate.ambiguous)
                        group.addButton(button)
                        role_layout.addWidget(button)
                        buttons[role] = button
                        button.clicked.connect(
                            lambda _=False, ident=candidate.identity, addr=candidate.address, value=role:
                            self.set_capture_role(ident, addr, value))
                    buttons[candidate.role].setChecked(True)
                    self._capture_role_buttons[(candidate.identity, candidate.address)] = buttons
                    self.source_table.setCellWidget(row, 0, role_widget)
                    sample = QTableWidgetItem(candidate.latest)
                    sample.setData(Qt.ItemDataRole.UserRole, (candidate.identity, candidate.address))
                    if candidate.ambiguous:
                        sample.setToolTip('同じ識別子を持つ複数の取得元があり、この候補は使用できません')
                    self.source_table.setItem(row, 1, sample)
            finally:
                self.source_table.blockSignals(False)
                self._refreshing_hook_rows = False

            lines = []
            for preview in view.previews:
                if preview.speaker:
                    lines.append(preview.speaker)
                lines.append(preview.text)
                lines.append('')
            self.capture_dialogue_preview.setPlainText('\n'.join(lines).rstrip())
            if view.problem:
                self.capture_problem.setText(view.problem.message)
                self.capture_problem.show()
            else:
                self.capture_problem.clear()
                self.capture_problem.hide()
            self.capture_recovery_action.setVisible(
                bool(view.problem and view.problem.action == 'open_capture_recovery'))
            self.btn_commit_capture.setEnabled(view.dirty and view.can_commit)
            self.refresh_capture_routes()
            self.refresh_capture_status()
            self.refresh_hook_connection()

        def refresh_capture_routes(self):
            workspace = self.application.capture
            if workspace is None:
                self.route_table.hide()
                return
            profile = workspace.profile
            texts = [h for h in profile.hooks if h.role == 'text']
            speakers = [h for h in profile.hooks if h.role == 'speaker']
            # One text + one speaker is intentionally implicit. Only expose
            # pairing/priority when there is a genuine choice to make.
            needs_editor = len(texts) > 1 or len(speakers) > 1
            self.route_table.setVisible(needs_editor)
            if not needs_editor:
                self.route_table.setRowCount(0)
                return
            candidates = {c.identity: c for c in self.application.capture_view().candidates}
            self.route_table.setRowCount(len(texts))
            for row, text_hook in enumerate(texts):
                text_candidate = candidates.get(text_hook.identity)
                self.route_table.setItem(row, 0, QTableWidgetItem(
                    (text_candidate.latest[:60] if text_candidate else text_hook.code)))
                combo = QComboBox()
                combo.addItem('話者なし', '')
                for speaker_hook in speakers:
                    candidate = candidates.get(speaker_hook.identity)
                    combo.addItem(candidate.latest[:40] if candidate else speaker_hook.code,
                                  speaker_hook.identity)
                current = profile.speaker_links.get(text_hook.identity, '')
                combo.setCurrentIndex(max(0, combo.findData(current)))
                combo.currentIndexChanged.connect(
                    lambda _=0, text_id=text_hook.identity, control=combo:
                    self.set_capture_pair(text_id, control.currentData() or None))
                self.route_table.setCellWidget(row, 1, combo)
                order = QWidget(); order_layout = QHBoxLayout(order); order_layout.setContentsMargins(0,0,0,0)
                up = QToolButton(); up.setText('↑'); down = QToolButton(); down.setText('↓')
                up.setEnabled(row > 0); down.setEnabled(row < len(texts)-1)
                up.clicked.connect(lambda _=False, identity=text_hook.identity: self.move_capture_route(identity, -1))
                down.clicked.connect(lambda _=False, identity=text_hook.identity: self.move_capture_route(identity, 1))
                order_layout.addWidget(up); order_layout.addWidget(down)
                self.route_table.setCellWidget(row, 2, order)

        def set_capture_pair(self, text_identity: str, speaker_identity: str | None):
            try:
                self.application.pair_capture_sources(text_identity, speaker_identity)
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('話者ソースを対応付けできません: ' + str(exc))

        def move_capture_route(self, identity: str, offset: int):
            self.application.move_dialogue_source(identity, offset)
            self.refresh_capture_workspace()

        def update_candidate(self, source: ThreadSource, sample: str | None, *, sequence: int = 0):
            self.application.observe_capture(source, sample or '', sequence=sequence)
            self.refresh_capture_workspace()

        def source_by_identity_address(self, identity: str, address: int):
            return self.application.capture_source(identity, address)

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
                    return
                if not self.attached_pid or not self.host_generation:
                    self.stop_session()
                    self.connect_game()
                    return
            self.begin_monitor(save_draft=False)

        def commit_capture_workspace(self):
            previous = self.current_profile()
            if previous is None:
                return
            try:
                updated = self.application.commit_capture()
                active = self.application.runtime_state().profile and self.application.runtime_state().profile.id == updated.id
                if active:
                    self.defer_capture_transition()
                elif any(h.role == 'text' for h in updated.hooks) and self.attached_pid and self.host_generation:
                    self._resume_monitor_requested = True
                    self.start_saved_monitor_when_ready()
                self.refresh_capture_workspace()
                self.refresh_selected_hooks()
            except Exception as exc:
                self.note('会話取得を確定できません: ' + str(exc))

        def refresh_selected_hooks(self):
            profile = self.current_profile()
            hooks = profile.hooks if profile else []
            if hooks:
                self.selected_hooks.setText(' / '.join(
                    ('台詞 ' if h.role == 'text' else '話者 ') + h.code for h in hooks))
            else:
                self.selected_hooks.setText('保存済み会話ソースなし')
            self.refresh_capture_workspace()

        def refresh_hook_connection(self):
            profile = self.current_profile()
            if not profile or not profile.hooks:
                self.hook_reconnect.setText('保存済み会話ソースなし')
                return
            ready = [h for h in profile.hooks
                     if self.application.capture_has_identity(h.identity)
                     and not self.application.capture_identity_is_ambiguous(h.identity)]
            ambiguous = [h for h in profile.hooks
                         if self.application.capture_identity_is_ambiguous(h.identity)]
            self.hook_reconnect.setText(
                f'保存ソース {len(ready)}/{len(profile.hooks)}確認' +
                (f' / 識別衝突 {len(ambiguous)}' if ambiguous else ''))

        def on_host_hook(self, event: HookEvent):
            self.on_hook(event)

        def on_hook(self, event: HookEvent):
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
                self.application.clear_capture_problem()
                self.refresh_capture_workspace()
                self.validation_trace.emit('hook_connected', pid=event.process_id,
                    generation=event.host_generation, connection=event.connection_generation)
                self.host_status.setText(f'LunaHook: 接続済み PID {event.process_id}')
                self.refresh_capture_status()
                self.start_saved_monitor_when_ready()
                p = self.current_profile()
                if p and p.source == 'hook':
                    if p.insert_pc_hooks:
                        self.application.hook_command('insert_pc_hooks', pid=event.process_id)
                    for code in p.inserted_hooks:
                        self.application.hook_command('insert_hook', pid=event.process_id, code=code)
            elif event.kind == 'disconnected' and event.process_id == self.pid.value():
                self.stop_session()
                self.attached_pid = 0
                self._connecting_pid = 0
                self.host_generation = ''
                self.connection_generation = 0
                self.clear_hook_observations()
                self.host_status.setText('LunaHook: 切断されました')
                self._resume_monitor_requested = False
                self.application.report_capture_problem('ゲームからの台詞取得が切断されました。')
                self.refresh_capture_workspace()
            if (event.kind == 'removed' and event.source and event.process_id == self.pid.value()
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                key = event.source.identity
                self.application.remove_capture_source(key, event.source.address)
                if self.application.runtime_state().active_text_source == key:
                    self.stop_session()
                    self.note('監視中の本文HOOKが消失しました。取得候補を確認してください。')
                self.refresh_capture_workspace()
                self.refresh_hook_connection()
                self.refresh_capture_status()
            if (event.kind in ('created', 'output') and event.source and event.process_id == self.pid.value()
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation):
                source = event.source
                key = source.identity
                was_ambiguous = self.application.capture_identity_is_ambiguous(key)
                self.update_candidate(source, event.text if event.kind == 'output' else None,
                                      sequence=event.sequence)
                if self.application.capture_identity_is_ambiguous(key) and not was_ambiguous:
                    if self.application.runtime_state().profile:
                        self.stop_session()
                    self.note('同一識別子のHOOKが複数検出されました。衝突していない取得元を選択してください。')
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
            if (event.kind == 'output' and event.source and event.process_id == self.pid.value()
                    and event.host_generation == self.host_generation
                    and event.connection_generation == self.connection_generation
                    and not self.application.capture_identity_is_ambiguous(event.source.identity)):
                self._capture_output_group.append(event)
                if len(self._capture_output_group) >= 64:
                    self._capture_output_timer.stop()
                    self.flush_capture_outputs()
                else:
                    # Same upper bound as the selected multi-HOOK collector.
                    # This batch is preview evidence only and never drives input.
                    self._capture_output_timer.start(100)
            if event.kind == 'output' and event.source:
                pending_profile = self.current_profile()
                if self.pending_hook_boundary(event, pending_profile):
                    # The first different output from the selected pending text
                    # source is the boundary. Rebuild the monitor first, then let
                    # this same output enter the new Controller session below.
                    self.apply_pending_monitor_transition()
                if (self.application.runtime_state().profile and self.application.runtime_state().profile.source == 'hook'
                        and event.process_id == (self.application.runtime_state().target.hook_pid or self.application.runtime_state().target.pid)
                        and event.host_generation == self.application.runtime_state().source_generation
                        and event.connection_generation == self.application.runtime_state().connection_generation
                        and any(h.matches(event.source) for h in self.application.runtime_state().profile.hooks)):
                    self.validation_trace.emit('hook_output', session=self.application.runtime_state().session,
                        generation=event.host_generation, connection=event.connection_generation,
                        hook_sequence=event.sequence, source=event.source.identity,
                        received_ns=event.received_ns, pid=event.process_id, address=event.source.address,
                        context=event.source.context, context2=event.source.context2,
                        raw_hash=text_digest(event.text))
                if (event.process_id == self.pid.value()
                        and not self.application.capture_identity_is_ambiguous(event.source.identity)
                        and event.host_generation == self.host_generation
                        and event.connection_generation == self.connection_generation):
                    selected = self.application.runtime_state().profile.hooks if self.application.runtime_state().profile else ()
                    if len(selected) > 1 and any(h.matches(event.source) for h in selected):
                        # Luna's Python multi-HOOK collector groups already-flushed
                        # outputs; it does not re-flush or reconstruct raw bytes.
                        self._output_group.append(event)
                        if len(self._output_group) >= 32:
                            self._output_timer.stop()
                            self.flush_selected_outputs()
                        else:
                            self._output_timer.start(max(10, min(100, self.application.runtime_state().profile.hook_flush_ms)))
                    else:
                        result = self.application.observe_runtime_hook(generation=event.host_generation,
                            connection=event.connection_generation, sequence=event.sequence,
                            source=event.source, text=event.text, pid=event.process_id or 0,
                            received_ns=event.received_ns)
                        if result and self.application.runtime_state().utterance:
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
                    self.application.report_capture_problem('対象には既にLunaHookが注入されています。ゲームを再起動してから選び直してください。')
                    self.refresh_capture_workspace()
                elif event.detail == 'cannot_attach_to_host_process':
                    self.host_status.setText('LunaHost自身には接続できません。ゲームを選択してください。')
                    self.application.report_capture_problem('ゲームではないLunaHostプロセスが選択されています。実行中のゲームを選び直してください。')
                    self.refresh_capture_workspace()
                self.note('LunaHost: ' + event.detail)


        def show_hook_preview(self, *_):
            source = self.chosen_source()
            candidate = (self.application.capture_candidate(source.identity, source.address)
                         if source else None)
            history = candidate.history if candidate else ()
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
            candidate = (self.application.capture_candidate(source.identity, source.address)
                         if source else None)
            samples = candidate.history if candidate else ()
            sample = samples[-1] if samples else ''
            if not sample:
                self.note('実際に受信された本文を選択してください')
                return
            self.example_raw.setPlainText(sample)
            self.tabs.setCurrentIndex(self.text_tab_index)

        def begin_monitor(self, *, save_draft=False):
            try:
                p = self.current_profile()
                target = self.session_target()
                if not p:
                    raise ValueError('作品とプロファイルを選択してください')
                p = self.catalog.effective_profile(p)
                self.application.cancel_voice()
                self.stop_session()
                if p.source == 'ocr':
                    self.application.begin_runtime(p, target, 'ocr', 1)
                    self.application.start_ocr(
                        p,
                        on_text=lambda session, pid, sequence, text:
                            self.bridge.ocr.emit(session, pid, sequence, text),
                        on_error=self.bridge.ocr_error.emit)
                else:
                    if any(self.application.capture_identity_is_ambiguous(h.identity) for h in p.hooks):
                        raise ValueError('選択したHOOKの識別子が衝突しています。再接続するか別の取得元を選択してください')
                    self.application.begin_runtime(p, target,
                                          self.host_generation, self.connection_generation,
                                          tuple(source.identity for source in self.application.capture_sources()))
                    self.sync_hook_threads()
                self.start_native_audio_if_needed(p)
                self.validation_trace.emit('monitor_started', session=self.application.runtime_state().session,
                    source=p.source, game_id=self.game_id, profile_id=self.profile_id,
                    hook_pid=target.hook_pid or target.pid, audio_pid=target.audio_pid or target.pid,
                    input_pid=target.pid, generation=self.application.runtime_state().source_generation,
                    connection=self.application.runtime_state().connection_generation)
                self.note('監視を開始しました。自動送りは' + ('ONです。' if self.application.runtime_state().authorized else 'OFFです。'))
            except Exception as exc:
                self.stop_session()
                self.note(f'監視を開始できません: {exc}')
            self.refresh_status()

        def sync_hook_threads(self, *, clear: bool = False):
            """Use Luna synchronization for every currently meaningful multi-source recipe."""
            selected = []
            if not clear:
                profiles = []
                if self.application.runtime_state().profile and self.application.runtime_state().profile.source == 'hook':
                    profiles.append(self.application.runtime_state().profile)
                if self.application.capture and self.application.capture.profile.source == 'hook':
                    profiles.append(self.application.capture.profile)
                seen = set()
                for profile in profiles:
                    hooks = profile.hooks if len(profile.hooks) > 1 else ()
                    for hook in hooks:
                        if hook.identity not in seen:
                            selected.append(hook)
                            seen.add(hook.identity)
            desired = {}
            for source in self.application.capture_sources():
                if (source.process_id == self.pid.value() and
                        any(h.matches(source) for h in selected)):
                    key = (source.process_id, source.address, source.context, source.context2)
                    desired[key] = source
            for key, source in tuple(self._synced_sources.items()):
                if key not in desired:
                    try:
                        if self.application.hook_host_running:
                            self.application.hook_command('sync_thread', source=source.__dict__, enabled=False)
                    except (RuntimeError, OSError, ValueError):
                        pass
                    self._synced_sources.pop(key, None)
            if self.application.hook_host_running:
                for key, source in desired.items():
                    if key not in self._synced_sources:
                        try:
                            self.application.hook_command('sync_thread', source=source.__dict__, enabled=True)
                            self._synced_sources[key] = source
                        except (RuntimeError, OSError, ValueError) as exc:
                            self.note('Luna selected-HOOK sync unavailable: ' + str(exc))

        def flush_capture_outputs(self):
            group, self._capture_output_group = self._capture_output_group, []
            if not group:
                return
            self.application.observe_capture_group(group)
            self.refresh_capture_workspace()

        def flush_selected_outputs(self):
            group, self._output_group = self._output_group, []
            if not group or not self.application.runtime_state().profile:
                return
            result = self.application.observe_runtime_group(group)
            if result and self.application.runtime_state().utterance:
                self.display_utterance(result)
            else:
                self.refresh_status()

        def start_native_audio_if_needed(self, profile: Profile):
            if profile.advance_mode != AdvanceMode.AUDIO:
                self.application.close_native_audio()
                return
            has_asr = bool(profile.asr_model and profile.asr_tokens)
            if not profile.native_helper or not has_asr:
                # This is a per-utterance prerequisite, not a session-wide
                # pause: audio-mode TTS lines require only playback drain.
                # A non-TTS line enters the normal uncertain-audio fallback
                # or manual-handoff decision when its text deadline arrives.
                self.note('非TTSの音声判断にはProcess LoopbackとASRモデル・tokensが必要です。根拠不足では設定に従います。')
                self.application.close_native_audio()
                return
            self.note('Loading native audio capture without blocking game UI')
            self.application.start_native_audio(
                profile,
                on_ready=lambda session, ready, reason:
                    self.bridge.native_ready.emit(session, ready, reason),
                on_event=lambda token, event, detail:
                    self.bridge.native.emit(token, event, detail),
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

        def on_native_ready(self, session, ready, reason):
            if session != self.application.runtime_state().session or not self.application.runtime_state().target:
                return
            if not ready:
                self.note('Native audio unavailable: ' + reason)
                self.refresh_status()
                return
            self.validation_trace.emit('native_ready', session=session, audio_pid=self.application.native_audio_pid)
            self.note('Process audio helper started; waiting for actual captured PCM')
            token, u = self.application.runtime_state().token, self.application.runtime_state().utterance
            if token and u and not self.application.runtime_state().sent and not self.application.runtime_state().completed and u.advance_mode == AdvanceMode.AUDIO and not u.use_tts:
                self.application.begin_native_audio(token, u.match_text)

        def on_native_pcm(self, token, frames):
            if token is not None and token == self.application.runtime_state().token:
                self.validation_trace.emit('native_pcm', session=token.session, line=token.line,
                    frames=frames, audio_pid=self.application.native_audio_pid)

        def on_native(self, token, event, detail):
            if token != self.application.runtime_state().token or token is None:
                return
            self.validation_trace.emit('native_event', session=token.session, line=token.line,
                status=event, method='asr')
            if event == 'error':
                self.note('Native audio: ' + detail)
            if self.application.handle_native_event(token, event, detail):
                self.validation_trace.emit('presentation_complete', session=token.session,
                    line=token.line, success=True,
                    reason=self.application.runtime_state().presentation_reason)
                self.advance_if_ready()
            self.refresh_status()

        def display_utterance(self, token: Token):
            u = self.application.runtime_state().utterance
            if not u:
                return
            conversation = (f'{u.speaker}\n' if u.speaker else '') + u.text
            self.preview.setPlainText(conversation)
            profile = self.application.runtime_state().profile
            if profile:
                self.wait_preview.setText(
                    f'{u.character_count}文字 / {u.text_wait_ms} ms')
            if not self.example_raw.hasFocus():
                blocked = self.example_raw.blockSignals(True)
                self.example_raw.setPlainText(u.raw)
                self.example_raw.blockSignals(blocked)
                self.refresh_rule_preview()
            self.validation_trace.emit('utterance', session=token.session, line=token.line,
                source=u.source, hook_sequence=u.sequence, raw_hash=text_digest(u.raw),
                body_hash=text_digest(u.text), speaker=u.speaker, advance_mode=u.advance_mode.value, use_tts=u.use_tts,
                characters=u.character_count, text_wait_ms=u.text_wait_ms,
                uncertain_text_fallback=u.uncertain_text_fallback)
            self.present(token)
            self.refresh_status()

        def present(self, token: Token):
            u = self.application.runtime_state().utterance
            if not u or token != self.application.runtime_state().token:
                return
            self.application.cancel_voice()
            self.application.cancel_native_audio()
            external_ns = self.application.take_external_transition(token)
            if external_ns:
                self.validation_trace.emit('external_hook_transition', session=token.session,
                    line=token.line, received_ns=external_ns)
            if u.advance_mode == AdvanceMode.TEXT:
                QTimer.singleShot(u.text_wait_ms, lambda t=token: self.presentation_event(t, 'text_elapsed'))
            elif not u.use_tts:
                if self.application.native_audio_available:
                    replay_from_ms = None
                    if external_ns:
                        # Luna may flush after the game already started voice.
                        # Replay only the bounded acquisition-delay window; ASR
                        # still must match the new text before the PCM is owned.
                        lookback_ms = max(100, self.application.runtime_state().profile.hook_flush_ms + 100)
                        replay_from_ms = max(0, external_ns // 1_000_000 - lookback_ms)
                    self.application.begin_native_audio(token, u.match_text, replay_from_ms=replay_from_ms)
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
                    self.application.start_voice(token, u.reading, u.voice)

        def fallback_due(self, token):
            u = self.application.runtime_state().utterance
            if (token != self.application.runtime_state().token or not u or u.use_tts or
                    u.advance_mode != AdvanceMode.AUDIO or self.application.runtime_state().completed or self.application.runtime_state().sent):
                return
            if self.application.native_audio_holds_deadline(token):
                # A correlated voice is active, or a bounded real ASR analysis
                # of captured speech is pending. Neither grants completion.
                QTimer.singleShot(200, lambda t=token: self.fallback_due(t))
                return
            if self.application.runtime_state().native_playing:
                self.presentation_event(token, 'native_evidence_stale')
            self.presentation_event(token, 'fallback_elapsed')

        def on_voice(self, token, success, reason):
            if token == self.application.runtime_state().token and token is not None:
                self.validation_trace.emit('tts_result', session=token.session,
                    line=token.line, success=bool(success))
            self.presentation_event(token, 'tts_finished', success=success)
            if reason and not success and token == self.application.runtime_state().token:
                self.note(reason)

        def presentation_event(self, token, event, *, success=True):
            if self.application.runtime_presentation_event(token, event, success=success):
                self.validation_trace.emit('presentation_complete', session=token.session,
                    line=token.line, success=True, reason=self.application.runtime_state().presentation_reason)
                self.advance_if_ready()
            self.refresh_status()

        def on_ocr(self, session, pid, sequence, text):
            if self._pending_monitor_apply and text.strip():
                current = self.application.runtime_state().utterance
                if current is None or self.application.runtime_state().sent or self.application.runtime_state().ocr_blank:
                    # A blank interval is OCR's verified dialogue boundary. The
                    # newly started OCR worker will re-observe the visible line.
                    self.apply_pending_monitor_transition()
                    return
            token = self.application.observe_runtime_ocr(session=session, pid=pid, sequence=sequence, text=text)
            if token:
                self.display_utterance(token)

        def on_ocr_error(self, reason):
            self.note('OCR stopped: ' + reason)
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
            token = self.application.runtime_state().token
            if token:
                self.validation_trace.emit('advance_attempt', session=token.session,
                    line=token.line)
            # Start next-line observation at the input operation, before the
            # game can render/play ahead of Luna's delayed HOOK flush.
            if self.application.native_audio_available:
                self.application.native_audio_boundary()
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
            if not self.application.try_advance():
                return
            token = self.application.runtime_state().token
            timeout = self.application.runtime_state().profile.next_line_timeout_ms
            QTimer.singleShot(timeout, lambda t=token: self.expire_next_line(t))

        def expire_next_line(self, token):
            if self.application.expire_next_line(token):
                self.validation_trace.emit('next_line_timeout', session=token.session, line=token.line)
                self.note('送り後に次の台詞を取得できません。自動送りはONのまま入力を保留します。')
                self.refresh_status()

        def permit(self):
            self.validation_trace.emit('permit_requested', session=self.application.runtime_state().session)
            self.application.set_auto_advance(True)
            self.advance_if_ready()
            self.refresh_status()

        def pause(self):
            self.validation_trace.emit('pause_requested', session=self.application.runtime_state().session)
            self.application.set_auto_advance(False)
            self.refresh_status()

        def refresh_status(self):
            c = self.application.runtime_state()
            token = c.token
            if self.validation_trace.path and token is not None and c.pause_reason:
                marker = (token, c.pause_reason)
                if marker != getattr(self, '_last_validation_handoff', None):
                    self.validation_trace.emit('controller_handoff', session=token.session,
                        line=token.line, reason=c.pause_reason)
                    u = c.utterance
                    if (self.application.native_audio_available and u and
                            u.advance_mode == AdvanceMode.AUDIO and not u.use_tts):
                        self.validation_trace.emit('native_capture_summary',
                            session=token.session, line=token.line,
                            audio_pid=self.application.native_audio_pid,
                            **self.application.native_audio_capture_status())
                    self._last_validation_handoff = marker
            view = self.application.play_view()
            previous = self.advance_authorization.blockSignals(True)
            self.advance_authorization.setChecked(view.advance_authorized)
            self.advance_authorization.setText('自動送り ON' if view.advance_authorized else '自動送り OFF')
            self.advance_authorization.blockSignals(previous)
            if view.issue:
                self.play_issue.setText(view.issue.message)
                self.play_issue.show()
                self._play_issue_action = view.issue.action
                labels = {
                    'open_capture': '会話取得を確認',
                    'open_capture_recovery': '取得の復旧を開く',
                    'open_audio_policy': '音声判断設定を開く',
                    'open_voice': 'Voice設定を開く',
                }
                if view.issue.action in labels:
                    self.play_issue_action.setText(labels[view.issue.action])
                    self.play_issue_action.show()
                else:
                    self.play_issue_action.hide()
            else:
                self._play_issue_action = None
                self.play_issue.clear()
                self.play_issue.hide()
                self.play_issue_action.hide()
            # Low-level runtime identity remains available only in Diagnostics.
            if c.profile and c.profile.source == 'hook':
                self.active_hook_status.setText('active text source: ' + (c.active_text_source or '(waiting)'))
            else:
                self.active_hook_status.clear()

        def stop_session(self):
            self.validation_trace.emit('monitor_ended', session=self.application.runtime_state().session)
            if hasattr(self, '_output_timer'):
                self._output_timer.stop()
                self._output_group.clear()
            if hasattr(self, '_capture_output_timer'):
                self._capture_output_timer.stop()
                self._capture_output_group.clear()
            if hasattr(self, '_synced_sources') and self._synced_sources:
                self.sync_hook_threads(clear=True)
            self.application.close_native_audio()
            self.application.close_ocr()
            self.application.cancel_voice()
            self.application.cancel_voice_preview()
            self.application.close_runtime('session_changed')
            if hasattr(self, 'preview'):
                self.preview.clear()
            if hasattr(self, 'status'):
                self.refresh_status()

        def on_host_error_from_host(self, message: str):
            self.on_host_error(message)

        def on_host_error(self, message):
            self.application.report_capture_problem('ゲームから台詞を取得できません。取得の復旧を確認してください。')
            self.refresh_capture_workspace()
            self.note('LunaHostネイティブエラー: ' + message)

        def on_host_closed(self, reason: str):
            self.on_closed(reason)

        def on_closed(self, reason):
            self._resume_monitor_requested = False
            self.stop_session()
            self.attached_pid = 0
            self._connecting_pid = 0
            self.host_generation = ''
            self.connection_generation = 0
            self.host_status.setText(f'LunaHook Host: 停止 ({reason})')
            if self.isVisible():
                self.application.report_capture_problem('ゲームからの台詞取得が停止しました。')
                self.refresh_capture_workspace()

        def closeEvent(self, event):
            self.stop_session()
            self.application.close_hook_host()
            self.application.close_irodori()
            super().closeEvent(event)

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    if session_driver is not None:
        QTimer.singleShot(0, lambda: session_driver(window, app))
    return app.exec()
