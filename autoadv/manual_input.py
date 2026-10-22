"""Windows physical-input observation scoped to the selected game window."""
from __future__ import annotations
from dataclasses import dataclass
from collections import deque
from threading import Event, Thread, Lock
import ctypes
import sys
import time
from .windows_input import AUTOADV_INPUT_MARK


def is_manual_hook_event(*, keyboard: bool, flags: int, extra_info: int) -> bool:
    """Return False only for AutoAdv's own tagged SendInput event."""
    injected_bit = 0x10 if keyboard else 0x01
    return not (flags & injected_bit and extra_info == AUTOADV_INPUT_MARK)


def targets_selected_window(*, target_root: int, target_pid: int,
                            event_root: int, event_pid: int) -> bool:
    """Pure target-scope predicate used by the Windows hook and unit tests."""
    return bool(target_root and target_pid and
                event_root == target_root and event_pid == target_pid)


@dataclass(frozen=True)
class ManualTargetInput:
    serial: int
    observed_ns: int
    keyboard: bool
    code: int
    message: int

    def is_advance_candidate(self, advance_key: int) -> bool:
        if self.keyboard:
            return self.message in (0x0100, 0x0104) and self.code == advance_key
        return self.message == 0x0201


class WindowsManualMonitor:
    """Latch only physical/foreign-injected input delivered to the selected game.

    Low-level hooks are system-wide because Windows exposes them that way, but
    events are ignored unless their destination resolves to the selected input
    window root and PID.  AutoAdv's own tagged SendInput is also ignored.
    There is intentionally no GetLastInputInfo fallback: that API is global and
    cannot prove which application received the input.
    """
    def __init__(self, target_hwnd: int, target_pid: int):
        if sys.platform != 'win32':
            raise OSError('Windows required')
        if not target_hwnd or not target_pid:
            raise ValueError('target HWND and PID are required')
        self.target_hwnd = int(target_hwnd)
        self.target_pid = int(target_pid)
        self._manual = Event()
        self._event_lock = Lock()
        self._ready = Event()
        self._target_inputs: deque[ManualTargetInput] = deque(maxlen=64)
        self._target_serial = 0
        self._stop = Event()
        self._thread_id = 0
        self._error: Exception | None = None
        self._thread = Thread(target=self._monitor, name='autoadv-input-monitor', daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self._error is not None:
            self.close()
            raise RuntimeError('unable to install low-level input observation hooks') from self._error

    def _monitor(self) -> None:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
        CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)

        class KBD(ctypes.Structure):
            _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD),
                        ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', PTR)]

        class MOUSE(ctypes.Structure):
            _fields_ = [('pt', wintypes.POINT), ('mouseData', wintypes.DWORD),
                        ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', PTR)]

        class MSG(ctypes.Structure):
            _fields_ = [('hwnd', wintypes.HWND), ('message', wintypes.UINT),
                        ('wParam', ctypes.c_size_t), ('lParam', ctypes.c_ssize_t),
                        ('time', wintypes.DWORD), ('pt', wintypes.POINT), ('lPrivate', wintypes.DWORD)]

        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.WindowFromPoint.argtypes = (wintypes.POINT,)
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
        user32.ClientToScreen.restype = wintypes.BOOL
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, CALLBACK, ctypes.c_void_p, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = (ctypes.POINTER(MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT)
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = (ctypes.POINTER(MSG),)
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(MSG),)
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)

        GA_ROOT = 2
        target_root = user32.GetAncestor(wintypes.HWND(self.target_hwnd), GA_ROOT) or self.target_hwnd

        def window_identity(hwnd: int) -> tuple[int, int]:
            if not hwnd:
                return 0, 0
            root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or hwnd
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(root), ctypes.byref(pid))
            return int(root), int(pid.value)

        def event_targets_game(*, keyboard: bool, info) -> bool:
            hwnd = user32.GetForegroundWindow() if keyboard else user32.WindowFromPoint(info.pt)
            root, pid = window_identity(hwnd)
            if not targets_selected_window(target_root=int(target_root), target_pid=self.target_pid,
                                           event_root=root, event_pid=pid):
                return False
            if keyboard:
                return True
            # Only client-area mouse operations can be dialogue/choice input.
            # A title-bar click used merely to focus the game must not become a
            # manual dialogue boundary candidate.
            rect = wintypes.RECT()
            if not user32.GetClientRect(wintypes.HWND(self.target_hwnd), ctypes.byref(rect)):
                return False
            top_left = wintypes.POINT(rect.left, rect.top)
            bottom_right = wintypes.POINT(rect.right, rect.bottom)
            if (not user32.ClientToScreen(wintypes.HWND(self.target_hwnd), ctypes.byref(top_left)) or
                    not user32.ClientToScreen(wintypes.HWND(self.target_hwnd), ctypes.byref(bottom_right))):
                return False
            return (top_left.x <= info.pt.x < bottom_right.x and
                    top_left.y <= info.pt.y < bottom_right.y)

        hooks = []

        def check_event(*, keyboard, code, message, lparam):
            if code < 0:
                return
            try:
                struct = KBD if keyboard else MOUSE
                info = ctypes.cast(lparam, ctypes.POINTER(struct)).contents
                msg = int(message)
                if not event_targets_game(keyboard=keyboard, info=info):
                    return
                if not is_manual_hook_event(keyboard=keyboard, flags=info.flags,
                                            extra_info=info.dwExtraInfo):
                    return

                keyboard_down = keyboard and msg in (0x0100, 0x0104)
                mouse_down = (not keyboard and msg in (0x0201, 0x0204, 0x0207, 0x020B))
                mouse_wheel = (not keyboard and msg in (0x020A, 0x020E))
                if not (keyboard_down or mouse_down or mouse_wheel):
                    return

                with self._event_lock:
                    self._manual.set()
                    if keyboard_down or mouse_down:
                        self._target_serial += 1
                        self._target_inputs.append(ManualTargetInput(
                            self._target_serial, time.monotonic_ns(), keyboard,
                            int(info.vkCode if keyboard else msg), msg))
            except Exception:
                # If the target-scoped hook itself becomes unreliable, fail
                # closed for automatic input rather than guessing ownership.
                self._manual.set()

        def keyboard(code, wparam, lparam):
            check_event(keyboard=True, code=code, message=wparam, lparam=lparam)
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def mouse(code, wparam, lparam):
            check_event(keyboard=False, code=code, message=wparam, lparam=lparam)
            return user32.CallNextHookEx(None, code, wparam, lparam)

        keyboard_cb, mouse_cb = CALLBACK(keyboard), CALLBACK(mouse)
        try:
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            for hook_id, cb in ((13, keyboard_cb), (14, mouse_cb)):
                hook = user32.SetWindowsHookExW(hook_id, cb, None, 0)
                if not hook:
                    raise OSError(f'cannot install input hook {hook_id}')
                hooks.append(hook)
            self._ready.set()
            msg = MSG()
            while not self._stop.is_set():
                status = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if status <= 0:
                    if status == -1:
                        raise OSError('low-level input hook message loop failed')
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            if not self._stop.is_set():
                self._manual.set()
        except Exception as exc:
            self._error = exc
            self._manual.set()
            self._ready.set()
        finally:
            for hook in reversed(hooks):
                user32.UnhookWindowsHookEx(hook)

    def intervened(self) -> bool:
        return bool(self._manual.is_set() or not self._thread.is_alive() or self._error is not None)

    def target_inputs_since(self, serial: int) -> tuple[ManualTargetInput, ...]:
        with self._event_lock:
            return tuple(item for item in self._target_inputs if item.serial > serial)

    @property
    def target_input_serial(self) -> int:
        with self._event_lock:
            return self._target_serial

    def reset_after_authorization(self):
        self._manual.clear()

    def close(self):
        self._stop.set()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
        if self._thread.is_alive():
            self._thread.join(timeout=2)
