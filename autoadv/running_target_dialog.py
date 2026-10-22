"""User-facing process picker. No HOOK inference or duplicate process discovery."""
from __future__ import annotations
import ntpath

from .game_windows import WindowCandidate, list_running_windows, list_running_processes


def available_targets(include_background: bool = False) -> tuple[WindowCandidate, ...]:
    windows = list_running_windows()
    if not include_background:
        return windows
    window_pids = {window.pid for window in windows}
    background = [WindowCandidate(p.pid, 0, '', p.executable)
                  for p in list_running_processes() if p.pid not in window_pids]
    return windows + tuple(background)


def choose_running_target(parent):
    """Return an explicitly selected target, or None if the user cancels."""
    from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QDialog, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout)

    dialog = QDialog(parent)
    dialog.setWindowTitle('実行中のゲーム・プロセスを選択')
    dialog.resize(840, 520)
    body = QVBoxLayout(dialog)
    body.addWidget(QLabel('実行中のウィンドウからゲームを選択してください。未登録の作品も登録できます。'))
    search = QLineEdit()
    search.setPlaceholderText('ウィンドウ名・実行ファイル名・PIDで検索')
    body.addWidget(search)
    background = QCheckBox('ウィンドウのないプロセスも表示（送り先ウィンドウは別途選択が必要）')
    body.addWidget(background)
    table = QTableWidget(0, 4)
    table.setHorizontalHeaderLabels(['ウィンドウ名', '実行ファイル', 'PID', 'パス'])
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setColumnWidth(0, 280)
    table.setColumnWidth(1, 160)
    table.setColumnWidth(2, 70)
    table.horizontalHeader().setStretchLastSection(True)
    body.addWidget(table)
    result = {'target': None, 'all': ()}
    status = QLabel('')
    body.addWidget(status)
    row = QHBoxLayout()
    refresh = QPushButton('一覧を更新')
    accept = QPushButton('この対象を使用')
    accept.setEnabled(False)
    cancel = QPushButton('キャンセル')
    row.addWidget(refresh)
    row.addStretch()
    row.addWidget(accept)
    row.addWidget(cancel)
    body.addLayout(row)

    def filter_rows(*_):
        query = search.text().strip().casefold()
        for index, target in enumerate(result['all']):
            display = f'{target.title} {ntpath.basename(target.executable)} {target.pid} {target.executable}'
            table.setRowHidden(index, query not in display.casefold())
        accept.setEnabled(table.currentRow() >= 0 and not table.isRowHidden(table.currentRow()))

    def populate(*_):
        try:
            targets = available_targets(background.isChecked())
        except OSError as exc:
            status.setText(f'一覧取得失敗: {exc}')
            targets = ()
        result['all'] = targets
        table.setRowCount(len(targets))
        for i, target in enumerate(targets):
            for j, text in enumerate((target.title or ('(タイトルなし)' if target.hwnd else '(ウィンドウなし)'),
                                     ntpath.basename(target.executable), str(target.pid), target.executable)):
                table.setItem(i, j, QTableWidgetItem(text))
        status.setText(f'{len(targets)} 件（参照可能な実行中プロセスのみ）')
        filter_rows()

    def selected(*_):
        index = table.currentRow()
        accept.setEnabled(index >= 0 and not table.isRowHidden(index))

    def submit(*_):
        index = table.currentRow()
        if index >= 0 and not table.isRowHidden(index):
            result['target'] = result['all'][index]
            dialog.accept()

    refresh.clicked.connect(populate)
    background.toggled.connect(populate)
    search.textChanged.connect(filter_rows)
    table.itemSelectionChanged.connect(selected)
    table.itemDoubleClicked.connect(submit)
    accept.clicked.connect(submit)
    cancel.clicked.connect(dialog.reject)
    populate()
    dialog.exec()
    return result['target']
