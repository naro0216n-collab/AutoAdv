"""Crash evidence survives GUI and native-host process failure without a console."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_python(script: str, log_base: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, 'AUTOADV_CRASH_DIR': str(log_base)}
    return subprocess.run(
        [sys.executable, '-c', script], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize('role', ['gui', 'hook-host', 'input-observer'])
def test_clean_process_does_not_leave_crash_report(tmp_path, role):
    completed = _run_python(
        f"from autoadv.crash_report import install_crash_reporting; "
        f"install_crash_reporting({role!r})", tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    assert list(tmp_path.glob('*.log')) == []


@pytest.mark.parametrize('script,expected', [
    ("from autoadv.crash_report import install_crash_reporting; "
     "install_crash_reporting('gui'); import sys; sys.stderr = None; "
     "raise ValueError('GUI failure')", 'ValueError: GUI failure'),
    ("from autoadv.crash_report import install_crash_reporting; "
     "install_crash_reporting('gui'); import threading; "
     "t = threading.Thread(target=lambda: 1 / 0, name='audio-callback'); "
     "t.start(); t.join()", 'ZeroDivisionError: division by zero'),
    ("from autoadv.crash_report import install_crash_reporting, record_exception; "
     "install_crash_reporting('hook-host'); "
     "exec('try:\\n raise RuntimeError(\\\"native init\\\")\\nexcept RuntimeError as e:\\n record_exception(\\\"native hook failure\\\", e)')",
     'RuntimeError: native init'),
])
def test_python_failure_is_persisted_with_full_traceback(tmp_path, script, expected):
    result = _run_python(script, tmp_path)
    reports = list(tmp_path.glob('*.log'))
    assert len(reports) == 1, result.stderr
    report = reports[0].read_text(encoding='utf-8')
    assert expected in report
    assert 'Traceback (most recent call last)' in report
    assert 'PID ' in report


def test_native_process_abort_preserves_faulthandler_trace(tmp_path):
    script = ("import os; from autoadv.crash_report import install_crash_reporting; "
              "install_crash_reporting('hook-host'); os.abort()")
    result = _run_python(script, tmp_path)
    assert result.returncode != 0
    reports = list(tmp_path.glob('hook-host-*.log'))
    assert len(reports) == 1
    report = reports[0].read_text(encoding='utf-8')
    assert 'Fatal Python error' in report


def test_handled_native_host_startup_failure_is_recorded(tmp_path):
    script = """
from pathlib import Path
from unittest.mock import patch
from autoadv.hook_host_worker import main
with patch('autoadv.hook_host_worker.NativeHost', side_effect=RuntimeError('host setup rejected')):
    raise SystemExit(main(['--dll', str(Path('unused.dll'))]))
"""
    result = _run_python(script, tmp_path)
    assert result.returncode == 1
    assert '"kind": "fatal"' in result.stdout
    reports = list(tmp_path.glob('hook-host-*.log'))
    assert len(reports) == 1
    report = reports[0].read_text(encoding='utf-8')
    assert 'RuntimeError: host setup rejected' in report
    assert 'native Hook Host startup failure' in report


def test_default_report_directory_is_crash_folder_beside_launched_product(monkeypatch):
    from autoadv.crash_report import crash_directory
    monkeypatch.delenv('AUTOADV_CRASH_DIR', raising=False)
    monkeypatch.setenv('AUTOADV_DATA_DIR', str(ROOT / 'arbitrary-runtime-data'))
    monkeypatch.chdir(ROOT)
    assert crash_directory() == ROOT / 'crash'


def test_abrupt_exit_preserves_last_reached_stage(tmp_path):
    result = _run_python(
        "from autoadv.crash_report import install_crash_reporting, record_stage; "
        "import os; install_crash_reporting('gui'); "
        "record_stage('native audio model initialization started'); os._exit(35)",
        tmp_path,
    )
    assert result.returncode == 35
    reports = list(tmp_path.glob('gui-*.log'))
    assert len(reports) == 1
    assert 'stage: native audio model initialization started' in reports[0].read_text(encoding='utf-8')


@pytest.mark.parametrize('role', ['gui', 'hook-host', 'input-observer'])
def test_active_hook_evidence_survives_clean_process_exit(tmp_path, role):
    result = _run_python(
        "from autoadv.crash_report import install_crash_reporting, record_stage; "
        f"install_crash_reporting({role!r}); "
        "record_stage('hook attach requested pid=42', retain=True)",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    reports = list(tmp_path.glob(f'{role}-*.log'))
    assert len(reports) == 1
    assert 'stage: hook attach requested pid=42' in reports[0].read_text(encoding='utf-8')


def test_native_hook_session_report_survives_explicit_gui_shutdown(tmp_path):
    script = '''
import io
import sys
from unittest.mock import patch
from autoadv.hook_host_worker import main
sys.stdin = io.StringIO('{"action":"connect","pid":42}\\n{"action":"stop"}\\n')
with patch('autoadv.hook_host_worker.NativeHost') as native:
    native.return_value.command.side_effect = [True, False]
    raise SystemExit(main(['--dll', 'unused.dll']))
'''
    result = _run_python(script, tmp_path)
    assert result.returncode == 0, result.stderr
    native_logs = list(tmp_path.glob('hook-host-*.log'))
    assert len(native_logs) == 1
    assert 'hook attach requested pid=42' in native_logs[0].read_text(encoding='utf-8')
    assert 'native Hook Host initialized' in native_logs[0].read_text(encoding='utf-8')
