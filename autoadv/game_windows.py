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


def find_game_processes(executable: str) -> tuple[int, ...]:
    """Find process IDs by full image path, including processes without windows.

    A result is a candidate, never proof of ownership or a grant to send input.
    """
    if sys.platform != 'win32':
        raise OSError('Windows process discovery required')
    if not executable.strip():
        raise ValueError('an executable path is required')
    target = ntpath.normcase(ntpath.normpath(executable))
    kernel32 = ctypes.windll.kernel32
    # PROCESSENTRY32W uses ULONG_PTR for th32DefaultHeapID.
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
    kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        raise OSError('CreateToolhelp32Snapshot failed')
    matches = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        alive = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while alive:
            proc = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
            if proc:
                try:
                    buffer = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(buffer))
                    if (kernel32.QueryFullProcessImageNameW(proc, 0, buffer, ctypes.byref(length))
                            and ntpath.normcase(ntpath.normpath(buffer.value)) == target):
                        matches.append(int(entry.th32ProcessID))
                finally:
                    kernel32.CloseHandle(proc)
            alive = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(sorted(set(matches)))
