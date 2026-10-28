"""Crash evidence survives GUI and native-host process failure without a console."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_python(script: str, log_base: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, 'AUTOADV_DATA_DIR': str(log_base)}
    return subprocess.run(
        [sys.executable, '-c', script], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize('role', ['gui', 'hook-host'])
def test_clean_process_does_not_leave_crash_report(tmp_path, role):
    completed = _run_python(
        f"from autoadv.crash_report import install_crash_reporting; "
        f"install_crash_reporting({role!r})", tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    assert list((tmp_path / 'crashes').glob('*.log')) == []


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
    reports = list((tmp_path / 'crashes').glob('*.log'))
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
    reports = list((tmp_path / 'crashes').glob('hook-host-*.log'))
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
    reports = list((tmp_path / 'crashes').glob('hook-host-*.log'))
    assert len(reports) == 1
    report = reports[0].read_text(encoding='utf-8')
    assert 'RuntimeError: host setup rejected' in report
    assert 'native Hook Host startup failure' in report
