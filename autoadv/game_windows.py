"""Discover real Windows processes and visible top-level windows for user selection."""
from __future__ import annotations
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import ntpath
import sys


@dataclass(frozen=True)
class WindowCandidate:
    pid: int
    hwnd: int
    title: str
    executable: str


@dataclass(frozen=True)
class ProcessCandidate:
    pid: int
    executable: str


def _windows_only():
    if sys.platform != 'win32':
        raise OSError('Windows process discovery required')


def _image_path(kernel32, pid: int) -> str:
    process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return ''
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(length)):
            return buffer.value
        return ''
    finally:
        kernel32.CloseHandle(process)


def list_running_processes() -> tuple[ProcessCandidate, ...]:
    """Accessible processes, independent of saved game profiles or window presence."""
    _windows_only()
    kernel32 = ctypes.windll.kernel32

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]

    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise OSError('CreateToolhelp32Snapshot failed')
    matches = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        alive = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while alive:
            if entry.th32ProcessID:
                path = _image_path(kernel32, entry.th32ProcessID)
                if path:
                    matches.append(ProcessCandidate(int(entry.th32ProcessID), path))
            alive = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(sorted(matches, key=lambda p: (ntpath.basename(p.executable).casefold(), p.pid)))


def list_running_windows() -> tuple[WindowCandidate, ...]:
    """Visible user windows, without requiring a game to have been registered."""
    _windows_only()
    user32 = ctypes.windll.user32
    process_paths = {p.pid: p.executable for p in list_running_processes()}
    CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (CALLBACK, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    found = []
    seen = set()

    def collect(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        path = process_paths.get(pid.value)
        if not path:
            return True
        label = ctypes.create_unicode_buffer(min(user32.GetWindowTextLengthW(hwnd) + 1, 4096))
        user32.GetWindowTextW(hwnd, label, len(label))
        key = (int(pid.value), int(hwnd))
        if key not in seen:
            seen.add(key)
            found.append(WindowCandidate(key[0], key[1], label.value, path))
        return True

    if not user32.EnumWindows(CALLBACK(collect), 0):
        raise OSError('EnumWindows failed')
    return tuple(sorted(found, key=lambda w: (w.title.casefold(), w.pid, w.hwnd)))


def find_game_windows(executable: str) -> tuple[WindowCandidate, ...]:
    if not executable.strip():
        raise ValueError('register an executable path before detecting a game')
    target = ntpath.normcase(ntpath.normpath(executable))
    return tuple(w for w in list_running_windows()
                 if ntpath.normcase(ntpath.normpath(w.executable)) == target)


def find_game_processes(executable: str) -> tuple[int, ...]:
    if not executable.strip():
        raise ValueError('an executable path is required')
    target = ntpath.normcase(ntpath.normpath(executable))
    return tuple(p.pid for p in list_running_processes()
                 if ntpath.normcase(ntpath.normpath(p.executable)) == target)
