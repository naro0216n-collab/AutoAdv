"""PySide6 Widgets entrypoint. Settings/preview use the live Application service."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from threading import Event
from .services import AutoAdvService
from .text import GameTextConfig,StructureRule,TextReplacement
from .contracts import TTSPolicy
from .luna import run_origin_listener


def run_gui() -> int:
    from PySide6.QtCore import Qt, Signal, QThread
    from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QGridLayout,
        QFormLayout,QLabel,QLineEdit,QPushButton,QPlainTextEdit,QScrollArea,QTableWidget,
        QTableWidgetItem,QCheckBox,QComboBox,QFileDialog,QMessageBox,QGroupBox,QSplitter)

    class ServerWorker(QThread):
        result=Signal(object)
        failed=Signal(str)
        def __init__(self, service:AutoAdvService, *,start:bool):
            super().__init__();self.service=service;self.start_server=start
        def run(self):
            try:self.result.emit(self.service.connect_server() if self.start_server else self.service.refresh_voices())
            except Exception as exc:self.failed.emit(f'{type(exc).__name__}: {exc}')

    class LunaWorker(QThread):
        received=Signal(str)
        status=Signal(bool,str)
        def __init__(self,url:str):
            super().__init__();self.url=url;self.stopping=Event()
        def run(self):
            try:run_origin_listener(self.url,self.stopping,
                lambda observation:self.received.emit(observation.text),
                lambda ok,message:self.status.emit(ok,message))
            except Exception as exc:self.status.emit(False,str(exc))

    class GameWorker(QThread):
        status=Signal(object)
        finished_status=Signal(str)
        def __init__(self,config,service,control):
            super().__init__()
            self.config=config
            self.preferences=service.preferences
            self.game_config=service.current_config()
            self.control=control
        def run(self):
            from .live import run_windows
            try:
                code=run_windows(self.config,preferences=self.preferences,
                    game_config=self.game_config,control=self.control,
                    status=lambda state:self.status.emit(state),enable_input=True)
                self.finished_status.emit('Monitor ended (code '+str(code)+')')
            except Exception as exc:
                self.finished_status.emit(f'{type(exc).__name__}: {exc}')

    def table(headers, minimum=180):
        t=QTableWidget(0,len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setMinimumHeight(minimum)
        t.horizontalHeader().setStretchLastSection(True)
        return t

    def append(t, values):
        row=t.rowCount();t.insertRow(row)
        for i,value in enumerate(values):
            if t.property('voice_routing') and i==2:
                combo=QComboBox();combo.setEditable(True)
                combo.addItems(t.property('voice_ids') or ())
                combo.setCurrentText(str(value))
                t.setCellWidget(row,i,combo)
            else:t.setItem(row,i,QTableWidgetItem(str(value)))

    def rows(t):
        return [[t.cellWidget(r,c).currentText() if isinstance(t.cellWidget(r,c),QComboBox)
                 else t.item(r,c).text() if t.item(r,c) is not None else ''
                 for c in range(t.columnCount())] for r in range(t.rowCount())]

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.service=AutoAdvService()
            self.server_worker=None
            self.luna_worker=None
            self.game_worker=None
            self.live_control=None
            self._other_workers=[]
            self.setWindowTitle('AutoAdv — 音声同期オート進行')
            self.resize(1160,920)
            top=QWidget();self.setCentralWidget(top)
            root=QVBoxLayout(top)
            statuses=QGroupBox('接続・音声・進行状態（常時表示）');root.addWidget(statuses)
            status_grid=QGridLayout(statuses)
            fields=['対象ゲーム','対象ウィンドウ/フォーカス','Luna 接続','最新Luna原文',
                    'Irodoriフォルダ','Irodori接続','Voice候補','提示経路','音声監視',
                    '自動進行許可','停止理由','入力/ACK']
            self.status_labels={}
            for i,name in enumerate(fields):
                label=QLabel(name);value=QLabel('未設定 / 不明');value.setWordWrap(True)
                status_grid.addWidget(label,i//2,2*(i%2));status_grid.addWidget(value,i//2,2*(i%2)+1)
                self.status_labels[name]=value
            controls=QHBoxLayout();root.addLayout(controls)
            self.game_name=QComboBox();self.game_name.setEditable(True)
            self.game_name.addItems(sorted(self.service.preferences.games))
            self.game_name.setCurrentText(self.service.preferences.selected_game)
            controls.addWidget(QLabel('作品'));controls.addWidget(self.game_name,1)
            self.folder=QPushButton('Irodoriフォルダ指定');controls.addWidget(self.folder)
            self.retry=QPushButton('Irodori再接続・Voice更新');controls.addWidget(self.retry)
            self.luna_button=QPushButton('Luna原文プレビュー開始');controls.addWidget(self.luna_button)
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
            runtime_box=QHBoxLayout();root.addLayout(runtime_box)
            self.live_path=QLineEdit(self.service.preferences.live_config)
            runtime_box.addWidget(QLabel('作品別ゲーム/画面/音声設定'))
            runtime_box.addWidget(self.live_path,1)
            choose_runtime=QPushButton('ファイル選択')
            runtime_box.addWidget(choose_runtime)
            def browse_runtime():
                path,_=QFileDialog.getOpenFileName(self,'ゲーム監視プロファイル',self.live_path.text(),'JSON (*.json)')
                if path:
                    self.live_path.setText(path)
                    self.service.preferences.live_config=path
                    self.service.preferences.save(self.service.settings_file)
            choose_runtime.clicked.connect(browse_runtime)
            splitter=QSplitter(Qt.Orientation.Horizontal);root.addWidget(splitter,1)
            left=QScrollArea();left.setWidgetResizable(True);splitter.addWidget(left)
            pane=QWidget();form=QVBoxLayout(pane);left.setWidget(pane)
            self.rules=table(['規則ID','名前付き正規表現','有効(1/0)','適用条件'])
            self.replacements=table(['置換対象/原語','置換結果','正規表現(1/0)','有効(1/0)','用途(tts/asr/both)'])
            self.readings=table(['名前・漢字','読み'])
            self.routes=table(['話者','SELECTED/DEFAULT/SKIP/UNASSIGNED','Voice ID'])
            self.routes.setProperty('voice_routing',True)
            for title,t in [('話者・本文分離（優先順位は上から）',self.rules),
                            ('読み置換（優先順位は上から）',self.replacements),
                            ('読み辞書',self.readings),('話者ごとのTTS',self.routes)]:
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
            self.fallback=QComboBox();self.fallback.addItems(['UNASSIGNED','SKIP','DEFAULT','SELECTED'])
            self.unknown_voice=QLineEdit();self.default_voice=QComboBox();self.default_voice.setEditable(True)
            defaults=QFormLayout();defaults.addRow('未知話者の動作',self.fallback)
            defaults.addRow('未知話者のVoice ID',self.unknown_voice)
            defaults.addRow('既定Voice ID',self.default_voice);form.addLayout(defaults)
            self.save=QPushButton('作品ルール・Voice割当てを保存');form.addWidget(self.save)
            right=QWidget();right_layout=QVBoxLayout(right);splitter.addWidget(right)
            right_layout.addWidget(QLabel('Luna原文（受信内容は加工しない）'))
            self.original=QPlainTextEdit();self.original.setPlaceholderText('LunaのHOOK結合結果を確認してください')
            self.original.setMinimumHeight(140);right_layout.addWidget(self.original)
            right_layout.addWidget(QLabel('実処理と共通のUtteranceプレビュー'))
            self.preview=QPlainTextEdit();self.preview.setReadOnly(True)
            self.preview.setMinimumHeight(140);right_layout.addWidget(self.preview)
            self.test=QPushButton('本文分離・読み置換をテスト');right_layout.addWidget(self.test)
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
            self.game_name.activated.connect(lambda _:self.game_changed(self.game_name.currentText()))
            self.original.textChanged.connect(self.update_preview)
            self.test.clicked.connect(self.update_preview)
            self.save.clicked.connect(self.save_profile)
            self.try_voice.clicked.connect(self.test_voice)
            self.load_profile()
            self.show_status('自動進行許可','無効（毎回明示操作が必要）')
            if self.service.preferences.irodori_root:self.connect_server(True)

        def show_status(self,key,value):
            self.status_labels[key].setText(str(value))

        def load_profile(self):
            c=self.service.current_config()
            tables=[(self.rules,[(r.rule_id,r.pattern,'1' if r.enabled else '0',r.condition) for r in c.structure_rules]),
                    (self.replacements,[(r.pattern,r.replacement,'1' if r.regex else '0','1' if r.enabled else '0',r.target) for r in c.replacements]),
                    (self.readings,list(c.name_readings.items())),
                    (self.routes,[(name,policy.name,voice or '') for name,(policy,voice) in c.speaker_routes.items()])]
            for t,data in tables:
                t.setRowCount(0)
                for row in data:append(t,row)
            self.fallback.setCurrentText(c.unknown_route.name)
            self.unknown_voice.setText(c.unknown_voice or '')
            self.default_voice.setCurrentText(c.default_voice or '')
            self.show_status('対象ゲーム',self.service.preferences.selected_game or '未選択')
            self.show_status('Irodoriフォルダ',self.service.preferences.irodori_root or '未設定')
            self.update_preview()

        def game_changed(self,text):
            # Changes are intentionally explicit; typing a new name must not discard editor state.
            if self.game_worker and self.game_worker.isRunning():
                self.pause_live()
                return
            self.service.preferences.selected_game=text
            self.load_profile()

        def current_editor_config(self):
            return GameTextConfig(
                structure_rules=tuple(StructureRule(a,b,c.strip()!='0',d) for a,b,c,d in rows(self.rules) if a and b),
                replacements=tuple(TextReplacement(a,b,c.strip()=='1',d.strip()!='0',e or 'tts')
                                   for a,b,c,d,e in rows(self.replacements) if a),
                name_readings={a:b for a,b in rows(self.readings) if a or b},
                speaker_routes={a:(TTSPolicy[b],c or None) for a,b,c in rows(self.routes) if a},
                unknown_route=TTSPolicy[self.fallback.currentText()],
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
                    f'Voice: {u.tts_route.name} / {u.voice_id or "未割当て"}',
                    f'競合: {", ".join(u.conflicts) or "なし"}',f'設定版: {u.config_revision}')))
                self.issue.setText('プレビューのみ。保存すると次の安全な境界から反映できます。')
            except (ValueError,KeyError) as exc:
                self.issue.setText(f'無効設定: {exc}')

        def save_profile(self):
            try:
                c=self.current_editor_config()
                available=self.service.server.state.voices
                assigned=[voice for policy,voice in c.speaker_routes.values()
                          if policy in (TTSPolicy.SELECTED,TTSPolicy.DEFAULT) and (voice or c.default_voice)]
                if c.unknown_route is TTSPolicy.SELECTED:assigned.append(c.unknown_voice)
                if c.unknown_route is TTSPolicy.DEFAULT or any(p is TTSPolicy.DEFAULT for p,_ in c.speaker_routes.values()):
                    assigned.append(c.default_voice)
                missing=[voice for voice in assigned if voice and available and voice not in available]
                if missing:raise ValueError('APIに存在しないVoice ID: '+', '.join(missing))
                self.service.save_game(self.game_name.currentText(),c)
                self.show_status('対象ゲーム',self.service.preferences.selected_game)
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
            if self.luna_worker and self.luna_worker.isRunning():return
            self.luna_worker=LunaWorker(self.service.preferences.luna_url)
            self.luna_worker.received.connect(self.receive_original)
            self.luna_worker.status.connect(self.luna_status)
            self.luna_worker.start()
            self.monitor_state.setText('Luna公開WS監視: 開始')
        def luna_status(self,ok:bool,message:str):
            self.service.luna_connected=ok
            self.show_status('Luna 接続',message)
            if not ok:self.disallow_until_verified()
        def receive_original(self,original:str):
            self.service.latest_original=original
            self.show_status('最新Luna原文',original[:200] or '空原文')
            self.original.setPlainText(original)
        def stop_luna(self):
            if self.luna_worker:
                self.luna_worker.stopping.set()
            self.service.luna_connected=False
            self.disallow_until_verified()
            self.show_status('Luna 接続','監視停止')
            self.monitor_state.setText('監視: 停止')
        def disallow_until_verified(self):
            self.service.autoplay_allowed=False
            self.show_status('自動進行許可','無効')
            self.show_status('停止理由','実ゲーム・画面・音声の許可条件が未確証')
        def start_game(self):
            if self.game_worker and self.game_worker.isRunning():return
            try:
                from .live import LiveConfig,LiveControl
                profile=LiveConfig.load(Path(self.live_path.text()))
                if not self.service.preferences.selected_game:raise ValueError('先に対象作品を保存してください')
                self.service.preferences.live_config=self.live_path.text()
                self.service.preferences.save(self.service.settings_file)
            except (ValueError,OSError,KeyError) as exc:
                self.issue.setText(f'監視開始不可: {exc}')
                return
            # The running game uses the saved profile snapshot, not unsaved widgets.
            self.stop_luna()
            self.live_control=LiveControl()
            self.game_worker=GameWorker(profile,self.service,self.live_control)
            self.game_worker.status.connect(self.game_status)
            self.game_worker.finished_status.connect(self.game_finished)
            self.game_worker.start()
            self.allow.setEnabled(True)
            self.pause.setEnabled(True)
            self.show_status('対象ウィンドウ/フォーカス',f'PID={profile.pid} / HWND={profile.hwnd}')
            self.monitor_state.setText('ゲーム音声・画面監視: 起動中')
            self.show_status('自動進行許可','無効（対象と原文を確認してください）')

        def grant_live(self):
            if not self.game_worker or not self.game_worker.isRunning() or not self.live_control:return
            # This user action confirms the selected Luna hook, target window,
            # and last original correspond to the same game. The worker still
            # requires a live WS connection, window focus and fresh preflight.
            self.live_control.bound.set()
            self.live_control.authorize.set()
            self.show_status('自動進行許可','許可要求済み（Controller検証待ち）')

        def pause_live(self):
            if self.live_control:
                self.live_control.pause.set()
                self.disallow_until_verified()

        def game_status(self,state):
            if 'original' in state:self.receive_original(state['original'])
            if 'luna' in state:self.show_status('Luna 接続',state['luna']+' '+state.get('luna_reason',''))
            if 'monitor' in state:self.monitor_state.setText('ゲーム監視: '+state['monitor'])
            if 'advance' in state:self.show_status('自動進行許可',state['advance'])
            if 'reason' in state:self.show_status('停止理由',state['reason'] or 'なし')
            if 'presentation' in state:self.show_status('提示経路',state['presentation'])
            if 'capture_ok' in state:self.show_status('音声監視','正常' if state['capture_ok'] else '未確認/異常')
            if 'focused' in state:self.show_status('対象ウィンドウ/フォーカス',
                          '対象が前面' if state['focused'] else '非フォーカス')
            if 'sent' in state:self.show_status('入力/ACK',
                          '遷移ACK待ち' if state.get('awaiting_ack') else '送出済み' if state['sent'] else '未送出')
            if 'controller' in state:self.monitor_state.setText('Controller: '+state['controller'])
            if state.get('speaker') or state.get('body'):
                self.show_status('対象ゲーム',self.service.preferences.selected_game+' / '+str(state.get('speaker') or '話者不明'))

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
            self.stop_all()
            self.service.autoplay_allowed=False
            if self.luna_worker is not None and self.luna_worker.isRunning():
                self.luna_worker.stopping.set()
                self.luna_worker.wait(3500)
            if self.game_worker is not None and self.game_worker.isRunning():
                self.live_control.stop.set()
                self.game_worker.wait(5000)
            if any(worker is not None and worker.isRunning() for worker in
                   (self.luna_worker,self.game_worker,self.server_worker,*self._other_workers)):
                self.issue.setText('ワーカー停止中です。完了後に閉じてください。')
                event.ignore()
                return
            self.service.close()
            super().closeEvent(event)

    app=QApplication.instance() or QApplication([])
    window=Window();window.show()
    return app.exec()
