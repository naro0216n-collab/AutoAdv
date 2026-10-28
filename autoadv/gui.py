"""A single GUI integrating game profiles, LunaHook, Irodori, and advancement."""
from __future__ import annotations
import os
import sys
import subprocess
import copy
import ntpath
from pathlib import Path
import re

from .model import Token
from .hook_protocol import HookEvent, ThreadSource
from .model import (AdvanceMode, InputMethod, Profile, SpeakerStyle, StructureRule, ReplacementRule,
                    COMMON_FIELDS, SPEAKER_STYLE_OPTIONS, build_speaker_caption,
                    configured_speaker_names, interpret)
from .storage import load, save
from .import_luna import inspect_luna, add_luna_games
from .game_windows import find_game_windows, find_game_processes, WindowCandidate
from .running_target_dialog import choose_running_target
from .application import AutoAdvApplication


def run_gui() -> int:
    from PySide6.QtCore import QObject, Signal, QTimer, Qt
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                                   QHBoxLayout, QFormLayout, QLineEdit, QPlainTextEdit,
                                   QLabel, QPushButton, QComboBox, QFileDialog,
                                   QSpinBox, QGroupBox, QMessageBox, QCheckBox, QScrollArea, QInputDialog,
                                   QTableWidget, QTableWidgetItem, QAbstractItemView, QTabWidget, QHeaderView, QToolButton, QMenu, QButtonGroup, QFrame)

    class SignalBridge(QObject):
        voice = Signal(object, bool, str)
        server = Signal(object)
        preview = Signal(bool, str)
        runtime_changed = Signal()
        runtime_event = Signal(str, object, object)
        runtime_schedule = Signal(int, object)

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('AutoAdv')
            self.resize(1180, 900)
            catalog = load()
            self.application = AutoAdvApplication(catalog, self.persist)
            self.bridge = SignalBridge()
            self.bridge.voice.connect(self.on_voice)
            self.bridge.server.connect(self.on_server)
            self.bridge.preview.connect(self.on_preview)
            self.bridge.runtime_changed.connect(self.refresh_status)
            self.bridge.runtime_event.connect(self.on_runtime_event)
            self.bridge.runtime_schedule.connect(self.schedule_runtime_callback)
            self.application.configure_runtime_loop(
                schedule=lambda delay, callback: self.bridge.runtime_schedule.emit(delay, callback),
                on_change=self.bridge.runtime_changed.emit,
                on_event=lambda event, token, detail: self.bridge.runtime_event.emit(event, token, detail))
            self.application.configure_voice_jobs(
                on_result=lambda token, done, reason: self.bridge.voice.emit(token, done, reason),
                on_preview=lambda done, reason: self.bridge.preview.emit(done, reason))
            self._loading_profile = False
            self.source_rows: dict[tuple[str, int], int] = {}
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
                QFrame[feedbackPanel="true"] { border: 1px solid palette(mid); border-radius: 6px; }
                QFrame[feedbackPanel="true"] QLabel { border: none; padding: 0; }
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
            self.game_tab_index = 1
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

            def fixed_feedback_panel(message_label, action_button=None):
                """Reserve a stable status area even when no message is currently present."""
                panel = QFrame()
                panel.setProperty('feedbackPanel', True)
                layout = QVBoxLayout(panel)
                layout.setContentsMargins(8, 8, 8, 8)
                layout.setSpacing(6)
                message_label.setWordWrap(True)
                message_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                layout.addWidget(message_label, 1)
                if action_button is not None:
                    action_slot = QWidget()
                    action_layout = QHBoxLayout(action_slot)
                    action_layout.setContentsMargins(0, 0, 0, 0)
                    action_layout.addWidget(action_button)
                    action_layout.addStretch(1)
                    action_slot.setMinimumHeight(34)
                    layout.addWidget(action_slot)
                panel.setMinimumHeight(92 if action_button is not None else 54)
                return panel

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
            self.play_issue_action = QPushButton('')
            self.play_issue_action.clicked.connect(self.open_play_issue_action)
            self.play_issue_action.hide()
            self.play_issue_panel = fixed_feedback_panel(self.play_issue, self.play_issue_action)
            live_dialogue.addRow(self.play_issue_panel)
            self._play_issue_action = None
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
            self.input_method = QComboBox()
            self.input_method.addItem('ウィンドウ指定（非アクティブ可）', InputMethod.WINDOW_MESSAGE.value)
            self.input_method.addItem('キーボード入力（前面時）', InputMethod.SEND_INPUT.value)
            advance.addRow('キー送信方式', setting_row('input_method', self.input_method))
            self.input_key_status = QLabel('')
            advance.addRow('送りキー', self.input_key_status)
            self.input_status = QLabel('')
            self.input_status.setWordWrap(True)
            advance.addRow('キー送信先', self.input_status)
            self.input_test_status = QLabel('未確認')
            self.input_test_status.setWordWrap(True)
            advance.addRow('動作確認', self.input_test_status)
            self.btn_input_test = QPushButton('入力をテスト')
            self.btn_input_test.clicked.connect(self.test_input)
            advance.addRow(self.btn_input_test)

            capture_box = QGroupBox('会話取得')
            capture_outer = QVBoxLayout(capture_box)
            self.capture_hint = QLabel('ゲームで台詞を進めると、取得元と実サンプルが自動的に追加されます。')
            self.capture_hint.setWordWrap(True)
            self.capture_hint.setMinimumHeight(24)
            capture_outer.addWidget(self.capture_hint)
            workspace = QHBoxLayout()

            source_column = QVBoxLayout()
            source_column.addWidget(QLabel('1. 取得元'))
            self.source_table = QTableWidget(0, 2)
            self.source_table.setHorizontalHeaderLabels(['取得されたテキスト', '採用先'])
            self.source_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.source_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.source_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_table.setMinimumHeight(300)
            self.source_table.verticalHeader().setVisible(False)
            self.source_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.source_table.setColumnWidth(1, 90)
            self.source_table.itemSelectionChanged.connect(self.show_hook_preview)
            source_column.addWidget(self.source_table, 1)
            workspace.addLayout(source_column, 3)

            format_column = QVBoxLayout()
            format_column.addWidget(QLabel('2. 選択した取得元の整形'))
            format_help = QLabel('この取得元から話者候補・本文候補を作ります。どちらを最終会話へ採用するかは右側で選びます。')
            format_help.setWordWrap(True)
            format_column.addWidget(format_help)
            self.capture_format_source = QLabel('取得元を選択してください')
            self.capture_format_source.setWordWrap(True)
            format_column.addWidget(self.capture_format_source)
            self.capture_format = QComboBox()
            for label, value in (
                    ('構造を自動判定', 'auto'),
                    ('原文全体を本文候補にする', 'body'),
                    ('原文全体を話者候補にする', 'speaker'),
                    ('話者「本文」', 'quoted'),
                    ('話者：本文', 'colon'),
                    ('話者の次行に本文', 'newline'),
                    ('話者 + 括弧情報 + 本文', 'metadata'),
                    ('「本文」のみ', 'quoted_body')):
                self.capture_format.addItem(label, value)
            self.capture_format.currentIndexChanged.connect(self.capture_format_changed)
            format_column.addWidget(self.capture_format)
            self.source_extraction_preview = QTableWidget(0, 3)
            self.source_extraction_preview.setHorizontalHeaderLabels(['原文', '話者候補', '本文候補'])
            self.source_extraction_preview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.source_extraction_preview.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.source_extraction_preview.verticalHeader().setVisible(False)
            self.source_extraction_preview.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.source_extraction_preview.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.source_extraction_preview.setMinimumHeight(300)
            format_column.addWidget(self.source_extraction_preview, 1)
            self.btn_advanced_extraction = QPushButton('詳細な抽出規則')
            self.btn_advanced_extraction.clicked.connect(lambda: self.reveal_section('extraction', 'game'))
            format_column.addWidget(self.btn_advanced_extraction)
            workspace.addLayout(format_column, 4)

            assembly_column = QVBoxLayout()
            assembly_column.addWidget(QLabel('3. 会話の組み立て'))
            assembly_form = QFormLayout()
            self.capture_body_source = QComboBox()
            self.capture_body_source.currentIndexChanged.connect(self.capture_body_source_changed)
            self.capture_speaker_source = QComboBox()
            self.capture_speaker_source.currentIndexChanged.connect(self.capture_speaker_source_changed)
            assembly_form.addRow('本文を使う取得元', self.capture_body_source)
            assembly_form.addRow('話者を使う取得元', self.capture_speaker_source)
            self.capture_unwrap_speaker_body = QCheckBox('話者名 + 囲み本文は本文だけを使う')
            self.capture_unwrap_speaker_body.setToolTip(
                '取得した話者、または既存の話者設定と本文先頭が一致する場合だけ、話者（本文）・話者「本文」・話者『本文』の囲み部分を本文にします。')
            self.capture_unwrap_speaker_body.toggled.connect(self.capture_unwrap_speaker_body_changed)
            assembly_form.addRow('', self.capture_unwrap_speaker_body)
            assembly_column.addLayout(assembly_form)
            assembly_column.addWidget(QLabel('最終結果プレビュー'))
            self.capture_result_table = QTableWidget(0, 2)
            self.capture_result_table.setHorizontalHeaderLabels(['話者', '本文'])
            self.capture_result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.capture_result_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.capture_result_table.verticalHeader().setVisible(False)
            self.capture_result_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.capture_result_table.setColumnWidth(0, 110)
            self.capture_result_table.setMinimumHeight(245)
            assembly_column.addWidget(self.capture_result_table, 1)
            self.capture_problem = QLabel('')
            self.capture_recovery_action = QPushButton('取得の復旧を開く')
            self.capture_recovery_action.clicked.connect(lambda: self.reveal_section('hook_details', 'game'))
            self.capture_recovery_action.hide()
            self.capture_problem_panel = fixed_feedback_panel(
                self.capture_problem, self.capture_recovery_action)
            assembly_column.addWidget(self.capture_problem_panel)
            self.btn_commit_capture = QPushButton('この会話取得を使用')
            self.btn_commit_capture.setProperty('primary', True)
            self.btn_commit_capture.clicked.connect(self.commit_capture_workspace)
            self.btn_commit_capture.setEnabled(False)
            assembly_column.addWidget(self.btn_commit_capture)
            workspace.addLayout(assembly_column, 3)
            capture_outer.addLayout(workspace)
            pages['game'].addWidget(capture_box)
            self.ui_sections['capture'] = capture_box
            self.ui_sections['source_format'] = capture_box

            candidate_details = group('選択した取得元の技術情報', page='game', section='candidate_details', detail=True)
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

            speech = group('話者・音声', page='game', section='speakers')
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
            self.speaker_table.itemSelectionChanged.connect(self.refresh_selected_speaker_style)
            speech.addRow(self.speaker_table)
            add_speaker = QWidget(); add_speaker_layout = QHBoxLayout(add_speaker); add_speaker_layout.setContentsMargins(0,0,0,0)
            self.new_speaker_name = QLineEdit(); self.new_speaker_name.setPlaceholderText('話者')
            self.new_speaker_tts = QCheckBox('TTS'); self.new_speaker_tts.setChecked(True)
            self.new_speaker_voice = QComboBox(); self.new_speaker_voice.setEditable(True)
            self.btn_add_speaker = QPushButton('追加'); self.btn_add_speaker.clicked.connect(self.add_speaker_inline)
            for w, stretch in ((self.new_speaker_name,2),(self.new_speaker_tts,0),(self.new_speaker_voice,2),(self.btn_add_speaker,0)): add_speaker_layout.addWidget(w, stretch)
            speech.addRow(add_speaker)

            speaker_style_box = QGroupBox('選択中の話者のIrodori表現')
            speaker_style_form = QFormLayout(speaker_style_box)
            self.speaker_style_controls = {}
            style_labels = {
                'age': '年齢感', 'gender': '声の性別感', 'pitch': '声の高さ',
                'timbre': '声質', 'demeanor': '性格・雰囲気', 'delivery': '話し方',
                'pace': '話速', 'intonation': '抑揚',
            }
            for field_name, label in style_labels.items():
                control = QComboBox()
                for value, display in SPEAKER_STYLE_OPTIONS[field_name]:
                    control.addItem(display, value)
                control.currentIndexChanged.connect(
                    lambda _index, field=field_name, combo=control:
                    self.speaker_style_changed(field, combo.currentData() or ''))
                self.speaker_style_controls[field_name] = control
                speaker_style_form.addRow(label, control)
            self.speaker_caption_preview = QLabel('話者を選択すると、Irodoriへ送るCaptionを確認できます。')
            self.speaker_caption_preview.setWordWrap(True)
            speaker_style_form.addRow('Caption', self.speaker_caption_preview)
            speech.addRow(speaker_style_box)
            self.audition_voice_choice = QComboBox(); self.audition_voice_choice.setEditable(True)
            speech.addRow('試聴Voice', self.audition_voice_choice)
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

            rule_preview = group('読み上げ・原音照合プレビュー', page='game', section='live_preview', detail=True)
            self.example_raw = QPlainTextEdit(); self.example_raw.setPlaceholderText('本文例。会話取得の整形とは独立して、読み・置換結果だけを確認します'); self.example_raw.setMaximumHeight(80)
            self.example_raw.textChanged.connect(self.refresh_rule_preview); rule_preview.addRow('本文例', self.example_raw)
            self.editor_preview = QPlainTextEdit(); self.editor_preview.setReadOnly(True); self.editor_preview.setMinimumHeight(110); rule_preview.addRow(self.editor_preview)

            extraction = group('選択した取得元の詳細な抽出規則', page='game', section='extraction', detail=True)
            self.structure_table=QTableWidget(0,4); self.structure_table.setHorizontalHeaderLabels(['優先','有効','適用条件','抽出パターン']); self.configure_editor_table(self.structure_table); self.structure_table.itemSelectionChanged.connect(self.show_structure_rule); extraction.addRow(self.structure_table)
            self.structure_template=QComboBox()
            for label,value in (('よくある形式を選ぶ',''),('本文のみ',r'^\s*(?P<body>.*)\s*$'),('話者名のみ',r'^\s*(?P<speaker>.*?)\s*$'),('話者「本文」',r'^\s*(?P<speaker>[^「」]+)「(?P<body>.*?)」\s*$'),('話者：本文',r'^\s*(?P<speaker>[^：:\n]+)[：:]\s*(?P<body>.+)$'),('話者の次行に本文',r'^\s*(?P<speaker>[^\n]+)\n(?P<body>.+)$'),('話者 + 括弧情報 + 本文',r'^\s*(?P<speaker>\S{1,40})\s+\([^\n)]{1,80}\)\s*(?P<body>.+)$'),('「本文」のみ',r'^\s*「(?P<body>.*?)」\s*$')): self.structure_template.addItem(label,value)
            self.btn_apply_template=QPushButton('この形式を使う'); self.btn_apply_template.clicked.connect(self.use_structure_template); extraction.addRow(self.structure_template,self.btn_apply_template)
            self.structure_pattern=QLineEdit(); self.structure_condition=QLineEdit(); self.structure_enabled=QCheckBox('有効'); self.structure_enabled.setChecked(True)
            extraction.addRow('抽出規則',self.structure_pattern); extraction.addRow('適用条件',self.structure_condition); extraction.addRow(self.structure_enabled)
            row=QWidget(); lay=QHBoxLayout(row); lay.setContentsMargins(0,0,0,0)
            self.btn_structure=QPushButton('プレビューに反映'); self.btn_structure.clicked.connect(self.add_structure_rule); self.btn_structure_new=QPushButton('新規'); self.btn_structure_new.clicked.connect(self.clear_structure_editor); self.btn_structure_up=QPushButton('↑'); self.btn_structure_up.clicked.connect(self.raise_structure_rule); self.btn_structure_down=QPushButton('↓'); self.btn_structure_down.clicked.connect(self.lower_structure_rule); self.btn_structure_remove=QPushButton('削除'); self.btn_structure_remove.clicked.connect(self.remove_structure_rule)
            for b in (self.btn_structure,self.btn_structure_new,self.btn_structure_up,self.btn_structure_down,self.btn_structure_remove): lay.addWidget(b)
            extraction.addRow(row)

            replacement = group('読み上げ・原音照合の詳細置換', page='game', section='replacement', detail=True)
            self.replacement_table=QTableWidget(0,5); self.replacement_table.setHorizontalHeaderLabels(['有効','一致対象','置換後','適用先','正規表現']); self.configure_editor_table(self.replacement_table); self.replacement_table.itemSelectionChanged.connect(self.show_replacement_rule); replacement.addRow(self.replacement_table)
            self.replace_pattern=QLineEdit(); self.replace_value=QLineEdit(); self.replace_target=QComboBox(); self.replace_target.addItem('読み上げ(TTS)','tts'); self.replace_target.addItem('原音照合','match'); self.replace_target.addItem('両方','both'); self.replace_regex=QCheckBox('正規表現'); self.replace_enabled=QCheckBox('有効'); self.replace_enabled.setChecked(True)
            replacement.addRow('対象',self.replace_pattern); replacement.addRow('置換後',self.replace_value); replacement.addRow('適用先',self.replace_target); replacement.addRow(self.replace_regex); replacement.addRow(self.replace_enabled)
            row=QWidget(); lay=QHBoxLayout(row); lay.setContentsMargins(0,0,0,0)
            self.btn_replace=QPushButton('置換を追加・変更'); self.btn_replace.clicked.connect(self.add_replacement); self.btn_replace_new=QPushButton('新規'); self.btn_replace_new.clicked.connect(self.clear_replacement_editor); self.btn_replacement_remove=QPushButton('削除'); self.btn_replacement_remove.clicked.connect(self.remove_replacement); self.btn_replace_up=QPushButton('↑'); self.btn_replace_up.clicked.connect(lambda: self.move_replacement_rule(-1)); self.btn_replace_down=QPushButton('↓'); self.btn_replace_down.clicked.connect(lambda: self.move_replacement_rule(1))
            for b in (self.btn_replace,self.btn_replace_new,self.btn_replacement_remove,self.btn_replace_up,self.btn_replace_down): lay.addWidget(b)
            replacement.addRow(row)

            game_settings = group('この作品の進行設定', page='game', section='advance_details', detail=True)
            self.uncertain_text_fallback=QCheckBox('音声判定が不確かなとき本文時間で進む')
            self.force_advance_seconds=QSpinBox(); self.force_advance_seconds.setRange(1,86400); self.force_advance_seconds.setSuffix(' 秒')
            self.per_reading_unit_wait_ms=QSpinBox(); self.per_reading_unit_wait_ms.setRange(1,600000); self.per_reading_unit_wait_ms.setSuffix(' ms / 読み単位')
            self.reading_ms=QSpinBox(); self.reading_ms.setRange(1,600000); self.reading_ms.setSuffix(' ms')
            self.wait_preview=QLabel('')
            self.input_key=QSpinBox(); self.input_key.setRange(1,254)
            self.repeat=QComboBox(); self.repeat.addItem('連続同文は重複として扱う','distinct_only'); self.repeat.addItem('連続同文は各flushを別台詞として扱う','each_flush')
            game_settings.addRow(setting_row('uncertain_text_fallback', self.uncertain_text_fallback)); game_settings.addRow('強制進行までの最大待機',setting_row('force_advance_seconds', self.force_advance_seconds)); game_settings.addRow('1読み単位待機',setting_row('per_reading_unit_wait_ms', self.per_reading_unit_wait_ms)); game_settings.addRow('本文待機の最低時間',setting_row('reading_ms', self.reading_ms)); game_settings.addRow('送りキー',setting_row('input_key', self.input_key)); game_settings.addRow('同文',self.repeat); game_settings.addRow(self.wait_preview)

            target_details = group('別プロセスを使う作品', page='game', section='target_details', detail=True)
            self.game_exe=QLineEdit(); self.audio_exe=QLineEdit(); self.input_exe=QLineEdit()
            target_details.addRow('ゲームEXE',self.game_exe); target_details.addRow('音声EXE',self.audio_exe); target_details.addRow('入力EXE',self.input_exe)

            audio_details = group('音声判断環境', page='game', section='audio_details', detail=True)
            self.native_helper=QLineEdit(); self.alignment_model=QLineEdit(); self.alignment_vocab=QLineEdit()
            from .native_runtime import managed_paths
            managed_native = managed_paths()
            self.native_helper.setPlaceholderText(managed_native.helper_dll)
            self.alignment_model.setPlaceholderText(managed_native.alignment_model)
            self.alignment_vocab.setPlaceholderText(managed_native.alignment_vocab)
            audio_details.addRow('Process Loopback helper 上書き',setting_row('native_helper', self.native_helper)); audio_details.addRow('音響整列モデル 上書き',setting_row('alignment_model', self.alignment_model)); audio_details.addRow('音響整列語彙 上書き',setting_row('alignment_vocab', self.alignment_vocab))

            environment = group('Irodori', page='settings', section='irodori')
            self.irodori_path=QLineEdit(self.catalog.irodori_root); self.irodori_url=QLineEdit(self.catalog.irodori_url)
            environment.addRow('サーバーフォルダ',self.irodori_path); environment.addRow('接続先',self.irodori_url)
            self.server_status=QLabel('')
            self.btn_server=QPushButton('接続を再試行')
            self.btn_server.clicked.connect(self.start_server)
            self.btn_server.hide()
            self.server_status_panel = fixed_feedback_panel(self.server_status, self.btn_server)
            environment.addRow(self.server_status_panel)

            common = group('共通の既定値', page='settings', section='common_advance')
            self.common_controls={}
            common_titles={'advance_mode':'自動進行方式','uncertain_text_fallback':'音声判定が不確かなとき本文時間で進む','force_advance_seconds':'強制進行までの最大待機 (秒)','default_tts':'未知話者にもIrodori TTSを使用','default_voice':'既定Voice','reading_ms':'本文待機の最低時間 (ms)','per_reading_unit_wait_ms':'1読み単位あたり待機時間 (ms)','native_helper':'Process Loopbackヘルパー','alignment_model':'日本語音響整列モデル','alignment_vocab':'音響整列語彙ファイル','input_key':'送りキーコード','input_method':'キー送信方式'}
            for key in common_titles:
                value=getattr(self.catalog.common,key)
                if key=='advance_mode':
                    control=QWidget(); lay=QHBoxLayout(control); lay.setContentsMargins(0,0,0,0); mode_group=QButtonGroup(self); mode_group.setExclusive(True); text_button=QPushButton('文字のみ判断'); audio_button=QPushButton('音声判断')
                    for button,mode in ((text_button,AdvanceMode.TEXT),(audio_button,AdvanceMode.AUDIO)): button.setCheckable(True); button.setProperty('advanceMode',mode.value); mode_group.addButton(button); lay.addWidget(button,1)
                    (audio_button if value==AdvanceMode.AUDIO else text_button).setChecked(True); self.common_advance_mode_group=mode_group; self.common_advance_text_button=text_button; self.common_advance_audio_button=audio_button
                elif key=='input_method':
                    control=QComboBox(); control.addItem('ウィンドウ指定（非アクティブ可）',InputMethod.WINDOW_MESSAGE.value); control.addItem('キーボード入力（前面時）',InputMethod.SEND_INPUT.value); control.setCurrentIndex(max(0,control.findData(InputMethod(value).value)))
                elif isinstance(value,bool): control=QCheckBox('有効'); control.setChecked(value)
                elif isinstance(value,int):
                    control=QSpinBox()
                    control.setRange(1,254 if key=='input_key' else (86400 if key=='force_advance_seconds' else 600000))
                    if key=='force_advance_seconds': control.setSuffix(' 秒')
                    control.setValue(value)
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
            self.btn_import=QPushButton('Luna設定を取り込む'); self.btn_import.clicked.connect(self.import_luna)
            management.addRow(self.btn_import)

            diagnostics = group('診断', page='settings', section='diagnostics', detail=True)
            self.diagnostics_snapshot=QPlainTextEdit(); self.diagnostics_snapshot.setReadOnly(True); self.diagnostics_snapshot.setMaximumHeight(180)
            diagnostics.addRow('現在の内部状態', self.diagnostics_snapshot)
            self.log=QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(220); diagnostics.addRow('履歴', self.log)
            self.btn_host=QPushButton('LunaHook Hostを再起動'); self.btn_host.clicked.connect(self.start_host)
            diagnostics.addRow(self.btn_host)
            self.section_toggles['diagnostics'].toggled.connect(lambda checked: checked and self.refresh_diagnostics())

            self.profile_field_controls={'advance_mode':self.advance_mode_widget,'uncertain_text_fallback':self.uncertain_text_fallback,'force_advance_seconds':self.force_advance_seconds,'default_tts':self.default_tts,'default_voice':self.default_voice,'reading_ms':self.reading_ms,'per_reading_unit_wait_ms':self.per_reading_unit_wait_ms,'native_helper':self.native_helper,'alignment_model':self.alignment_model,'alignment_vocab':self.alignment_vocab,'input_key':self.input_key,'input_method':self.input_method}

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

            self.fill_games()
            if self.irodori_url.text().strip(): QTimer.singleShot(0,self.start_server)
            self._input_status_timer = QTimer(self)
            self._input_status_timer.timeout.connect(self.refresh_input_status)
            self._input_status_timer.start(500)
            for layout in pages.values(): layout.addStretch(1)

        @property
        def catalog(self):
            return self.application.catalog

        @property
        def game_id(self):
            return self.application.game_id

        @property
        def profile_id(self):
            return self.application.profile_id

        def refresh_diagnostics(self):
            toggle = self.section_toggles.get('diagnostics')
            if toggle is not None and not toggle.isChecked():
                return
            self.diagnostics_snapshot.setPlainText(self.application.diagnostics_view().text)

        def note(self, message):
            self.log.appendPlainText(message)
            self._last_note = message
            actionable = ('失敗', 'できません', 'できない', '必要', '選択してください',
                          '保留', '衝突', '停止', '利用でき')
            if any(word in message for word in actionable):
                self.statusBar().showMessage(message, 12000)
            self.refresh_diagnostics()

        def common_control_value(self, key: str):
            if key == 'advance_mode':
                return AdvanceMode.AUDIO if self.common_advance_audio_button.isChecked() else AdvanceMode.TEXT
            widget = self.common_controls[key]
            if key == 'input_method':
                return InputMethod(widget.currentData())
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
                if profile and self.application.effective_setting(key)[1] == 'common':
                    self.refresh_inherited_control(key)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('共通設定を保存できません: ' + str(exc))

        def persist(self):
            save(self.application.catalog)

        def fill_games(self):
            self.game_choice.blockSignals(True)
            self.game_choice.clear()
            for gid, game in self.catalog.games.items():
                self.game_choice.addItem(game.name, gid)
            self.game_choice.blockSignals(False)
            self.choose_game()

        def current_profile(self) -> Profile | None:
            return self.application.current_profile()

        def choose_game(self, *_):
            self.stop_session()
            self.application.close_hook_host()
            self.clear_hook_observations()
            self.application.clear_runtime_target()
            game_id = self.game_choice.currentData() or ''
            self.application.select_profile(game_id, '')
            self.profile_choice.blockSignals(True)
            self.profile_choice.clear()
            game = self.catalog.games.get(game_id)
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
            if sys.platform != 'win32' or not self.application.target_resolution_needed():
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
            self.refresh_source_workspace_mode(source)

        def refresh_source_workspace_mode(self, source: str | None = None):
            source = source or self.current_source_mode()
            is_hook = source == 'hook'
            if hasattr(self, 'source_table'):
                self.source_table.setVisible(is_hook)
                self.btn_commit_capture.setVisible(is_hook)
            for section in ('candidate_details', 'hook_details'):
                widget = self.ui_sections.get(section)
                if widget is not None:
                    widget.setVisible(is_hook)
            if hasattr(self, 'capture_hint'):
                if is_hook:
                    self.capture_hint.setText('ゲームで台詞を進めると、取得候補が自動的に追加されます。')
                else:
                    self.capture_hint.setText('OCRで取得した会話は右のプレビューへ自動的に反映されます。')
                    self.capture_hint.show()

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

        def test_input(self):
            if QMessageBox.question(
                    self, '入力をテスト',
                    '実際にゲームへ送りキーを1回送るため、会話が進む可能性があります。\n'
                    'キーボード入力方式では、実行後5秒以内にゲームを前面にしてください。\n\n'
                    'テスト入力を送りますか？') != QMessageBox.StandardButton.Yes:
                return
            self.application.test_input()
            self.refresh_input_status()

        @staticmethod
        def input_key_text(key: int) -> str:
            names = {0x08: 'Backspace', 0x09: 'Tab', 0x0D: 'Enter', 0x1B: 'Esc', 0x20: 'Space'}
            if key in names:
                return f'{names[key]} (VK {key})'
            if 0x30 <= key <= 0x39 or 0x41 <= key <= 0x5A:
                return f'{chr(key)} (VK {key})'
            return f'VK {key}'

        def refresh_input_status(self):
            if not hasattr(self, 'input_status'):
                return
            view = self.application.play_view().input
            self.input_key_status.setText(self.input_key_text(view.key))
            self.input_status.setText(view.status)
            self.input_test_status.setText(view.test_status or '未確認')
            self.btn_input_test.setEnabled(view.can_test)

        def profile_setting_changed(self, key: str):
            if self._loading_profile:
                return
            if self.current_profile() is None:
                return
            try:
                value = self.current_source_mode() if key == 'source' else self.profile_control_value(key)
                self.application.set_profile_setting(key, value)
                if key in COMMON_FIELDS:
                    self.refresh_inherited_control(key)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('設定を保存できません: ' + str(exc))
                self.choose_profile()

        def profile_control_value(self, key: str):
            if key == 'advance_mode':
                return self.current_advance_mode()
            control = self.profile_field_controls[key]
            if key == 'input_method':
                return InputMethod(control.currentData())
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
                if key == 'input_method':
                    control.setCurrentIndex(max(0, control.findData(InputMethod(value).value)))
                elif isinstance(control, QCheckBox):
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
            if self._loading_profile or self.current_profile() is None:
                return
            try:
                self.application.reset_profile_setting(key)
                self.refresh_inherited_control(key)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('共通設定へ戻せません: ' + str(exc))

        def save_capture_environment(self):
            """Persist the related acquisition environment, never unrelated UI fields."""
            if self._loading_profile or self.current_profile() is None:
                return
            try:
                self.application.set_capture_environment(
                    ocr_executable=self.ocr_exe.text(),
                    ocr_language=self.ocr_lang.text(),
                    ocr_region=tuple(int(x.strip()) for x in self.ocr_region.text().split(',')),
                    ocr_stable_samples=self.ocr_stability.value(),
                    insert_pc_hooks=self.insert_pc_hooks_on_attach.isChecked(),
                    hook_flush_ms=self.hook_flush.value(),
                    hook_codepage=self.hook_codepage.value(),
                    hook_max_buffer=self.hook_buffer.value(),
                    hook_max_history=self.hook_history.value())
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('取得設定を保存できません: ' + str(exc))

        def repeat_setting_changed(self, *_):
            if self._loading_profile:
                return
            current = self.current_profile()
            if current is None:
                return
            try:
                self.application.set_profile_setting('repeat_rule', self.repeat.currentData())
            except Exception as exc:
                self.note('同文設定を保存できません: ' + str(exc))

        def game_executables_changed(self):
            if self._loading_profile or not self.game_id:
                return
            try:
                self.application.update_game_executables(
                    self.game_exe.text(), self.audio_exe.text(), self.input_exe.text())
                if self.application.runtime_target_state().selected:
                    self.resolve_associated_targets()
            except Exception as exc:
                self.note('作品の実行対象を保存できません: ' + str(exc))

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
            self.stop_session()
            profile_id = self.profile_choice.currentData() or ''
            profile = self.application.select_profile(self.game_id, profile_id)
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
            self.force_advance_seconds.setValue(effective.force_advance_seconds)
            self.per_reading_unit_wait_ms.setValue(effective.per_reading_unit_wait_ms)
            self.default_voice.setEditText(effective.default_voice)
            self.audition_voice_choice.setEditText(effective.default_voice)
            self.reading_ms.setValue(effective.reading_ms)
            self.insert_pc_hooks_on_attach.setChecked(profile.insert_pc_hooks)
            self.hook_flush.setValue(profile.hook_flush_ms)
            self.hook_codepage.setValue(profile.hook_codepage)
            self.hook_buffer.setValue(profile.hook_max_buffer)
            self.hook_history.setValue(profile.hook_max_history)
            self.input_key.setValue(effective.input_key)
            self.input_method.setCurrentIndex(max(0,self.input_method.findData(InputMethod(effective.input_method).value)))
            self.repeat.setCurrentIndex(self.repeat.findData(profile.repeat_rule))
            self.set_source_mode(profile.source)
            self.ocr_exe.setText(profile.ocr.executable)
            self.ocr_lang.setText(profile.ocr.language)
            self.ocr_region.setText(','.join(map(str, profile.ocr.region)))
            self.ocr_stability.setValue(profile.ocr.stable_samples)
            self.native_helper.setText(effective.native_helper)
            self.alignment_model.setText(effective.alignment_model)
            self.alignment_vocab.setText(effective.alignment_vocab)
            for field in COMMON_FIELDS:
                self.refresh_inherited_control(field)
            self.refresh_rules()
            self.refresh_speaker_choices()
            # Editors must never carry the previous game's unsaved draft.
            self.clear_structure_editor()
            self.clear_replacement_editor()
            self._preview_edit_kind = None
            self.refresh_rule_preview()
            self._loading_profile = False
            self.refresh_capture_workspace()
            if self.application.runtime_target_state().selected:
                QTimer.singleShot(0, self.connect_game)
            else:
                QTimer.singleShot(0, self.auto_attach_selected_game)

        def add_game(self):
            try:
                game = self.application.create_game(self.new_game.text(), self.game_exe.text().strip())
                self.fill_games()
                self.game_choice.setCurrentIndex(self.game_choice.findData(game.id))
            except Exception as exc:
                self.note(str(exc))

        def add_profile(self):
            if not self.game_id:
                return
            try:
                p = self.application.add_profile(self.new_profile.text())
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

        def add_speaker_inline(self):
            name = self.new_speaker_name.text().strip()
            try:
                self.application.add_speaker(
                    name, self.new_speaker_tts.isChecked(), self.new_speaker_voice.currentText())
                self.new_speaker_name.clear()
                self.new_speaker_voice.setEditText('')
                self.refresh_speaker_choices(name)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('話者設定を追加できません: ' + str(exc))

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
            if not self.current_profile() or not name:
                self.refresh_speaker_choices(old if self.current_profile() else None)
                return
            try:
                if item.column() == 0 and name != old:
                    self.application.rename_speaker(old, name)
                    self.refresh_speaker_choices(name)
                elif item.column() == 1:
                    self.application.set_speaker_tts(
                        name, item.checkState() == Qt.CheckState.Checked)
                    self.refresh_speaker_choices(name)
                else:
                    return
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('話者設定を変更できません: ' + str(exc))
                self.refresh_speaker_choices(old)

        def speaker_voice_changed(self, name: str, voice: str):
            if getattr(self, '_refreshing_speakers', False):
                return
            p = self.current_profile()
            if not p or name not in configured_speaker_names(p):
                return
            if p.speaker_voices.get(name, '') == voice.strip():
                return
            try:
                # Do not rebuild the table from inside QComboBox.currentTextChanged.
                # Replacing the active combo during its own selection event can leave
                # the adjacent preview action bound to a stale widget/selection.
                self.application.set_speaker_voice(name, voice)
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('話者のVoiceを変更できません: ' + str(exc))
                self.refresh_speaker_choices(name)

        def selected_speaker_name(self) -> str:
            row = self.speaker_table.currentRow()
            if row < 0:
                return ''
            item = self.speaker_table.item(row, 0)
            return item.text().strip() if item is not None else ''

        def refresh_selected_speaker_style(self):
            p = self.current_profile()
            name = self.selected_speaker_name()
            style = p.speaker_styles.get(name, SpeakerStyle()) if p and name else SpeakerStyle()
            self._refreshing_speaker_style = True
            try:
                for field_name, control in self.speaker_style_controls.items():
                    control.setEnabled(bool(p and name))
                    index = control.findData(getattr(style, field_name))
                    control.setCurrentIndex(index if index >= 0 else 0)
            finally:
                self._refreshing_speaker_style = False
            if not p or not name:
                self.speaker_caption_preview.setText(
                    '話者を選択すると、Irodoriへ送るCaptionを確認できます。')
                return
            caption = build_speaker_caption(style)
            self.speaker_caption_preview.setText(caption or '未設定（Captionは送信しません）')

        def speaker_style_changed(self, field_name: str, value: str):
            if getattr(self, '_refreshing_speaker_style', False):
                return
            p = self.current_profile()
            name = self.selected_speaker_name()
            if not p or not name or name not in configured_speaker_names(p):
                self.refresh_selected_speaker_style()
                return
            current = p.speaker_styles.get(name, SpeakerStyle())
            try:
                style = SpeakerStyle(**{**vars(current), field_name: value})
                self.application.set_speaker_style(name, style)
                self.speaker_caption_preview.setText(
                    build_speaker_caption(style) or '未設定（Captionは送信しません）')
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('話者のIrodori表現を変更できません: ' + str(exc))
                self.refresh_selected_speaker_style()

        def audition_speaker_voice(self, name: str):
            p = self.current_profile()
            if p is None:
                return
            effective = self.catalog.effective_profile(p)
            caption = build_speaker_caption(p.speaker_styles.get(name, SpeakerStyle()))
            self.audition_voice_id(p.speaker_voices.get(name, effective.default_voice), caption)

        def delete_speaker_name(self, name: str):
            if not self.current_profile():
                return
            try:
                self.application.delete_speaker(name)
                self.refresh_speaker_choices()
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('話者を削除できません: ' + str(exc))

        def audition_voice_id(self, voice: str, caption: str = ''):
            voice = voice.strip() or self.default_voice.currentText().strip()
            try:
                if not voice:
                    raise ValueError('Voice IDを選択してください')
                # Keep the concrete Voice ID being auditioned visible.  Preview
                # never infers its source from whichever settings widget happened
                # to be edited most recently.
                self.audition_voice_choice.setEditText(voice)
                self.application.start_voice_preview(self.audition_text.text(), voice, caption)
            except Exception as exc:
                self.note('試聴できません: ' + str(exc))

        def refresh_speaker_choices(self, selected=None):
            p = self.current_profile()
            names = sorted(configured_speaker_names(p)) if p else []
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
                    audition.clicked.connect(lambda _=False, n=name: self.audition_speaker_voice(n))
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
            self.refresh_selected_speaker_style()
            self.refresh_rule_preview()

        def selected_capture_rule_source(self):
            source = self.chosen_source()
            if source is None:
                return None
            return source.identity, source.address

        def refresh_rules(self, focus=None, row=-1):
            p = self.current_profile()
            if not p:
                return
            source_key = self.selected_capture_rule_source()
            rules = self.application.capture_source_rules(source_key[0]) if source_key else ()
            self.fill_editor_table(self.structure_table, [
                (str(i + 1), '有効' if rule.enabled else '無効', rule.condition or '（常時）', rule.pattern)
                for i, rule in enumerate(rules)], row if focus == 'structure' else -1)
            self.structure_table.setEnabled(source_key is not None)
            self.btn_structure.setEnabled(source_key is not None)
            self.fill_editor_table(self.replacement_table, [
                ('有効' if rule.enabled else '無効', rule.pattern, rule.replacement,
                 {'tts': 'TTS', 'match': '原音照合', 'both': '両方'}[rule.target],
                 '正規表現' if rule.regex else '通常文字列')
                for rule in p.replacements], row if focus == 'replacement' else -1)
            self.refresh_reading_table()
            self.refresh_rule_preview()

        def show_structure_rule(self):
            row = self.selected_editor_row(self.structure_table)
            source_key = self.selected_capture_rule_source()
            rules = self.application.capture_source_rules(source_key[0]) if source_key else ()
            if 0 <= row < len(rules):
                rule = rules[row]
                self.structure_pattern.setText(rule.pattern)
                self.structure_condition.setText(rule.condition)
                self.structure_enabled.setChecked(rule.enabled)

        def use_structure_template(self):
            template = self.structure_template.currentData()
            if template:
                self.structure_pattern.setText(template)

        def clear_structure_editor(self):
            self.structure_table.clearSelection()
            self.structure_pattern.clear()
            self.structure_condition.clear()
            self.structure_enabled.setChecked(True)

        def move_structure_rule(self, movement):
            row = self.selected_editor_row(self.structure_table)
            source_key = self.selected_capture_rule_source()
            rules = self.application.capture_source_rules(source_key[0]) if source_key else ()
            if source_key is None or row < 0 or not 0 <= row + movement < len(rules):
                return
            try:
                self.application.move_capture_source_rule(source_key[0], source_key[1], row, movement)
                self.refresh_capture_workspace()
                self.refresh_rules('structure', row + movement)
            except Exception as exc:
                self.note('抽出規則の順序を変更できません: ' + str(exc))

        def raise_structure_rule(self):
            self.move_structure_rule(-1)

        def lower_structure_rule(self):
            self.move_structure_rule(1)

        def add_structure_rule(self):
            source_key = self.selected_capture_rule_source()
            if source_key is None:
                self.note('整形する取得元を一覧から選んでください')
                return
            row = self.selected_editor_row(self.structure_table)
            try:
                rule = StructureRule(self.structure_pattern.text().strip(),
                                     self.structure_condition.text().strip(),
                                     self.structure_enabled.isChecked())
                self.application.save_capture_source_rule(
                    source_key[0], source_key[1], row if row >= 0 else None, rule)
                self.refresh_capture_workspace()
                self.refresh_rules('structure', row if row >= 0 else 0)
            except Exception as exc:
                self.note('抽出規則を適用できません: ' + str(exc))

        def remove_structure_rule(self):
            source_key = self.selected_capture_rule_source()
            row = self.selected_editor_row(self.structure_table)
            if source_key is None or row < 0:
                self.note('削除する取得元の抽出規則を選んでください')
                return
            try:
                self.application.delete_capture_source_rule(source_key[0], source_key[1], row)
                self.refresh_capture_workspace()
                self.refresh_rules()
                self.clear_structure_editor()
            except Exception as exc:
                self.note('抽出規則を削除できません: ' + str(exc))

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
                self.application.save_replacement_rule(row if row >= 0 else None, rule)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
                self.refresh_rules('replacement', row if row >= 0 else self.replacement_table.rowCount())
            except Exception as exc:
                self.note('置換規則を保存できません: ' + str(exc))
                self.refresh_rule_preview()

        def move_replacement_rule(self, movement):
            row = self.selected_editor_row(self.replacement_table)
            p = self.current_profile()
            if p is None or row < 0 or not 0 <= row + movement < len(p.replacements):
                return
            try:
                self.application.move_replacement_rule(row, movement)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
                self.refresh_rules('replacement', row + movement)
            except Exception as exc:
                self.note('置換の適用順を変更できません: ' + str(exc))

        def remove_replacement(self):
            row = self.selected_editor_row(self.replacement_table)
            if row < 0:
                self.note('削除する置換規則を一覧から選んでください')
                return
            try:
                self.application.delete_replacement_rule(row)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
                self.refresh_rules()
                self.clear_replacement_editor()
            except Exception as exc:
                self.note('置換規則を削除できません: ' + str(exc))

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
            if not self.current_profile() or not word or not reading:
                self.refresh_reading_table(old if self.current_profile() else None)
                return
            try:
                self.application.update_reading(old, word, reading)
                self.refresh_reading_table(word)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('読みを変更できません: ' + str(exc))
                self.refresh_reading_table(old)

        def add_reading_inline(self):
            word = self.new_reading_word.text().strip()
            reading = self.new_reading_value.text().strip()
            try:
                self.application.add_reading(word, reading)
                self.new_reading_word.clear()
                self.new_reading_value.clear()
                self.refresh_reading_table(word)
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('読みを追加できません: ' + str(exc))

        def delete_reading_word(self, word: str):
            if not self.current_profile():
                return
            try:
                self.application.delete_reading(word)
                self.refresh_reading_table()
                self.refresh_capture_workspace()
                self.refresh_rule_preview()
            except Exception as exc:
                self.note('読みを削除できません: ' + str(exc))

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
            self.audition_voice_id(self.audition_voice_choice.currentText())

        def on_preview(self, success, reason):
            self.note('試聴が終了しました' if success else '試聴: ' + reason)

        def preview_from_control(self, control):
            if control in (self.replace_pattern, self.replace_value, self.replace_target,
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
            try:
                if kind == 'replacement' and self.replace_pattern.text():
                    rule = ReplacementRule(self.replace_pattern.text(), self.replace_value.text(),
                        self.replace_regex.isChecked(), self.replace_target.currentData(),
                        self.replace_enabled.isChecked())
                    row = self.selected_editor_row(self.replacement_table)
                    if row >= 0:
                        draft.replacements[row] = rule
                    else:
                        draft.replacements.append(rule)
                body = self.example_raw.toPlainText()
                speaker = ''
                assembly = self.application.capture_view().assembly
                if assembly and not self.example_raw.hasFocus():
                    speaker = assembly[-1].speaker
                trace = []
                u = interpret(body, draft, session=0, line=0, source='preview', sequence=0, g2p=self.application.g2p,
                              speaker_override=speaker, body_override=body, trace=trace)
                if u.use_tts:
                    tts_result = f'TTS: Voice {u.voice or "未設定"}'
                    if u.tts_caption:
                        tts_result += f' / Caption: {u.tts_caption}'
                else:
                    tts_result = 'TTS: 使用しない'
                self.editor_preview.setPlainText(
                    f'{tts_result}\n'
                    f'共通読み: {u.speech.resolved_text}\n'
                    f'TTS読み上げ: {u.speech.tts_text}\n'
                    f'原音照合: {u.speech.alignment_text}')
                self._preview_utterance = u
            except (ValueError, re.error, IndexError) as exc:
                self._preview_utterance = None
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

        def edit_current_dialogue(self):
            u = self.application.runtime_state().utterance
            if not u:
                self.note('台詞を取得してから編集を始めてください')
                return
            self.example_raw.setPlainText(u.text)
            self.tabs.setCurrentIndex(self.game_tab_index)

        def duplicate_profile(self):
            if self.current_profile():
                try:
                    p = self.application.duplicate_current_profile(
                        self.new_profile.text().strip() or self.current_profile().name + ' copy')
                    self.choose_game()
                    self.profile_choice.setCurrentIndex(self.profile_choice.findData(p.id))
                except Exception as exc:
                    self.note(str(exc))

        def rename_profile(self):
            if self.current_profile():
                try:
                    self.application.rename_current_profile(self.new_profile.text())
                    self.choose_game()
                except Exception as exc:
                    self.note(str(exc))

        def delete_profile(self):
            if self.current_profile() and QMessageBox.question(self, 'プロファイルを削除',
                    'このプロファイルと設定を削除しますか？') == QMessageBox.StandardButton.Yes:
                try:
                    self.application.delete_current_profile()
                    self.choose_game()
                except Exception as exc:
                    self.note(str(exc))

        def set_default_profile(self):
            if self.current_profile():
                try:
                    self.application.set_default_profile()
                except Exception as exc:
                    self.note('既定プロファイルを保存できません: ' + str(exc))

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
                self.application.install_catalog(proposed)
                self.fill_games()
                self.note(f'Imported {len(entries)} games')
            except Exception as exc:
                self.note(f'Import not applied: {exc}')

        def start_recovery_hook_search(self):
            try:
                game = self.catalog.games.get(self.game_id)
                module = Path(game.executable).name if game and game.executable else ''
                self.application.start_recovery_hook_search(module)
                self.note('追加の取得元を探索しています。台詞を表示すると、取得できた本文が同じ一覧に現れます。')
            except Exception as exc:
                self.note('HOOK探索を開始できません: ' + str(exc))

        def insert_pc_hooks(self):
            try:
                self.application.insert_pc_recovery_hooks()
                self.note('本家Lunaの標準文字API用HOOK追加を要求しました。ゲームで台詞を表示してください。')
            except (OSError, RuntimeError, ValueError) as exc:
                self.note('PC用HOOK追加に失敗: ' + str(exc))

        def insert_hook_code(self, code: str):
            if not self.current_profile() or not code:
                return
            try:
                self.application.insert_recovery_hook(code)
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
            for control in (self.default_voice, self.new_speaker_voice, self.audition_voice_choice):
                existing = control.currentText().strip()
                blocked = control.blockSignals(True)
                try:
                    control.clear()
                    control.addItems(list(view.voices))
                    if existing:
                        control.setEditText(existing)
                    elif control is self.audition_voice_choice:
                        profile = self.current_profile()
                        default = self.catalog.effective_profile(profile).default_voice if profile else ''
                        if default:
                            control.setEditText(default)
                finally:
                    control.blockSignals(blocked)
            self.refresh_speaker_choices()

        def start_host(self):
            try:
                self.stop_session()
                self.clear_hook_observations()
                self.application.start_hook_host()
                self.application.clear_capture_problem()
                self.refresh_capture_workspace()
            except Exception as exc:
                self.application.close_hook_host()
                self.application.handle_capture_activation_error(str(exc))
                self.refresh_capture_workspace()
                self.note('LunaHook Hostの起動に失敗しました: ' + str(exc))

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
                game = self.application.create_game(title, target.executable)
                game_id = game.id
                self.fill_games()
            self.game_choice.setCurrentIndex(self.game_choice.findData(game_id))
            self.application.select_primary_target(target.pid, target.hwnd)
            if not target.hwnd:
                self.note(f'PID {target.pid}: 自動送りにはゲームウィンドウの指定が必要です。')
            self.resolve_associated_targets()
            self.tabs.setCurrentIndex(1)
            self.connect_game()

        def resolve_associated_targets(self):
            """Resolve saved audio/input targets automatically; ask only on ambiguity."""
            game = self.catalog.games.get(self.game_id)
            if not game or not self.application.runtime_target_state().selected:
                return
            # Never retain an alternate process from a previous resolution.
            # The primary game is the baseline; configured alternates replace it
            # only when the current process/window can actually be identified.
            self.application.select_audio_target(None)
            self.application.select_input_target(None, None)
            if game.audio_executable.strip():
                try:
                    candidates = find_game_processes(game.audio_executable)
                    if len(candidates) == 1:
                        self.application.select_audio_target(candidates[0])
                    elif len(candidates) > 1:
                        choices = [f'PID {pid}' for pid in candidates]
                        choice, ok = QInputDialog.getItem(
                            self, '音声プロセスを選択', '同じ実行ファイルが複数あります', choices, 0, False)
                        if ok:
                            self.application.select_audio_target(candidates[choices.index(choice)])
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
                        self.application.select_input_target(chosen.pid, chosen.hwnd)
                except OSError as exc:
                    self.note('入力先ウィンドウを確認できません: ' + str(exc))

        def connect_game(self):
            try:
                self.application.activate_selected_target()
                self.application.clear_capture_problem()
            except Exception as exc:
                self.application.handle_capture_activation_error(str(exc))
                self.note('会話取得を開始できません: ' + str(exc))
            self.refresh_capture_workspace()
            self.refresh_status()

        def clear_hook_observations(self):
            self.source_rows.clear()
            self.application.clear_capture_observations()
            self.source_table.setRowCount(0)
            self.capture_result_table.setRowCount(0)
            self.source_extraction_preview.setRowCount(0)
            self.capture_problem.clear()
            self.capture_recovery_action.hide()
            self.hook_preview.clear()
            self.hook_technical_info.clear()
            self.capture_format_source.setText('取得元を選択してください')
            self.refresh_capture_hint()

        def source_from_candidate_row(self, row: int):
            if row < 0 or row >= self.source_table.rowCount():
                return None
            item = self.source_table.item(row, 0)
            identity = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if not isinstance(identity, (tuple, list)) or len(identity) != 2:
                return None
            key, address = identity
            return self.application.capture_source(key, address, require_unique=False)

        def chosen_source(self):
            return self.source_from_candidate_row(self.source_table.currentRow())

        def try_discovered_hook(self, code: str):
            """Trial a recovery-search code; only real output becomes a candidate."""
            try:
                self.application.insert_recovery_hook(code, discovered=True)
            except (RuntimeError, OSError, ValueError) as exc:
                self.note('探索したHOOKを試用できません: ' + str(exc))

        def refresh_capture_hint(self):
            view = self.application.capture_view()
            has_output = bool(view.candidates)
            self.capture_hint.setText(
                '' if has_output else 'ゲームで台詞を進めると、取得候補が自動的に追加されます。')

        def refresh_source_extraction_preview(self, candidate=None):
            if candidate is None:
                source = self.chosen_source()
                candidate = (self.application.capture_candidate(source.identity, source.address)
                             if source else None)
            self.source_extraction_preview.setRowCount(0)
            if candidate is None:
                self.capture_format_source.setText('取得元を選択してください')
                return
            self.capture_format_source.setText(candidate.latest[:120] or candidate.identity)
            index = self.capture_format.findData(candidate.preset)
            blocked = self.capture_format.blockSignals(True)
            self.capture_format.setCurrentIndex(index if index >= 0 else 0)
            self.capture_format.blockSignals(blocked)
            self.source_extraction_preview.setRowCount(len(candidate.samples))
            for row, sample in enumerate(candidate.samples):
                self.source_extraction_preview.setItem(row, 0, QTableWidgetItem(sample.raw))
                self.source_extraction_preview.setItem(row, 1, QTableWidgetItem(sample.speaker))
                self.source_extraction_preview.setItem(row, 2, QTableWidgetItem(sample.body))

        def refresh_candidate_actions(self, *_):
            source = self.chosen_source()
            candidate = (self.application.capture_candidate(source.identity, source.address)
                         if source else None)
            self.refresh_source_extraction_preview(candidate)
            self.refresh_rules()

        def capture_format_changed(self, *_):
            if getattr(self, '_refreshing_capture_controls', False):
                return
            source = self.chosen_source()
            if source is None:
                return
            try:
                self.application.set_capture_extraction_preset(
                    source.identity, source.address, self.capture_format.currentData())
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('取得文の整形を変更できません: ' + str(exc))

        def _capture_combo_value(self, combo):
            data = combo.currentData()
            if isinstance(data, (tuple, list)) and len(data) == 2:
                return data[0], int(data[1])
            return None, None

        def capture_unwrap_speaker_body_changed(self, enabled):
            if getattr(self, '_refreshing_capture_controls', False):
                return
            try:
                self.application.set_capture_unwrap_speaker_enclosed_body(bool(enabled))
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('話者を使った本文整形を変更できません: ' + str(exc))

        def capture_body_source_changed(self, *_):
            if getattr(self, '_refreshing_capture_controls', False):
                return
            identity, address = self._capture_combo_value(self.capture_body_source)
            if identity is None:
                return
            try:
                self.application.set_capture_field_source('body', identity, address)
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('本文の取得元を変更できません: ' + str(exc))

        def capture_speaker_source_changed(self, *_):
            if getattr(self, '_refreshing_capture_controls', False):
                return
            identity, address = self._capture_combo_value(self.capture_speaker_source)
            try:
                self.application.set_capture_field_source('speaker', identity, address)
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('話者の取得元を変更できません: ' + str(exc))

        def _fill_capture_source_combos(self, view):
            self._refreshing_capture_controls = True
            try:
                self.capture_body_source.blockSignals(True)
                self.capture_speaker_source.blockSignals(True)
                self.capture_unwrap_speaker_body.blockSignals(True)
                self.capture_body_source.clear()
                self.capture_speaker_source.clear()
                self.capture_body_source.addItem('本文の取得元を選択', None)
                self.capture_speaker_source.addItem('本文と同じ取得元（自動）', None)
                body_index = 0
                speaker_index = 0
                for candidate in view.candidates:
                    label = candidate.latest[:70] or candidate.identity
                    data = (candidate.identity, candidate.address)
                    self.capture_body_source.addItem(label, data)
                    self.capture_speaker_source.addItem(label, data)
                    if candidate.identity == view.body_source:
                        body_index = self.capture_body_source.count() - 1
                    if candidate.identity == view.speaker_source:
                        speaker_index = self.capture_speaker_source.count() - 1
                self.capture_body_source.setCurrentIndex(body_index)
                self.capture_speaker_source.setCurrentIndex(speaker_index)
                self.capture_unwrap_speaker_body.setChecked(view.unwrap_speaker_enclosed_body)
            finally:
                self.capture_body_source.blockSignals(False)
                self.capture_speaker_source.blockSignals(False)
                self.capture_unwrap_speaker_body.blockSignals(False)
                self._refreshing_capture_controls = False

        def refresh_capture_workspace(self):
            view = self.application.capture_view()
            profile = self.current_profile()
            if profile and profile.source == 'ocr':
                self.source_table.setRowCount(0)
                self.capture_body_source.clear()
                self.capture_speaker_source.clear()
                self.capture_result_table.setRowCount(0)
                dialogue = self.application.play_view().dialogue
                if dialogue is not None:
                    self.capture_result_table.setRowCount(1)
                    self.capture_result_table.setItem(0, 0, QTableWidgetItem(dialogue.speaker))
                    self.capture_result_table.setItem(0, 1, QTableWidgetItem(dialogue.text))
                issue = self.application.play_view().issue
                if issue and issue.action == 'open_capture':
                    self.capture_problem.setText(issue.message)
                else:
                    self.capture_problem.clear()
                self.capture_recovery_action.hide()
                self.refresh_source_workspace_mode('ocr')
                return

            self.refresh_source_workspace_mode('hook')
            selected_key = None
            source = self.chosen_source()
            if source is not None:
                selected_key = (source.identity, source.address)
            self.source_table.blockSignals(True)
            self.source_table.setRowCount(len(view.candidates))
            self.source_rows.clear()
            try:
                for row, candidate in enumerate(view.candidates):
                    key = (candidate.identity, candidate.address)
                    self.source_rows[key] = row
                    sample = QTableWidgetItem(candidate.latest)
                    sample.setData(Qt.ItemDataRole.UserRole, key)
                    if candidate.ambiguous:
                        sample.setToolTip('同じ識別子を持つ複数の取得元があり、この候補は使用できません')
                    self.source_table.setItem(row, 0, sample)
                    labels = []
                    if 'body' in candidate.used_for:
                        labels.append('本文')
                    if 'speaker' in candidate.used_for:
                        labels.append('話者')
                    self.source_table.setItem(row, 1, QTableWidgetItem('・'.join(labels) if labels else '—'))
                    if key == selected_key:
                        self.source_table.selectRow(row)
            finally:
                self.source_table.blockSignals(False)

            self._fill_capture_source_combos(view)
            self.capture_result_table.setRowCount(len(view.assembly))
            for row, preview in enumerate(view.assembly):
                self.capture_result_table.setItem(row, 0, QTableWidgetItem(preview.speaker))
                self.capture_result_table.setItem(row, 1, QTableWidgetItem(preview.body))

            if view.problem:
                self.capture_problem.setText(view.problem.message)
            else:
                self.capture_problem.clear()
            self.capture_recovery_action.setVisible(
                bool(view.problem and view.problem.action == 'open_capture_recovery'))
            self.btn_commit_capture.setEnabled(view.dirty and view.can_commit)
            self.refresh_capture_hint()
            if self.source_table.currentRow() < 0 and self.source_table.rowCount():
                self.source_table.selectRow(0)
            else:
                self.refresh_candidate_actions()

        def commit_capture_workspace(self):
            if self.current_profile() is None:
                return
            try:
                self.application.commit_capture()
                self.refresh_capture_workspace()
            except Exception as exc:
                self.note('会話取得を確定できません: ' + str(exc))

        def on_hook(self, event: HookEvent, outcome):
            if outcome.connection_transition in ('connected', 'changed', 'restore_failed'):
                self.refresh_capture_workspace()
                self.refresh_capture_hint()
            elif outcome.connection_transition == 'disconnected':
                self.refresh_capture_workspace()

            capture = outcome.capture
            if capture.changed:
                self.refresh_capture_workspace()
                self.refresh_capture_hint()
                if capture.active_source_removed and not capture.monitor_started:
                    self.note('監視中の台詞取得元が消失しました。取得の復旧を確認してください。')
                if capture.new_ambiguity:
                    self.note('同じ識別子の取得元が複数検出されました。別の取得元を選択してください。')
            if event.kind == 'diagnostic':
                self.refresh_capture_workspace()

            if outcome.note:
                self.note(outcome.note)


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

        def display_utterance(self, token: Token):
            u = self.application.runtime_state().utterance
            if not u:
                return
            heading = u.speaker
            if u.use_tts:
                voice = u.voice or 'Voice未設定'
                heading = f'{heading} · {voice}' if heading else voice
            conversation = (f'{heading}\n' if heading else '') + u.text
            self.preview.setPlainText(conversation)
            profile = self.application.runtime_state().profile
            if profile:
                self.wait_preview.setText(
                    f'{u.reading_unit_count}読み単位 / {u.text_wait_ms} ms')
            if not self.example_raw.hasFocus():
                blocked = self.example_raw.blockSignals(True)
                self.example_raw.setPlainText(u.text)
                self.example_raw.blockSignals(blocked)
                self.refresh_rule_preview()
            self.refresh_status()
            if self.current_profile() and self.current_profile().source == 'ocr':
                self.refresh_capture_workspace()

        def on_voice(self, token, success, reason):
            if reason and not success and token == self.application.runtime_state().token:
                self.note(reason)

        def schedule_runtime_callback(self, delay_ms: int, callback):
            QTimer.singleShot(delay_ms, callback)

        def on_runtime_event(self, event: str, token: Token | None, detail: dict):
            if event == 'hook_event':
                self.on_hook(detail['hook_event'], detail['outcome'])
            elif event == 'hook_closed':
                self.on_host_closed(detail.get('reason', ''))
            elif event == 'hook_error':
                self.on_host_error_from_host(detail.get('reason', ''))
            elif event == 'native_ready':
                if detail.get('ready'):
                    self.note('Process audio helper started; waiting for actual captured PCM')
                else:
                    self.note('Native audio unavailable: ' + detail.get('reason', ''))
            elif event == 'native_audio_error' and token is not None:
                self.note('Native audio: ' + detail.get('reason', ''))
            elif event == 'utterance' and token is not None:
                self.display_utterance(token)
            elif event == 'hook_group_flushed':
                self.refresh_capture_workspace()
            elif event == 'ocr_error':
                self.note('OCR stopped: ' + detail.get('reason', ''))
                self.refresh_capture_workspace()


        def permit(self):
            self.application.set_auto_advance(True)

        def pause(self):
            self.application.set_auto_advance(False)

        def refresh_status(self):
            c = self.application.runtime_state()
            token = c.token
            view = self.application.play_view()
            previous = self.advance_authorization.blockSignals(True)
            self.advance_authorization.setChecked(view.advance_authorized)
            self.advance_authorization.setText('自動送り ON' if view.advance_authorized else '自動送り OFF')
            self.advance_authorization.blockSignals(previous)
            self.refresh_input_status()
            if view.issue:
                self.play_issue.setText(view.issue.message)
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
                self.play_issue_action.hide()
            self.refresh_diagnostics()

        def stop_session(self):
            self.application.stop_monitoring('session_changed')
            if hasattr(self, 'preview'):
                self.preview.clear()
            self.refresh_status()

        def on_host_error_from_host(self, message: str):
            self.on_host_error(message)

        def on_host_error(self, message):
            self.refresh_capture_workspace()
            self.note('LunaHostネイティブエラー: ' + message)

        def on_host_closed(self, reason: str):
            self.on_closed(reason)

        def on_closed(self, reason):
            if self.isVisible():
                self.refresh_capture_workspace()
                self.refresh_status()

        def closeEvent(self, event):
            self.stop_session()
            self.application.close_hook_host()
            self.application.close_irodori()
            super().closeEvent(event)

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
