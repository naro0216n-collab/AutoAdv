"""Identify visible Windows game windows by configured executable, never title alone."""
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


def find_game_windows(executable: str) -> tuple[WindowCandidate, ...]:
    if sys.platform != 'win32':
        raise OSError('Windows window discovery required')
    if not executable.strip():
        raise ValueError('register an executable path before detecting a game')
    target = ntpath.normcase(ntpath.normpath(executable))
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    found = []
    seen = set()
    CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return True
        proc = kernel32.OpenProcess(0x1000, False, pid.value)
        if not proc:
            return True
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(proc, 0, buffer, ctypes.byref(length)):
                return True
            path = buffer.value
        finally:
            kernel32.CloseHandle(proc)
        if ntpath.normcase(ntpath.normpath(path)) != target:
            return True
        key = (pid.value, int(hwnd))
        if key in seen:
            return True
        seen.add(key)
        length = user32.GetWindowTextLengthW(hwnd)
        label = ctypes.create_unicode_buffer(min(length + 1, 4096))
        user32.GetWindowTextW(hwnd, label, len(label))
        found.append(WindowCandidate(pid.value, int(hwnd), label.value, path))
        return True

    callback = CALLBACK(collect)
    if not user32.EnumWindows(callback, 0):
        raise OSError('EnumWindows failed')
    return tuple(found)
