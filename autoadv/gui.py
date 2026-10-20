"""PySide6 Widgets entrypoint. Settings/preview use the live Application service."""
from __future__ import annotations
import asyncio
import json
from queue import Queue, Empty
from pathlib import Path
from threading import Event
from .services import AutoAdvService
from .text import GameTextConfig,StructureRule,TextReplacement
from .contracts import TTSPolicy
from .hook_host import HookHost
from .hook_application import HookApplication
from .hook_protocol import HookKey, ThreadSource
from .preferences import Preferences, LegacyPreferencesDetected, settings_path


def run_gui() -> int:
    from PySide6.QtCore import Qt, Signal, QThread, QTimer, QPoint, QRect
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import (QDialog, QRubberBand, QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,
        QFormLayout,QLabel,QLineEdit,QPushButton,QPlainTextEdit,QScrollArea,QTableWidget,
        QTableWidgetItem,QCheckBox,QComboBox,QFileDialog,QMessageBox,QGroupBox,QSplitter,QListWidget)

    class ServerWorker(QThread):
        result=Signal(object)
        failed=Signal(str)
        def __init__(self, service:AutoAdvService, *,start:bool):
            super().__init__();self.service=service;self.start_server=start
        def run(self):
            try:self.result.emit(self.service.connect_server() if self.start_server else self.service.refresh_voices())
            except Exception as exc:self.failed.emit(f'{type(exc).__name__}: {exc}')

    class ServerShutdown(QThread):
        failed=Signal(str)
        def __init__(self,service:AutoAdvService):
            super().__init__();self.service=service
        def run(self):
            try:self.service.close()
            except Exception as exc:self.failed.emit(f'Irodori終了時のエラー: {exc}')

    class HookWorker(QThread):
        event=Signal(object)
        def __init__(self,dll_path:str,pid:int,hwnd:int,game_id:str,profile_id:str,keys:tuple[HookKey,...]):
            super().__init__()
            self.dll_path=Path(dll_path);self.pid=pid;self.hwnd=hwnd
            self.game_id=game_id;self.profile_id=profile_id;self.keys=keys
            self.stopping=Event();self.commands=Queue()

        def issue(self,action:str,**kwargs):
            self.commands.put((action,kwargs))

        def run(self):
            messages=Queue()
            host=HookHost(self.dll_path,
                lambda message:messages.put(('event',message)),
                lambda reason:messages.put(('closed',reason)))
            app=HookApplication(self.game_id,self.profile_id,
                lambda observation:self.event.emit({'original':observation.text}),
                lambda reason:self.event.emit({'reason':reason}))
            app.update_roles(hook_pids={self.pid},audio_pids={self.pid},
                             input_pid=self.pid,input_hwnd=self.hwnd)
            app.select(self.keys)
            try:
                host.start();host.command('connect',pid=self.pid)
                while not self.stopping.is_set():
                    try:
                        kind,payload=messages.get(timeout=.05)
                        if kind=='closed':
                            app.host_closed(payload)
                            self.event.emit({'connected':False,'reason':payload})
                            break
                        app.handle(payload)
                        view=app.status()
                        candidates=list(app.candidates.items())
                        self.event.emit({'connected':view.state in ('connected','observing','preview_only'),
                                         'state':view.state,'reason':view.reason,
                                         'candidates':candidates,'search':dict(app.search_results)})
                    except Empty:pass
                    while True:
                        try:action,kw=self.commands.get_nowait()
                        except Empty:break
                        if action=='select':app.select(tuple(kw['keys']))
                        elif action in ('find_hooks','insert_hook','history','sync_thread'):
                            host.command(action,**kw)
            except Exception as exc:
                self.event.emit({'connected':False,'reason':f'{type(exc).__name__}: {exc}'})
            finally:
                host.close()

    class ScreenCaptureWorker(QThread):
        captured=Signal(object)
        failed=Signal(str)
        def __init__(self,pid:int,hwnd:int):
            super().__init__();self.pid=pid;self.hwnd=hwnd
        def run(self):
            try:
                from .screen import WindowsClientCapture
                self.captured.emit(WindowsClientCapture(self.hwnd,self.pid)())
            except Exception as exc:self.failed.emit(f'{type(exc).__name__}: {exc}')

    class RegionPicker(QDialog):
        """Select a rectangle from a real captured game client area."""
        def __init__(self,frame,parent=None):
            super().__init__(parent)
            self.setWindowTitle('ゲーム画面内の範囲を選択')
            self.frame=frame
            self.rect_pixels=None
            layout=QVBoxLayout(self)
            self.label=QLabel()
            self.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            self.label.setMouseTracking(True)
            height,width=frame.shape[:2]
            self.display_width=min(width,1000)
            self.display_height=max(1,round(height*self.display_width/width))
            img=QImage(frame.data,width,height,int(frame.strides[0]),QImage.Format.Format_BGR888).copy()
            self.label.setPixmap(QPixmap.fromImage(img).scaled(
                self.display_width,self.display_height,Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
            self.label.setFixedSize(self.display_width,self.display_height)
            band=QRubberBand(QRubberBand.Shape.Rectangle,self.label)
            self.band=band
            self._origin=None
            def press(event):
                if event.button()==Qt.MouseButton.LeftButton:
                    self._origin=event.position().toPoint()
                    band.setGeometry(QRect(self._origin,self._origin));band.show()
            def move(event):
                if self._origin is not None:
                    band.setGeometry(QRect(self._origin,event.position().toPoint()).normalized())
            def release(event):
                if self._origin is None:return
                result=QRect(self._origin,event.position().toPoint()).normalized().intersected(self.label.rect())
                self._origin=None
                x1=round(result.left()*width/self.display_width)
                y1=round(result.top()*height/self.display_height)
                x2=round((result.left()+result.width())*width/self.display_width)
                y2=round((result.top()+result.height())*height/self.display_height)
                self.rect_pixels=(x1,y1,x2-x1,y2-y1)
            self.label.mousePressEvent=press
            self.label.mouseMoveEvent=move
            self.label.mouseReleaseEvent=release
            layout.addWidget(self.label)
            layout.addWidget(QLabel(f'元画像 {width}×{height} px（ゲームのクライアント領域）。ドラッグで矩形を選択'))
            actions=QHBoxLayout()
            ok=QPushButton('この範囲を使用');cancel=QPushButton('取消')
            actions.addWidget(ok);actions.addWidget(cancel);layout.addLayout(actions)
            def accept_selected():
                try:
                    from .acquisition import SourceSelection, SourceKind
                    SourceSelection(SourceKind.OCR,region=self.rect_pixels)
                    self.accept()
                except ValueError as exc:QMessageBox.warning(self,'無効なOCR範囲',str(exc))
            ok.clicked.connect(accept_selected);cancel.clicked.connect(self.reject)

    class GameWorker(QThread):
        status=Signal(object)
        finished_status=Signal(str)
        def __init__(self,config,service,control,temporary):
            super().__init__()
            self.config=config
            self.preferences=service.preferences
            self.game_config=service.current_config()
            self.control=control
            self.temporary=temporary
        def run(self):
            from .live import run_windows
            try:
                code=run_windows(self.config,preferences=self.preferences,
                    game_config=self.game_config,control=self.control,
                    status=lambda state:self.status.emit(state),enable_input=True)
                self.finished_status.emit('Monitor ended (code '+str(code)+')')
            except Exception as exc:
                self.finished_status.emit(f'{type(exc).__name__}: {exc}')
            finally:
                self.temporary.cleanup()

    def table(headers, minimum=180):
        t=QTableWidget(0,len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setMinimumHeight(minimum)
        t.horizontalHeader().setStretchLastSection(True)
        return t

    ROUTE_LABELS={
        'AUTO':'状況から選択（AUTO）',
        'NATIVE':'ゲーム原音（NATIVE）',
        'SELECTED':'指定VoiceでTTS（SELECTED）',
        'DEFAULT':'既定VoiceでTTS（DEFAULT）',
        'SILENT':'読み上げず閲覧（SILENT）',
        'UNASSIGNED':'状況から選択（UNASSIGNED）',
        'SKIP':'読み上げず閲覧（SKIP）',
        'DISABLED':'読み上げず閲覧（DISABLED）',
    }

    def append(t, values):
        row=t.rowCount();t.insertRow(row)
        for i,value in enumerate(values):
            if t.property('voice_routing') and i in (1,2):
                combo=QComboBox();combo.setEditable(i==2)
                if i==1:
                    for key,label in ROUTE_LABELS.items():combo.addItem(label,key)
                    combo.setCurrentIndex(max(0,combo.findData(value)))
                else:
                    combo.addItems(t.property('voice_ids') or ())
                    combo.setCurrentText(str(value))
                t.setCellWidget(row,i,combo)
            else:t.setItem(row,i,QTableWidgetItem(str(value)))

    def rows(t):
        return [[(t.cellWidget(r,c).currentData() if t.property('voice_routing') and c==1
                  else t.cellWidget(r,c).currentText()) if isinstance(t.cellWidget(r,c),QComboBox)
                 else t.item(r,c).text() if t.item(r,c) is not None else ''
                 for c in range(t.columnCount())] for r in range(t.rowCount())]

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            try:
                self.service=AutoAdvService()
            except LegacyPreferencesDetected:
                answer=QMessageBox.question(self,'旧設定の移行',
                    '旧AutoAdv設定が見つかりました。原本をバックアップし、新しい統合設定へ一回だけ取り込みますか？')
                if answer != QMessageBox.StandardButton.Yes:
                    raise RuntimeError('設定移行が未承認です。原本は変更していません')
                Preferences.import_legacy(settings_path())
                self.service=AutoAdvService()
            self.server_worker=None
            self.hook_worker=None
            self.game_worker=None
            self.live_control=None
            self.active_target=None
            self._other_workers=[]
            self._closing=False
            self._shutdown_worker=None
            self.setWindowTitle('AutoAdv — 音声同期オート進行')
            self.resize(1160,920)
            top=QWidget();self.setCentralWidget(top)
            root=QVBoxLayout(top)
            statuses=QGroupBox('接続・音声・進行状態（常時表示）');root.addWidget(statuses)
            status_grid=QGridLayout(statuses)
            fields=['対象ゲーム','対象ウィンドウ/フォーカス','取得方式','HOOK Host接続','最新取得原文',
                    'Irodoriフォルダ','Irodori接続','Voice候補','提示経路','音声監視',
                    '自動進行許可','HOOK/音声同期','停止理由','入力/次HOOK候補','現在Utterance']
            self.status_labels={}
            for i,name in enumerate(fields):
                label=QLabel(name);value=QLabel('未設定 / 不明');value.setWordWrap(True)
                status_grid.addWidget(label,i//2,2*(i%2));status_grid.addWidget(value,i//2,2*(i%2)+1)
                self.status_labels[name]=value
            controls=QHBoxLayout();root.addLayout(controls)
            self.game_name=QComboBox();self.game_name.setEditable(True)
            controls.addWidget(QLabel('作品'));controls.addWidget(self.game_name,1)
            self.new_game=QPushButton('作品登録');controls.addWidget(self.new_game)
            self.profile_name=QComboBox();self.profile_name.setEditable(True)
            controls.addWidget(QLabel('プロファイル'));controls.addWidget(self.profile_name,1)
            self.duplicate_profile=QPushButton('複製');controls.addWidget(self.duplicate_profile)
            self.rename_profile=QPushButton('改名');controls.addWidget(self.rename_profile)
            self.delete_profile=QPushButton('削除');controls.addWidget(self.delete_profile)
            self.default_profile=QPushButton('既定にする');controls.addWidget(self.default_profile)
            self.folder=QPushButton('Irodoriフォルダ指定');controls.addWidget(self.folder)
            self.retry=QPushButton('Irodori再接続・Voice更新');controls.addWidget(self.retry)
            self.luna_button=QPushButton('HOOK探索・プレビュー開始');controls.addWidget(self.luna_button)
            self.live_button=QPushButton('ゲーム監視開始');controls.addWidget(self.live_button)
            self.stop_button=QPushButton('監視終了');controls.addWidget(self.stop_button)
            advance_controls=QHBoxLayout();root.addLayout(advance_controls)
            self.monitor_state=QLabel('監視: 停止')
            advance_controls.addWidget(self.monitor_state)
            self.allow=QPushButton('対象ゲームと原文の一致を確認して自動進行許可')
            self.pause=QPushButton('明示一時停止')
            self.allow.setEnabled(False)  # Enabled only while the guarded game session is active.
            self.pause.setEnabled(False)
            advance_controls.addWidget(self.allow);advance_controls.addWidget(self.pause)
            # The monitor profile is a per-game saved configuration, not a
            # second input loop or a JSON file the user must hand-edit.
            splitter=QSplitter(Qt.Orientation.Horizontal);root.addWidget(splitter,1)
            left=QScrollArea();left.setWidgetResizable(True);splitter.addWidget(left)
            pane=QWidget();form=QVBoxLayout(pane);left.setWidget(pane)
            self.rules=table(['規則ID','名前付き正規表現','有効(1/0)','適用条件'])
            self.replacements=table(['置換対象/原語','置換結果','正規表現(1/0)','有効(1/0)','用途(tts/asr/both)'])
            self.readings=table(['名前・漢字','読み'])
            self.routes=table(['話者','音声方針 (NATIVE / SELECTED / DEFAULT / AUTO / SILENT)','Voice ID'])
            self.routes.setProperty('voice_routing',True)
            for title,t in [('話者・本文分離（優先順位は上から）',self.rules),
                            ('読み置換（優先順位は上から）',self.replacements),
                            ('読み辞書',self.readings),('話者別の原音・TTS・自動選択・閲覧',self.routes)]:
                group=QGroupBox(title);gl=QVBoxLayout(group);gl.addWidget(t)
                rr=QHBoxLayout();gl.addLayout(rr)
                add=QPushButton('行追加');add.clicked.connect(lambda _=False,x=t:append(x,['']*x.columnCount()))
                remove=QPushButton('選択行削除');remove.clicked.connect(lambda _=False,x=t:x.removeRow(x.currentRow()) if x.currentRow()>=0 else None)
                up=QPushButton('優先順位↑')
                def move_up(x=t):
                    index=x.currentRow()
                    if index<1:return
                    before=rows(x)
                    before[index-1],before[index]=before[index],before[index-1]
                    x.setRowCount(0)
                    for values in before:append(x,values)
                    x.selectRow(index-1)
                up.clicked.connect(move_up)
                for b in (add,remove,up):rr.addWidget(b)
                form.addWidget(group)
            self.fallback=QComboBox()
            for policy,label in ROUTE_LABELS.items():self.fallback.addItem(label,policy)
            self.unknown_voice=QLineEdit();self.default_voice=QComboBox();self.default_voice.setEditable(True)
            defaults=QFormLayout();defaults.addRow('未知話者の動作',self.fallback)
            defaults.addRow('未知話者のVoice ID',self.unknown_voice)
            defaults.addRow('既定Voice ID',self.default_voice);form.addLayout(defaults)
            self.save=QPushButton('作品ルール・Voice割当てを保存');form.addWidget(self.save)
            hook_group=QGroupBox('LunaHook（AutoAdv内で探索・選択・保存）')
            form.addWidget(hook_group)
            hbox=QVBoxLayout(hook_group)
            self.host_dll=QLineEdit(self.service.preferences.luna_host_dll)
            hostrow=QHBoxLayout();hostrow.addWidget(QLabel('LunaHost DLL'));hostrow.addWidget(self.host_dll,1)
            self.browse_host=QPushButton('参照');hostrow.addWidget(self.browse_host)
            hbox.addLayout(hostrow)
            self.hook_candidates=QListWidget();hbox.addWidget(self.hook_candidates)
            self.hook_selection=QListWidget();hbox.addWidget(QLabel('保存するHOOK（上から優先順）'))
            hbox.addWidget(self.hook_selection)
            hook_buttons=QHBoxLayout();hbox.addLayout(hook_buttons)
            self.add_hook=QPushButton('候補を選択');self.remove_hook=QPushButton('選択解除')
            self.move_hook_up=QPushButton('優先↑');self.save_hooks=QPushButton('HOOK選択を保存')
            self.find_hooks=QPushButton('追加HOOK探索')
            self.hook_code=QLineEdit();self.hook_code.setPlaceholderText('手動HOOKコード')
            self.insert_hook=QPushButton('手動HOOK追加')
            for button in (self.add_hook,self.remove_hook,self.move_hook_up,self.save_hooks,self.find_hooks):
                hook_buttons.addWidget(button)
            manual_hook=QHBoxLayout();manual_hook.addWidget(self.hook_code,1);manual_hook.addWidget(self.insert_hook)
            hbox.addLayout(manual_hook)
            source_group=QGroupBox('本文取得方式（HOOK標準・OCR任意）')
            form.addWidget(source_group)
            source_layout=QFormLayout(source_group)
            self.source_kind=QComboBox();self.source_kind.addItems(['hook','ocr'])
            self.ocr_executable=QLineEdit('tesseract')
            self.ocr_language=QLineEdit('jpn')
            self.ocr_rect=QLineEdit();self.ocr_rect.setPlaceholderText('x,y,width,height（ゲーム画面のクライアント領域）')
            self.save_source=QPushButton('取得方式をプロファイルへ保存')
            self.pick_ocr=QPushButton('ゲーム画面からOCR本文領域を選択')
            source_layout.addRow('取得方式（既定HOOK）',self.source_kind)
            source_layout.addRow('OCR実行ファイル（OCR選択時）',self.ocr_executable)
            source_layout.addRow('OCR言語（OCR選択時）',self.ocr_language)
            source_layout.addRow('OCR領域（OCR選択時）',self.ocr_rect)
            source_layout.addRow(self.pick_ocr)
            source_layout.addRow(self.save_source)
            monitor=QGroupBox('作品別ゲーム監視・音声・送り（保存後に反映）')
            form.addWidget(monitor)
            monitor_layout=QVBoxLayout(monitor)
            self.monitor_location=QLabel('監視設定未保存');self.monitor_location.setWordWrap(True)
            monitor_layout.addWidget(self.monitor_location)
            self.window_choices=QComboBox();self.window_choices.setMinimumWidth(310)
            find_windows=QPushButton('起動中ゲームの窓を一覧更新')
            use_window=QPushButton('この窓のPID/HWNDを設定')
            window_bar=QHBoxLayout();window_bar.addWidget(self.window_choices,1)
            window_bar.addWidget(find_windows);window_bar.addWidget(use_window)
            monitor_layout.addLayout(window_bar)
            live_form=QFormLayout();monitor_layout.addLayout(live_form)
            self.monitor_fields={}
            self._loading_monitor=False
            monitor_fields=(
                ('pid','対象ゲームPID',False),('hwnd','対象HWND',False),
                ('loopback_helper','Process Loopback helper (原音用・任意)',True),
                ('asr_model','日本語ASRモデル (原音用・任意)',True),('asr_tokens','ASR tokens (原音用・任意)',True),
                ('log_path','ログ出力先',True),('grace_ms','native開始猶予 ms',False),
                ('tts_speed','TTS速度',False),('silent_base_ms','無音基準閲覧 ms',False),
                ('silent_min_ms','無音最低閲覧 ms',False),
                ('reading_chars_per_sec','閲覧文字数/秒',False),
                ('input_virtual_key','送りキー仮想キーコード',False))
            for name,title,is_path in monitor_fields:
                edit=QLineEdit();self.monitor_fields[name]=edit
                if is_path:
                    row=QWidget();horizontal=QHBoxLayout(row)
                    horizontal.setContentsMargins(0,0,0,0)
                    horizontal.addWidget(edit,1)
                    choose=QPushButton('参照')
                    def browse_file(_=False,key=name,entry=edit):
                        path,_=QFileDialog.getOpenFileName(self,'ファイル選択',entry.text())
                        if path:entry.setText(path)
                    choose.clicked.connect(browse_file)
                    horizontal.addWidget(choose)
                    live_form.addRow(title,row)
                else:live_form.addRow(title,edit)
                if name not in ('pid','hwnd'):edit.textEdited.connect(self.mark_monitor_dirty)
            self.input_mode=QComboBox();self.input_mode.addItems(['keyboard','mouse_left'])
            live_form.addRow('自動入力方式',self.input_mode)
            self.heuristic_absence=QCheckBox('ASRの無音判定を利用（不明時は停止）')
            self.tts_required=QCheckBox('TTS必須（失敗時は停止）')
            live_form.addRow(self.heuristic_absence);live_form.addRow(self.tts_required)
            for widget in (self.input_mode,self.heuristic_absence,self.tts_required):
                (widget.currentIndexChanged if isinstance(widget,QComboBox) else widget.toggled).connect(self.mark_monitor_dirty)
            monitor_layout.addWidget(QLabel('HOOK本文・音声同期で送る。特殊画面・選択肢の完全自動検出はしません。'))
            monitor_actions=QHBoxLayout();monitor_layout.addLayout(monitor_actions)
            self.import_monitor=QPushButton('既存監視JSONを取り込む')
            self.save_monitor=QPushButton('ゲーム監視設定を保存')
            monitor_actions.addWidget(self.import_monitor);monitor_actions.addWidget(self.save_monitor)
            self.import_monitor.clicked.connect(self.import_monitor_profile)
            self.save_monitor.clicked.connect(self.save_monitor_profile)
            find_windows.clicked.connect(self.refresh_windows)
            use_window.clicked.connect(self.select_target_window)
            self.pick_ocr.clicked.connect(self.capture_ocr_region)

            right=QWidget();right_layout=QVBoxLayout(right);splitter.addWidget(right)
            right_layout.addWidget(QLabel('Luna原文（受信内容は加工しない）'))
            self.original=QPlainTextEdit();self.original.setPlaceholderText('選択したHOOKまたはOCRの取得文を確認してください')
            self.original.setMinimumHeight(140);right_layout.addWidget(self.original)
            right_layout.addWidget(QLabel('実処理と共通のUtteranceプレビュー'))
            self.preview=QPlainTextEdit();self.preview.setReadOnly(True)
            self.preview.setMinimumHeight(140);right_layout.addWidget(self.preview)
            self.test=QPushButton('本文分離・読み置換をテスト');right_layout.addWidget(self.test)
            right_layout.addWidget(QLabel('実行中のUtterance（保存済み設定による解釈結果）'))
            self.active_utterance=QPlainTextEdit();self.active_utterance.setReadOnly(True)
            self.active_utterance.setPlaceholderText('監視停止中 / 台詞未確定')
            self.active_utterance.setMinimumHeight(130);right_layout.addWidget(self.active_utterance)
            right_layout.addWidget(QLabel('接続済みVoice IDs（API一覧優先）'))
            self.voices=QPlainTextEdit();self.voices.setReadOnly(True);right_layout.addWidget(self.voices)
            self.sample_voice=QComboBox();self.sample_voice.setEditable(True)
            self.try_voice=QPushButton('プレビューを試聴（ゲーム送出と独立）')
            r=QHBoxLayout();r.addWidget(self.sample_voice);r.addWidget(self.try_voice)
            right_layout.addLayout(r)
            self.issue=QLabel('設定変更中は現在の行へ割り込ませません');self.issue.setWordWrap(True)
            right_layout.addWidget(self.issue);right_layout.addStretch()
            splitter.setSizes([650,510])
            self.folder.clicked.connect(self.choose_folder)
            self.retry.clicked.connect(lambda:self.connect_server(False))
            self.luna_button.clicked.connect(self.start_luna)
            self.stop_button.clicked.connect(self.stop_all)
            self.live_button.clicked.connect(self.start_game)
            self.allow.clicked.connect(self.grant_live)
            self.pause.clicked.connect(self.pause_live)
            self.game_name.activated.connect(lambda _:self.game_changed(self.game_name.currentData()))
            self.profile_name.activated.connect(lambda _:self.profile_changed(self.profile_name.currentData()))
            self.new_game.clicked.connect(self.create_game)
            self.duplicate_profile.clicked.connect(self.clone_profile)
            self.rename_profile.clicked.connect(self.rename_current_profile)
            self.delete_profile.clicked.connect(self.remove_profile)
            self.default_profile.clicked.connect(self.make_default)
            self.browse_host.clicked.connect(self.choose_host_dll)
            self.add_hook.clicked.connect(self.add_selected_hook)
            self.remove_hook.clicked.connect(self.remove_selected_hook)
            self.move_hook_up.clicked.connect(self.hook_up)
            self.save_hooks.clicked.connect(self.save_hook_profile)
            self.save_source.clicked.connect(self.save_acquisition_profile)
            self.find_hooks.clicked.connect(lambda:self.hook_worker.issue('find_hooks',pid=int(self.monitor_fields['pid'].text()))
                                             if self.hook_worker and self.hook_worker.isRunning() else None)
            self.insert_hook.clicked.connect(lambda:self.hook_worker.issue('insert_hook',pid=int(self.monitor_fields['pid'].text()),code=self.hook_code.text())
                                              if self.hook_worker and self.hook_worker.isRunning() else None)
            self.original.textChanged.connect(self.update_preview)
            self.test.clicked.connect(self.update_preview)
            self.save.clicked.connect(self.save_profile)
            self.try_voice.clicked.connect(self.test_voice)
            self.load_profile()
            self.show_status('自動進行許可','無効（毎回明示操作が必要）')
            if self.service.preferences.irodori_root:self.connect_server(True)

        def show_status(self,key,value):
            self.status_labels[key].setText(str(value))

        def refresh_catalog(self):
            selected=self.service.catalog.active()
            self.game_name.blockSignals(True)
            self.game_name.clear()
            for game in self.service.catalog.games.values():
                self.game_name.addItem(f'{game.display_name} [{game.game_id[:6]}]',game.game_id)
            if selected:self.game_name.setCurrentIndex(self.game_name.findData(selected[0].game_id))
            self.game_name.blockSignals(False)
            self.profile_name.blockSignals(True)
            self.profile_name.clear()
            if selected:
                for profile in selected[0].profiles.values():
                    self.profile_name.addItem(profile.name,profile.profile_id)
                self.profile_name.setCurrentIndex(self.profile_name.findData(selected[1].profile_id))
            self.profile_name.blockSignals(False)
            self.hook_selection.clear()
            if selected:
                for key in selected[1].keys():self.hook_selection.addItem(f'{key.code} | {key.name} | {key.context:x}:{key.context2:x}')
                self._selected_keys=list(selected[1].keys())
            else:self._selected_keys=[]

        def create_game(self):
            if self.game_worker and self.game_worker.isRunning():return
            text=self.game_name.currentText().strip()
            if not text:
                self.issue.setText('作品名を指定してください');return
            self.service.new_game(text)
            self.refresh_catalog();self.load_profile()

        def profile_changed(self,id):
            if self.game_worker and self.game_worker.isRunning():return
            if id:self.service.select(self.service.catalog.active_game_id,id)
            self.refresh_catalog();self.load_profile()

        def clone_profile(self):
            if self.game_worker and self.game_worker.isRunning():return
            self.service.profile_duplicate(self.profile_name.currentText().strip()+'（複製）')
            self.refresh_catalog();self.load_profile()

        def rename_current_profile(self):
            if self.game_worker and self.game_worker.isRunning():return
            try:
                self.service.profile_rename(self.profile_name.currentText())
                self.refresh_catalog();self.load_profile()
            except ValueError as exc:self.issue.setText(str(exc))

        def remove_profile(self):
            if self.game_worker and self.game_worker.isRunning():return
            try:
                self.service.profile_delete();self.refresh_catalog();self.load_profile()
            except ValueError as exc:self.issue.setText(str(exc))

        def make_default(self):
            self.service.profile_set_default()
            self.issue.setText('この作品の既定プロファイルを設定しました')

        def choose_host_dll(self):
            if self.game_worker and self.game_worker.isRunning():return
            path,_=QFileDialog.getOpenFileName(self,'LunaHost DLL',self.host_dll.text(),'DLL (*.dll)')
            if path:
                self.service.preferences.luna_host_dll=path
                self.service.preferences.save(self.service.settings_file)
                self.host_dll.setText(path)

        def add_selected_hook(self):
            i=self.hook_candidates.currentRow()
            if not hasattr(self,'_hook_sources') or not 0<=i<len(self._hook_sources):return
            key=self._hook_sources[i].key
            if key not in self._selected_keys:
                self._selected_keys.append(key)
                self.hook_selection.addItem(f'{key.code} | {key.name} | {key.context:x}:{key.context2:x}')
                if self.hook_worker:self.hook_worker.issue('select',keys=self._selected_keys[:])

        def remove_selected_hook(self):
            i=self.hook_selection.currentRow()
            if 0<=i<len(self._selected_keys):
                self._selected_keys.pop(i);self.hook_selection.takeItem(i)
                if self.hook_worker:self.hook_worker.issue('select',keys=self._selected_keys[:])

        def hook_up(self):
            i=self.hook_selection.currentRow()
            if i>0:
                self._selected_keys[i-1],self._selected_keys[i]=self._selected_keys[i],self._selected_keys[i-1]
                text=self.hook_selection.takeItem(i)
                self.hook_selection.insertItem(i-1,text)
                self.hook_selection.setCurrentRow(i-1)
                if self.hook_worker:self.hook_worker.issue('select',keys=self._selected_keys[:])

        def save_hook_profile(self):
            try:
                if self.game_worker and self.game_worker.isRunning():raise ValueError('監視中のHOOK変更不可')
                self.service.save_hook_selection(tuple(self._selected_keys))
                self.issue.setText('HOOKと選択順を現在プロファイルに保存しました')
            except Exception as exc:self.issue.setText(str(exc))

        def save_acquisition_profile(self):
            from .acquisition import SourceSelection, SourceKind
            try:
                if self.game_worker and self.game_worker.isRunning():
                    raise ValueError('監視中の取得方式変更不可')
                kind=SourceKind(self.source_kind.currentText())
                raw=self.ocr_rect.text().strip()
                region=tuple(int(x.strip()) for x in raw.split(',')) if kind is SourceKind.OCR else None
                selection=SourceSelection(kind,self.ocr_executable.text().strip(),
                    self.ocr_language.text().strip(),region)
                self.service.configure_acquisition(selection)
                self.show_status('停止理由','取得源変更後は再検証と再許可が必要です')
                self.issue.setText('取得方式を現在プロファイルに保存しました')
            except (ValueError,TypeError) as exc:
                self.issue.setText('取得方式保存不可: '+str(exc))

        def load_profile(self):
            self.refresh_catalog()
            c=self.service.current_config()
            tables=[(self.rules,[(r.rule_id,r.pattern,'1' if r.enabled else '0',r.condition) for r in c.structure_rules]),
                    (self.replacements,[(r.pattern,r.replacement,'1' if r.regex else '0','1' if r.enabled else '0',r.target) for r in c.replacements]),
                    (self.readings,list(c.name_readings.items())),
                    (self.routes,[(name,policy.name,voice or '') for name,(policy,voice) in c.speaker_routes.items()])]
            for t,data in tables:
                t.setRowCount(0)
                for row in data:append(t,row)
            self.fallback.setCurrentIndex(self.fallback.findData(c.unknown_route.name))
            self.unknown_voice.setText(c.unknown_voice or '')
            self.default_voice.setCurrentText(c.default_voice or '')
            self.show_status('対象ゲーム',self.service.current_game_name() or '未選択')
            self.show_status('Irodoriフォルダ',self.service.preferences.irodori_root or '未設定')
            active=self.service.active()
            from .acquisition import SourceSelection
            selection=active[1].source() if active else SourceSelection()
            self.source_kind.setCurrentText(selection.kind.value)
            self.show_status('取得方式',selection.kind.value)
            self.ocr_executable.setText(selection.tesseract_executable)
            self.ocr_language.setText(selection.language)
            self.ocr_rect.setText(','.join(map(str,selection.region)) if selection.region else '')
            self.load_monitor_profile()
            self.update_preview()

        def game_changed(self,game_id):
            if self.game_worker and self.game_worker.isRunning():
                self.refresh_catalog()
                self.issue.setText('監視中は作品を変更できません')
                return
            if game_id:self.service.select(game_id)
            self.refresh_catalog();self.load_profile()

        def mark_monitor_dirty(self,*_):
            if not self._loading_monitor:
                self.monitor_dirty=True
                self.monitor_location.setText('監視設定に未保存の変更があります')

        def refresh_windows(self):
            self.window_choices.clear()
            try:
                from .live import list_target_windows
                self._window_candidates=list_target_windows()
                for pid,hwnd,title in self._window_candidates:
                    self.window_choices.addItem(f'{title[:65]}  PID={pid} HWND={hwnd}')
                if not self._window_candidates:
                    self.issue.setText('対象候補のウィンドウがありません。PID/HWNDを手動入力できます')
            except (OSError,RuntimeError) as exc:self.issue.setText(str(exc))

        def select_target_window(self):
            index=self.window_choices.currentIndex()
            if not hasattr(self,'_window_candidates') or not 0<=index<len(self._window_candidates):return
            pid,hwnd,_=self._window_candidates[index]
            self.monitor_fields['pid'].setText(str(pid))
            self.monitor_fields['hwnd'].setText(str(hwnd))
            self.show_status('対象ウィンドウ/フォーカス',f'PID={pid} / HWND={hwnd} / 未検証')

        def load_monitor_document(self,document,path_label):
            self._loading_monitor=True
            try:
                defaults={'pid':'','hwnd':'','loopback_helper':'','asr_model':'','asr_tokens':'',
                          'log_path':'','grace_ms':500,'tts_speed':1.0,'silent_base_ms':700,
                          'silent_min_ms':1000,'reading_chars_per_sec':12.0,'input_virtual_key':13}
                values={**defaults,**document}
                for key in ('silent_base_ms','silent_min_ms','reading_chars_per_sec'):
                    values[key]=document.get('silent_dwell',{}).get({'silent_base_ms':'base_ms',
                        'silent_min_ms':'min_ms','reading_chars_per_sec':'reading_chars_per_sec'}[key],defaults[key])
                values['input_virtual_key']=document.get('advance_input',{}).get('virtual_key',13)
                for name,edit in self.monitor_fields.items():edit.setText(str(values[name]))
                self.input_mode.setCurrentText(document.get('advance_input',{}).get('mode','keyboard'))
                self.heuristic_absence.setChecked(document.get('allow_heuristic_absence',True))
                self.tts_required.setChecked(document.get('tts_required',False))
                self.monitor_location.setText(path_label)
                self.monitor_dirty=False
            finally:self._loading_monitor=False

        def load_monitor_profile(self):
            try:
                data=self.service.monitor_document()
                self.load_monitor_document(data,'統合プロファイルに保存済み' if data else '監視設定未保存')
            except (ValueError,OSError,KeyError) as exc:
                self.monitor_location.setText('監視設定を読み取れません: '+str(exc))
                self.monitor_dirty=True

        def import_monitor_profile(self):
            if self.game_worker is not None and self.game_worker.isRunning():
                self.issue.setText('監視終了後に監視設定を変更してください');return
            path,_=QFileDialog.getOpenFileName(self,'既存のゲーム監視JSONを指定','','JSON (*.json)')
            if not path:return
            try:
                from .live import LiveConfig
                config=LiveConfig.load(Path(path))
                data=json.loads(Path(path).read_text(encoding='utf-8'))
                for key in ('loopback_helper','asr_model','asr_tokens','log_path'):
                    data[key]=str(getattr(config,key) or '')
                data.pop('screen_profile',None)
                self.load_monitor_document(data,'取り込み済み・まだ保存されていません')
                self.mark_monitor_dirty()
            except (ValueError,OSError,KeyError) as exc:
                self.issue.setText('監視設定を取り込めません: '+str(exc))

        def current_monitor_document(self):
            values={name:edit.text().strip() for name,edit in self.monitor_fields.items()}
            abs_path=lambda value:str(Path(value).expanduser().resolve()) if value else ''
            return {'loopback_helper':abs_path(values['loopback_helper']),
                  'asr_model':abs_path(values['asr_model']),
                  'asr_tokens':abs_path(values['asr_tokens']),
                  'log_path':abs_path(values['log_path']),
                  'grace_ms':int(values['grace_ms']),
                  'allow_heuristic_absence':self.heuristic_absence.isChecked(),
                  'tts_speed':float(values['tts_speed']),
                  'silent_dwell':{'base_ms':int(values['silent_base_ms']),
                                  'min_ms':int(values['silent_min_ms']),
                                  'reading_chars_per_sec':float(values['reading_chars_per_sec'])},
                  'tts_required':self.tts_required.isChecked(),
                  'advance_input':{'mode':self.input_mode.currentText(),
                                   'virtual_key':int(values['input_virtual_key'])}}

        def capture_ocr_region(self):
            if self.game_worker and self.game_worker.isRunning():
                self.issue.setText('OCR範囲は監視終了後に指定してください');return
            try:
                pid=int(self.monitor_fields['pid'].text())
                hwnd=int(self.monitor_fields['hwnd'].text())
                if pid<=0 or hwnd<=0:raise ValueError('対象ウィンドウを指定してください')
            except ValueError as exc:self.issue.setText(str(exc));return
            worker=ScreenCaptureWorker(pid,hwnd)
            self._other_workers.append(worker)
            self.issue.setText('OCR本文範囲用のゲーム画像を取得中')
            worker.captured.connect(self.ocr_region_frame)
            worker.failed.connect(lambda reason:self.issue.setText('OCR画像取得失敗: '+reason))
            worker.finished.connect(lambda:self._other_workers.remove(worker))
            worker.start()

        def ocr_region_frame(self,frame):
            dialog=RegionPicker(frame,self)
            if dialog.exec()==QDialog.DialogCode.Accepted and dialog.rect_pixels:
                self.ocr_rect.setText(','.join(map(str,dialog.rect_pixels)))
                self.issue.setText('OCR本文領域を指定しました。取得方式の保存で反映します。')

        def save_monitor_profile(self):
            if self.game_worker and self.game_worker.isRunning():
                self.issue.setText('監視設定を変更するにはゲーム監視を終了してください');return
            if not self.service.active() or self.game_name.currentData()!=self.service.catalog.active_game_id:
                self.issue.setText('先に作品ルールを保存し、作品を選択してください');return
            try:
                self.service.save_monitor(self.current_monitor_document())
                self.monitor_dirty=False
                self.monitor_location.setText('統合プロファイルに保存済み')
                self.issue.setText('ゲーム監視・音声設定を保存しました。次の監視開始から適用します')
            except (OSError,ValueError,TypeError,KeyError) as exc:
                self.issue.setText('監視設定を保存できません: '+str(exc))

        def current_editor_config(self):
            return GameTextConfig(
                structure_rules=tuple(StructureRule(a,b,c.strip()!='0',d) for a,b,c,d in rows(self.rules) if a and b),
                replacements=tuple(TextReplacement(a,b,c.strip()=='1',d.strip()!='0',e or 'tts')
                                   for a,b,c,d,e in rows(self.replacements) if a),
                name_readings={a:b for a,b in rows(self.readings) if a or b},
                speaker_routes={a:(TTSPolicy[b],c or None) for a,b,c in rows(self.routes) if a},
                unknown_route=TTSPolicy[self.fallback.currentData()],
                unknown_voice=self.unknown_voice.text() or None,
                default_voice=self.default_voice.currentText() or None)

        def update_preview(self):
            try:
                config=self.current_editor_config()
                from .text import TextInterpreter
                u=TextInterpreter(config).parse(self.original.toPlainText())
                self.preview.setPlainText('\n'.join((f'構造規則: {u.structure_rule_id or "不一致"}',
                    f'話者: {u.speaker or "不明"}',f'本文: {u.body}',f'TTS文: {u.tts_text}',
                    f'ASR照合文: {u.asr_target}',f'種類: {u.content_kind.name}',
                    f'音声: {u.audio_route.name} ({u.tts_route.name}) / {u.voice_id or "未割当て"}',
                    f'競合: {", ".join(u.conflicts) or "なし"}',f'設定版: {u.config_revision}')))
                self.issue.setText('プレビューのみ。保存すると次の安全な境界から反映できます。')
            except (ValueError,KeyError) as exc:
                self.issue.setText(f'無効設定: {exc}')

        def save_profile(self):
            try:
                if (self.game_worker is not None and self.game_worker.isRunning() and
                    self.game_name.currentData()!=self.service.catalog.active_game_id):
                    raise ValueError('監視中は対象作品を変更できません。監視終了後に切り替えてください')
                c=self.current_editor_config()
                available=self.service.server.state.voices
                assigned=[c.resolve_voice(name)[1] for name in (*c.speaker_routes,None)]
                missing=[voice for voice in assigned if voice and available and voice not in available]
                if missing:raise ValueError('APIに存在しないVoice ID: '+', '.join(missing))
                self.service.save_game(self.service.current_game_name(),c)
                self.show_status('対象ゲーム',self.service.current_game_name())
                if self.game_worker is not None and self.game_worker.isRunning() and self.live_control is not None:
                    self.live_control.queue_config(c)
                self.issue.setText('保存しました。監視中の変更は次の安全な行境界から適用されます。')
            except (ValueError,OSError,KeyError) as exc:
                self.issue.setText(f'保存できません: {exc}')

        def choose_folder(self):
            if self.game_worker is not None and self.game_worker.isRunning():
                self.issue.setText('監視終了後にIrodoriのインストール先を変更してください。')
                return
            path=QFileDialog.getExistingDirectory(self,'既存Irodori-TTS-Serverのルートフォルダ',
                                                   self.service.preferences.irodori_root)
            if not path:return
            try:
                self.service.configure_server(path)
                self.show_status('Irodoriフォルダ',self.service.preferences.irodori_root)
                self.connect_server(True)
            except (ValueError,OSError) as exc:self.issue.setText(f'フォルダを使用できません: {exc}')

        def connect_server(self,start:bool):
            if self.server_worker is not None and self.server_worker.isRunning():return
            self.show_status('Irodori接続','確認中…')
            self.server_worker=ServerWorker(self.service,start=start)
            self.server_worker.result.connect(self.server_result)
            self.server_worker.failed.connect(lambda x:self.show_status('Irodori接続',x))
            self.server_worker.start()

        def server_result(self,state):
            self.show_status('Irodori接続',state.status+' / '+state.origin+' '+state.message)
            self.show_status('Voice候補',f'API {len(state.voices)}件 / ローカル {len(state.local_candidates)}件')
            self.voices.setPlainText('API: '+', '.join(state.voices)+'\n候補: '+', '.join(state.local_candidates)
               +'\n差異: '+', '.join(state.difference))
            self.routes.setProperty('voice_ids',list(state.voices if state.status=='connected' else state.local_candidates))
            for row in range(self.routes.rowCount()):
                combo=self.routes.cellWidget(row,2)
                if isinstance(combo,QComboBox):
                    selected=combo.currentText()
                    combo.clear();combo.addItems(self.routes.property('voice_ids'))
                    combo.setCurrentText(selected)
            for combo in (self.default_voice,self.sample_voice):
                existing=combo.currentText()
                combo.clear();combo.addItems(state.voices if state.status=='connected' else state.local_candidates)
                combo.setCurrentText(existing)

        def start_luna(self):
            if self.hook_worker and self.hook_worker.isRunning():return
            try:
                game,profile=self.service.active()
                pid=int(self.monitor_fields['pid'].text())
                hwnd=int(self.monitor_fields['hwnd'].text())
                if not self.service.preferences.luna_host_dll:
                    raise ValueError('LunaHost DLLを選択してください')
                if not Path(self.service.preferences.luna_host_dll).is_file():
                    raise ValueError('LunaHost DLLが見つかりません')
                if pid<=0 or hwnd<=0:raise ValueError('対象ウィンドウを選択してください')
            except Exception as exc:
                self.issue.setText('HOOK開始不可: '+str(exc));return
            self.hook_worker=HookWorker(self.service.preferences.luna_host_dll,pid,hwnd,
                                        game.game_id,profile.profile_id,profile.keys())
            self.hook_worker.event.connect(self.hook_event)
            self.hook_worker.start()
            self.monitor_state.setText('HOOK探索・プレビュー中')

        def hook_event(self,data):
            if 'original' in data:self.receive_original(data['original'])
            if 'connected' in data:
                self.service.hook_connected=data['connected']
                self.show_status('HOOK Host接続',data.get('state','停止')+' / '+data.get('reason',''))
                if not self.service.hook_connected:self.disallow_until_verified()
            if 'candidates' in data:
                candidates=data['candidates']
                previous=self.hook_candidates.currentRow()
                self._hook_sources=[src for src,_ in candidates]
                self.hook_candidates.clear()
                for source,text in candidates:
                    self.hook_candidates.addItem(f'{source.hook_code} | PID={source.process_id} | {source.hook_name} | {text[:70]}')
                if 0<=previous<len(candidates):self.hook_candidates.setCurrentRow(previous)
            if 'reason' in data:self.show_status('停止理由',data['reason'])

        def receive_original(self,original:str):
            self.service.latest_original=original
            self.show_status('最新取得原文',original[:200] or '空原文')
            self.original.setPlainText(original)

        def stop_luna(self):
            if self.hook_worker:self.hook_worker.stopping.set()
            self.service.hook_connected=False
            self.disallow_until_verified()
            self.show_status('HOOK Host接続','監視停止')
            self.monitor_state.setText('監視: 停止')

        def disallow_until_verified(self):
            self.service.autoplay_allowed=False
            self.show_status('自動進行許可','無効')
            self.show_status('停止理由','HOOK取得元・音声同期・入力対象が未確認')

        def start_game(self):
            if self.game_worker and self.game_worker.isRunning():return
            if self.hook_worker and self.hook_worker.isRunning():
                self.hook_worker.stopping.set()
                self.hook_worker.finished.connect(self.start_game)
                self.issue.setText('HOOKプレビュー接続を閉じ、監視接続へ切替えます')
                return
            try:
                from .live import LiveControl
                if not self.service.active():raise ValueError('先に対象作品を登録してください')
                if self.monitor_dirty:raise ValueError('監視設定に未保存の変更があります')
                from .acquisition import SourceKind
                if (self.service.active()[1].source().kind is SourceKind.HOOK and
                    not self.service.active()[1].selected_hooks):
                    raise ValueError('HOOK方式ではプロファイルにHOOKを保存してください')
                pid=int(self.monitor_fields['pid'].text())
                hwnd=int(self.monitor_fields['hwnd'].text())
                profile,temporary=self.service.runtime_snapshot(pid,hwnd)
            except (ValueError,OSError,KeyError,TypeError) as exc:
                self.issue.setText(f'監視開始不可: {exc}')
                return
            self.service.latest_original=''
            self.service.hook_connected=False
            self.show_status('最新取得原文','新しい監視セッションの受信待ち')
            self.live_control=LiveControl()
            self.active_target=(profile.pid,profile.hwnd)
            self.game_worker=GameWorker(profile,self.service,self.live_control,temporary)
            self.game_worker.status.connect(self.game_status)
            self.game_worker.finished_status.connect(self.game_finished)
            self.game_worker.start()
            self.allow.setEnabled(True)
            self.pause.setEnabled(True)
            self.show_status('対象ウィンドウ/フォーカス',f'PID={profile.pid} / HWND={profile.hwnd}')
            self.monitor_state.setText('ゲーム音声・本文監視: 起動中')
            self.show_status('自動進行許可','無効（取得元の確認待ち）')

        def grant_live(self):
            if not self.game_worker or not self.game_worker.isRunning() or not self.live_control:return
            if not self.live_control.bound.is_set() or not self.service.latest_original.strip():
                self.show_status('停止理由','監視中ゲームの選択取得元から原文を確認できていません')
                return
            # Authorization is separate from HOOK text and audio; the Controller
            # still checks current input target, audio and manual intervention.
            self.live_control.authorize.set()
            self.show_status('自動進行許可','許可要求済み（Controller検証待ち）')

        def pause_live(self):
            if self.live_control:
                self.live_control.pause.set()
                self.service.autoplay_allowed=False
                self.show_status('自動進行許可','一時停止要求済み')

        def game_status(self,state):
            if 'original' in state:self.receive_original(state['original'])
            if 'acquisition' in state:self.show_status('取得方式',state['acquisition'])
            if 'acquisition_error' in state:self.show_status('停止理由',state['acquisition_error'])
            if 'hook' in state:
                self.service.hook_connected=state['hook'] in ('connected','observing','preview_only')
                self.show_status('HOOK Host接続',state['hook']+' '+state.get('hook_reason',''))
                if not self.service.hook_connected:self.disallow_until_verified()
            if 'monitor' in state:self.monitor_state.setText('ゲーム監視: '+state['monitor'])
            if 'advance' in state:self.show_status('自動進行許可',state['advance'])
            if 'reason' in state:self.show_status('停止理由',state['reason'] or 'なし')
            if 'presentation' in state:self.show_status('提示経路',state['presentation'])
            if 'capture_ok' in state:self.show_status('音声監視',('正常' if state['capture_ok'] else '未確認/異常') if state.get('native_audio_enabled',True) else '無効（TTS・閲覧専用）')
            if 'focused' in state:
                target=(f'PID={self.active_target[0]} HWND={self.active_target[1]}'
                        if self.active_target else '対象未設定')
                self.show_status('対象ウィンドウ/フォーカス',
                                 target + (' / 前面' if state['focused'] else ' / 非フォーカス'))
            if 'sent' in state:self.show_status('入力/次HOOK候補',
                          '次HOOK候補待ち' if state.get('awaiting_next_hook') else '送出済み' if state['sent'] else '未送出')
            if 'controller' in state:self.monitor_state.setText('Controller: '+state['controller'])
            if 'utterance' in state:
                u=state['utterance']
                if u is None:
                    self.active_utterance.setPlainText('新しい通常台詞の確定待ち')
                    self.show_status('現在Utterance','未確定')
                else:
                    self.active_utterance.setPlainText('\n'.join((
                        '原文: '+u['observed_original'],
                        '話者: '+str(u['speaker'] or '不明'),
                        '本文: '+u['body'], 'TTS文: '+u['tts_text'],
                        'ASR文: '+u['asr_target'],
                        '音声: '+u['audio_route']+' ('+u['tts_route']+') / '+str(u['voice_id'] or '未割当て'),
                        '構造規則: '+str(u['structure_rule_id'] or '不一致'),
                        '設定版: '+u['config_revision'])))
                    self.show_status('現在Utterance',str(u['speaker'] or '不明')+' / '+u['body'][:75])
            if state.get('speaker') or state.get('body'):
                self.show_status('対象ゲーム',self.service.current_game_name() or '未選択')

        def game_finished(self,message):
            self.show_status('停止理由',message)
            self.show_status('自動進行許可','無効')
            self.allow.setEnabled(False)
            self.pause.setEnabled(False)
            self.monitor_state.setText('ゲーム監視: 停止')
            self.service.autoplay_allowed=False

        def stop_all(self):
            if self.live_control:self.live_control.stop.set()
            self.stop_luna()
            self.allow.setEnabled(False)
            self.pause.setEnabled(False)
            self.show_status('停止理由','ユーザーによる監視終了')

        def test_voice(self):
            voice=self.sample_voice.currentText()
            try: text=self.original.toPlainText()
            except Exception:return
            from .text import TextInterpreter
            t=TextInterpreter(self.current_editor_config()).parse(text).tts_text
            if not voice or not t:
                self.issue.setText('試聴にはVoice IDと発話文が必要です');return
            from .irodori import IrodoriClient,IrodoriConfig,play_wav
            class PlaybackThread(QThread):
                done=Signal(str)
                def run(inner):
                    try:
                        client=IrodoriClient(IrodoriConfig(url=self.service.preferences.irodori_url+'/v1/audio/speech'))
                        audio=client.synthesize(t,voice)
                        played=play_wav(audio,Event())
                        inner.done.emit('試聴が完了しました' if played else '試聴は中断されました')
                    except Exception as exc:inner.done.emit(f'試聴エラー: {exc}')
            worker=PlaybackThread();worker.done.connect(self.issue.setText)
            self._other_workers.append(worker)
            worker.finished.connect(lambda:self._other_workers.remove(worker))
            worker.start()
        def closeEvent(self,event):
            # Closing never restores input permission. Let Qt process worker
            # shutdown without blocking its event loop on QThread.wait().
            if not self._closing:
                self._closing=True
                self.stop_all()
                self.service.autoplay_allowed=False
            workers=(self.hook_worker,self.game_worker,self.server_worker,*self._other_workers)
            if any(worker is not None and worker.isRunning() for worker in workers):
                self.issue.setText('監視・試聴ワーカーの終了を確認しています')
                event.ignore()
                QTimer.singleShot(120,self.close)
                return
            if self._shutdown_worker is None:
                self._shutdown_worker=ServerShutdown(self.service)
                self._shutdown_worker.failed.connect(self.issue.setText)
                self._shutdown_worker.start()
            if self._shutdown_worker.isRunning():
                self.issue.setText('Irodoriの所有プロセスを終了しています')
                event.ignore()
                QTimer.singleShot(120,self.close)
                return
            super().closeEvent(event)

    app=QApplication.instance() or QApplication([])
    window=Window();window.show()
    return app.exec()
