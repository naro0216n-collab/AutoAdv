"""Isolated low-level Windows input observation for one selected game process.

This process owns the global WH_KEYBOARD_LL/WH_MOUSE_LL callbacks so native
callback failure cannot terminate the GUI.  The callbacks only capture raw
coordinates/foreground HWND and a monotonic timestamp; target ownership checks
and batching happen in the helper's normal message loop.
"""
from __future__ import annotations

import argparse
from collections import deque
import ctypes
import json
import sys
import threading
import time

from .crash_report import install_crash_reporting, record_exception, record_stage
from .windows_input import (
    AUTOADV_INPUT_MARKER,
    GA_ROOT,
    LLKHF_INJECTED,
    WH_KEYBOARD_LL,
    WH_MOUSE_LL,
    WM_INPUT_OBSERVED,
    WM_QUIT,
    _KEY_DOWN,
    _MOUSE_DOWN,
)


def ipc_json(packet: dict) -> str:
    return json.dumps(packet, ensure_ascii=True, separators=(',', ':'))


def _window_pid(user32, hwnd: int) -> int:
    if not hwnd:
        return 0
    root = user32.GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT)
    target = int(root or hwnd)
    from ctypes import wintypes
    pid = wintypes.DWORD()
    if not user32.GetWindowThreadProcessId(ctypes.c_void_p(target), ctypes.byref(pid)):
        return 0
    return int(pid.value)


def run_observer(pid: int, send, *, clock=None) -> None:
    if sys.platform != 'win32':
        raise OSError('Windows input observer requires Windows')
    if pid <= 0:
        raise ValueError('invalid game input observation target')
    clock = clock or (lambda: time.monotonic_ns() // 1_000_000)

    from ctypes import wintypes

    class POINT(ctypes.Structure):
        _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ('pt', POINT),
            ('mouseData', wintypes.DWORD),
            ('flags', wintypes.DWORD),
            ('time', wintypes.DWORD),
            ('dwExtraInfo', ctypes.c_size_t),
        ]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ('vkCode', wintypes.DWORD),
            ('scanCode', wintypes.DWORD),
            ('flags', wintypes.DWORD),
            ('time', wintypes.DWORD),
            ('dwExtraInfo', ctypes.c_size_t),
        ]

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hookproc = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    user32.SetWindowsHookExW.argtypes = (
        ctypes.c_int, hookproc, ctypes.c_void_p, wintypes.DWORD)
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = (
        ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.WindowFromPoint.argtypes = (POINT,)
    user32.WindowFromPoint.restype = ctypes.c_void_p
    user32.GetAncestor.argtypes = (ctypes.c_void_p, wintypes.UINT)
    user32.GetAncestor.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = (
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
    user32.GetMessageW.restype = ctypes.c_int
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    user32.PostThreadMessageW.argtypes = (
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
        wintypes.UINT, wintypes.UINT, wintypes.UINT)
    user32.PeekMessageW.restype = wintypes.BOOL

    keyboard_hook = mouse_hook = None
    thread_id = int(kernel32.GetCurrentThreadId())
    pending = deque()
    wake_posted = False

    # PostThreadMessage requires a created queue before the stop-reader can wake us.
    user32.PeekMessageW(ctypes.byref(wintypes.MSG()), None, 0, 0, 0)

    def request_stop() -> None:
        try:
            for line in sys.stdin:
                try:
                    packet = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if packet == {'action': 'stop'}:
                    user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
                    return
        except Exception:
            return

    threading.Thread(
        target=request_stop, daemon=True, name='autoadv-input-observer-stop').start()

    def relay(kind: str, target: int | tuple[int, int]) -> None:
        nonlocal wake_posted
        try:
            pending.append((kind, target, int(clock())))
            if not wake_posted:
                wake_posted = bool(
                    user32.PostThreadMessageW(thread_id, WM_INPUT_OBSERVED, 0, 0))
        except Exception:
            # Never let Python exceptions cross a low-level hook thunk.
            pass

    @hookproc
    def keyboard_proc(code, message, data):
        if code >= 0 and int(message) in _KEY_DOWN:
            own_injected = False
            if data:
                event = ctypes.cast(data, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                own_injected = (
                    bool(int(event.flags) & LLKHF_INJECTED)
                    and int(event.dwExtraInfo) == AUTOADV_INPUT_MARKER)
            if not own_injected:
                relay('keyboard', int(user32.GetForegroundWindow() or 0))
        return user32.CallNextHookEx(keyboard_hook, code, message, data)

    @hookproc
    def mouse_proc(code, message, data):
        if code >= 0 and int(message) in _MOUSE_DOWN and data:
            point = ctypes.cast(data, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.pt
            relay('mouse', (int(point.x), int(point.y)))
        return user32.CallNextHookEx(mouse_hook, code, message, data)

    # Hold references for the lifetime of SetWindowsHookEx callbacks.
    callbacks = (keyboard_proc, mouse_proc)
    del callbacks

    try:
        module = kernel32.GetModuleHandleW(None)
        keyboard_hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, keyboard_proc, module, 0)
        mouse_hook = user32.SetWindowsHookExW(
            WH_MOUSE_LL, mouse_proc, module, 0)
        if not keyboard_hook or not mouse_hook:
            raise OSError('SetWindowsHookExW failed')
        send({'kind': 'ready'})
        record_stage(f'Windows input observer initialized pid={pid}')

        message = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result <= 0:
                if result < 0:
                    raise OSError('GetMessageW failed')
                break
            if message.message == WM_INPUT_OBSERVED:
                wake_posted = False
                accepted: list[int] = []
                while pending:
                    kind, target, at_ms = pending.popleft()
                    if kind == 'mouse':
                        hwnd = int(user32.WindowFromPoint(POINT(*target)) or 0)
                    else:
                        hwnd = int(target)
                    if _window_pid(user32, hwnd) == pid:
                        accepted.append(at_ms)
                if accepted:
                    send({'kind': 'inputs', 'times': accepted})
                continue
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        if keyboard_hook:
            user32.UnhookWindowsHookEx(keyboard_hook)
        if mouse_hook:
            user32.UnhookWindowsHookEx(mouse_hook)


def main(argv=None) -> int:
    install_crash_reporting('input-observer')
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', required=True, type=int)
    args = parser.parse_args(argv)
    out_lock = threading.Lock()

    def send(packet: dict) -> None:
        with out_lock:
            print(ipc_json(packet), flush=True)

    try:
        run_observer(args.pid, send)
        return 0
    except BaseException as error:
        record_exception('Windows input observer failure', error)
        try:
            send({'kind': 'fatal', 'reason': f'{type(error).__name__}: {error}'})
        except Exception:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
