"""Windows input boundaries used by AutoAdv.

Automatic advancement is posted directly to the selected game HWND so it does
not steal focus.  Separately, monitoring can observe low-level keyboard/mouse
input from outside AutoAdv that is actually directed at the selected game
process.  The observer publishes only a monotonic timestamp; it never decides
that the input advanced dialogue or changed product state.
"""
from __future__ import annotations

import ctypes
from collections import deque
import sys
import threading
import time
from typing import Callable

from .model import InputMethod


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_LBUTTONDOWN = 0x0201
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207
WM_XBUTTONDOWN = 0x020B
WM_QUIT = 0x0012
WM_INPUT_OBSERVED = 0x802A  # WM_APP message on the hook thread
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
GA_ROOT = 2
VK_MENU = 0x12
VK_F10 = 0x79
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
LLKHF_INJECTED = 0x00000010
AUTOADV_INPUT_MARKER = 0x41554144  # ``AUAD`` in a ULONG_PTR-sized field.

INPUT_TARGET_UNAVAILABLE = 'target_unavailable'
INPUT_FOREGROUND_REQUIRED = 'input_foreground_required'

_EXTENDED_KEYS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E, 0x6F, 0x90, 0x91,
}
_KEY_DOWN = frozenset({WM_KEYDOWN, WM_SYSKEYDOWN})
_MOUSE_DOWN = frozenset({WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN})


def key_message_parameters(virtual_key: int, scan_code: int) -> tuple[int, int, int, int]:
    """Return Win32 key messages/lParams for one physical-style key stroke."""
    if not 1 <= virtual_key <= 254 or not 1 <= scan_code <= 0xFF:
        raise ValueError('invalid Windows virtual key or scancode')
    system_key = virtual_key in (VK_MENU, VK_F10)
    down_message = WM_SYSKEYDOWN if system_key else WM_KEYDOWN
    up_message = WM_SYSKEYUP if system_key else WM_KEYUP
    extended = 1 if virtual_key in _EXTENDED_KEYS else 0
    base = 1 | (scan_code << 16) | (extended << 24)
    # Key-up: previous key state=1 (bit 30), transition state=1 (bit 31).
    return down_message, base, up_message, base | (1 << 30) | (1 << 31)


class WindowsAdvancementInput:
    """Send one configured keyboard press through the selected Windows method."""

    def __init__(self, expected_pid: int, expected_hwnd: int, *,
                 method: InputMethod = InputMethod.WINDOW_MESSAGE,
                 virtual_key: int = 0x0D):
        if sys.platform != 'win32':
            raise OSError('WindowsAdvancementInput requires Windows')
        if expected_pid <= 0 or expected_hwnd <= 0 or not 1 <= virtual_key <= 254:
            raise ValueError('invalid target or advance input configuration')
        self.pid = int(expected_pid)
        self.hwnd = expected_hwnd
        self.method = InputMethod(method)
        self.virtual_key = virtual_key

    @staticmethod
    def availability_reason(expected_pid: int, expected_hwnd: int,
                            method: InputMethod) -> str | None:
        """Return the current factual Windows precondition blocking one input."""
        if sys.platform != 'win32' or expected_pid <= 0 or expected_hwnd <= 0:
            return INPUT_TARGET_UNAVAILABLE
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = (ctypes.c_void_p,)
        user32.IsWindow.restype = ctypes.c_int
        user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = (
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32

        hwnd = ctypes.c_void_p(expected_hwnd)
        if not user32.IsWindow(hwnd):
            return INPUT_TARGET_UNAVAILABLE
        pid = ctypes.c_uint32()
        if (not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                or pid.value != expected_pid):
            return INPUT_TARGET_UNAVAILABLE
        if InputMethod(method) == InputMethod.SEND_INPUT:
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            foreground = int(user32.GetForegroundWindow() or 0)
            if not foreground:
                return INPUT_FOREGROUND_REQUIRED
            target_root = int(user32.GetAncestor(hwnd, GA_ROOT) or expected_hwnd)
            foreground_root = int(user32.GetAncestor(
                ctypes.c_void_p(foreground), GA_ROOT) or foreground)
            if foreground_root != target_root:
                return INPUT_FOREGROUND_REQUIRED
        return None

    def _post(self, message: int, lparam: int) -> None:
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = (
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t)
        user32.PostMessageW.restype = ctypes.c_int
        if not user32.PostMessageW(
                ctypes.c_void_p(self.hwnd), message, self.virtual_key, lparam):
            raise RuntimeError('targeted key message failed; no retry')

    def _scan_code(self) -> int:
        user32 = ctypes.windll.user32
        user32.MapVirtualKeyW.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
        user32.MapVirtualKeyW.restype = ctypes.c_uint32
        scan = user32.MapVirtualKeyW(self.virtual_key, 0)  # MAPVK_VK_TO_VSC
        if not scan or scan > 0xFF:
            raise RuntimeError('configured key has no Windows scancode')
        return int(scan)

    def _post_message(self, scan: int) -> None:
        down_message, down_lparam, up_message, up_lparam = key_message_parameters(
            self.virtual_key, scan)
        # PostMessage preserves queue order without changing global input state.
        # There is no physical key-state polling to satisfy, so an artificial
        # held-key delay would only postpone acknowledgement.
        self._post(down_message, down_lparam)
        self._post(up_message, up_lparam)

    def _send_input(self, scan: int) -> None:
        from ctypes import wintypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ('wVk', wintypes.WORD), ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('dwExtraInfo', ctypes.c_size_t),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD),
                ('wParamH', wintypes.WORD),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ('value',)
            _fields_ = [('type', wintypes.DWORD), ('value', INPUTUNION)]

        # ``input_key`` is a Windows virtual-key setting. Preserve that semantic
        # for system keyboard injection instead of silently translating the
        # configured key into a scan-code-only event. This also matches the
        # ordinary external keyboard stimulus used by the Windows live suite.
        extended = KEYEVENTF_EXTENDEDKEY if self.virtual_key in _EXTENDED_KEYS else 0
        inputs = (INPUT * 2)(
            INPUT(INPUT_KEYBOARD, INPUTUNION(ki=KEYBDINPUT(
                self.virtual_key, scan, extended, 0, AUTOADV_INPUT_MARKER))),
            INPUT(INPUT_KEYBOARD, INPUTUNION(ki=KEYBDINPUT(
                self.virtual_key, scan, extended | KEYEVENTF_KEYUP,
                0, AUTOADV_INPUT_MARKER))),
        )
        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        inserted = int(user32.SendInput(2, inputs, ctypes.sizeof(INPUT)))
        if inserted != 2:
            # A partial insertion may have delivered only KEYDOWN. Best-effort
            # release is cleanup, never a second advancement attempt.
            if inserted == 1:
                try:
                    user32.SendInput(1, ctypes.byref(inputs[1]), ctypes.sizeof(INPUT))
                except Exception:
                    pass
            raise RuntimeError('system keyboard input failed; no retry')

    def advance(self) -> None:
        reason = self.availability_reason(self.pid, self.hwnd, self.method)
        if reason is not None:
            raise RuntimeError(reason)
        scan = self._scan_code()
        if self.method == InputMethod.WINDOW_MESSAGE:
            self._post_message(scan)
        else:
            self._send_input(scan)


class WindowsGameInputObserver:
    """Observe external low-level keyboard/mouse input directed at one game.

    The callback receives only a monotonic timestamp.  No key/button is treated
    as proof that dialogue advanced.  AutoAdv's own advancement path uses
    Window-message advancement never appears here. System-keyboard advancement
    is tagged and ignored by this observer. Low-level input injected by another
    application remains external game input and is retained as the same factual
    timing evidence as hardware input.
    """

    def __init__(self, expected_pid: int, on_input: Callable[[int], None], *,
                 clock: Callable[[], int] | None = None):
        if sys.platform != 'win32':
            raise OSError('WindowsGameInputObserver requires Windows')
        if expected_pid <= 0:
            raise ValueError('invalid game input observation target')
        self.pid = int(expected_pid)
        self.on_input = on_input
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: BaseException | None = None

    @staticmethod
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

    def start(self) -> None:
        if self._thread is not None:
            return
        self._ready.clear()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True, name='autoadv-game-input')
        self._thread.start()
        if not self._ready.wait(2.0):
            self.close()
            raise RuntimeError('game input observer did not start')
        if self._error is not None:
            error = self._error
            self.close()
            raise RuntimeError('game input observer failed to start') from error

    def _run(self) -> None:
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
        hookproc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, hookproc, ctypes.c_void_p, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.WindowFromPoint.argtypes = (POINT,)
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = (ctypes.c_void_p, wintypes.UINT)
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
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
        self._thread_id = int(kernel32.GetCurrentThreadId())

        # Win32 low-level hooks run synchronously before the target window
        # receives its input. Never query the target window (WindowFromPoint can
        # send WM_NCHITTEST), nor invoke application callbacks, in that context.
        # Instead, relay the observed input to this thread's normal message loop.
        pending = deque()

        def relay(kind: str, target: int | tuple[int, int]) -> None:
            try:
                at_ms = int(self.clock())
                if user32.PostThreadMessageW(self._thread_id, WM_INPUT_OBSERVED, 0, 0):
                    pending.append((kind, target, at_ms))
            except Exception:
                # Never let Python exceptions escape through a Win32 hook thunk.
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
            if code >= 0 and int(message) in _MOUSE_DOWN:
                point = ctypes.cast(data, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.pt
                relay('mouse', (int(point.x), int(point.y)))
            return user32.CallNextHookEx(mouse_hook, code, message, data)

        self._keyboard_proc = keyboard_proc
        self._mouse_proc = mouse_proc
        try:
            # PostThreadMessage requires a created message queue.
            user32.PeekMessageW(ctypes.byref(wintypes.MSG()), None, 0, 0, 0)
            module = kernel32.GetModuleHandleW(None)
            keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_proc, module, 0)
            mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_proc, module, 0)
            if not keyboard_hook or not mouse_hook:
                raise OSError('SetWindowsHookExW failed')
            self._ready.set()
            message = wintypes.MSG()
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    if result < 0:
                        raise OSError('GetMessageW failed')
                    break
                if message.message == WM_INPUT_OBSERVED:
                    if not pending:
                        continue
                    kind, target, at_ms = pending.popleft()
                    if kind == 'mouse':
                        hwnd = int(user32.WindowFromPoint(POINT(*target)) or 0)
                    else:
                        hwnd = target
                    if self._window_pid(user32, hwnd) == self.pid:
                        try:
                            self.on_input(at_ms)
                        except Exception:
                            pass
                    continue
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            self._error = exc
            self._ready.set()
        finally:
            if keyboard_hook:
                user32.UnhookWindowsHookEx(keyboard_hook)
            if mouse_hook:
                user32.UnhookWindowsHookEx(mouse_hook)
            self._thread_id = 0

    def close(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread_id = self._thread_id
        if thread_id and sys.platform == 'win32':
            user32 = ctypes.windll.user32
            user32.PostThreadMessageW.argtypes = (
                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t)
            user32.PostThreadMessageW.restype = ctypes.c_int
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
