"""Windows input boundaries used by AutoAdv.

Automatic advancement is posted directly to the selected game HWND so it does
not steal focus.  Separately, monitoring can observe low-level keyboard/mouse
input from outside AutoAdv that is actually directed at the selected game
process.  The observer publishes only a monotonic timestamp; it never decides
that the input advanced dialogue or changed product state.
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
from typing import Callable

from .crash_report import record_exception, record_failure

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


def parse_input_observer_packet(packet: dict) -> tuple[str, tuple[int, ...] | str | None]:
    """Validate the isolated input observer's deliberately small IPC protocol."""
    if not isinstance(packet, dict) or not isinstance(packet.get('kind'), str):
        raise ValueError('invalid Windows input observer packet')
    kind = packet['kind']
    if kind == 'ready' and set(packet) == {'kind'}:
        return kind, None
    if kind == 'inputs' and set(packet) == {'kind', 'times'}:
        times = packet['times']
        if (not isinstance(times, list) or not times or
                any(not isinstance(value, int) or value <= 0 for value in times)):
            raise ValueError('invalid Windows input observer input batch')
        return kind, tuple(times)
    if kind == 'fatal' and set(packet) == {'kind', 'reason'}:
        reason = packet['reason']
        if not isinstance(reason, str) or not reason:
            raise ValueError('invalid Windows input observer fatal packet')
        return kind, reason
    raise ValueError('invalid Windows input observer packet')


class WindowsGameInputObserver:
    """Isolate global low-level Windows hooks from the GUI process.

    The helper process owns ``WH_KEYBOARD_LL``/``WH_MOUSE_LL`` and publishes
    only ordered batches of monotonic timestamps that were directed at the
    selected game process.  No key/button is treated as proof that dialogue
    advanced.  AutoAdv's own ``SendInput`` events are tagged and excluded in
    the helper, while other applications' injected input remains factual input.
    """

    def __init__(self, expected_pid: int, on_inputs: Callable[[tuple[int, ...]], None], *,
                 on_error: Callable[[str], None] | None = None):
        if sys.platform != 'win32':
            raise OSError('WindowsGameInputObserver requires Windows')
        if expected_pid <= 0:
            raise ValueError('invalid game input observation target')
        self.pid = int(expected_pid)
        self.on_inputs = on_inputs
        self.on_error = on_error or (lambda _reason: None)
        self.child: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._error_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._closing = threading.Event()

    def start(self) -> None:
        if self.child is not None:
            return
        self._ready.clear()
        self._error = None
        self._closing.clear()
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        child = subprocess.Popen(
            [sys.executable, '-m', 'autoadv.windows_input_worker', '--pid', str(self.pid)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', bufsize=1, creationflags=flags,
        )
        self.child = child
        self._reader_thread = threading.Thread(
            target=self._reader, daemon=True, name='autoadv-game-input-reader')
        self._error_thread = threading.Thread(
            target=self._read_errors, daemon=True, name='autoadv-game-input-errors')
        self._reader_thread.start()
        self._error_thread.start()
        if not self._ready.wait(2.0):
            self.close()
            raise RuntimeError('game input observer did not start')
        if self._error is not None:
            error = self._error
            self.close()
            raise RuntimeError('game input observer failed to start') from error

    def _reader(self) -> None:
        child = self.child
        if child is None or child.stdout is None:
            return
        try:
            for line in child.stdout:
                kind, value = parse_input_observer_packet(json.loads(line))
                if kind == 'ready':
                    self._ready.set()
                elif kind == 'inputs':
                    try:
                        self.on_inputs(value)  # type: ignore[arg-type]
                    except Exception as exc:
                        record_exception('Windows input observer callback failure', exc)
                else:
                    raise RuntimeError(str(value))
        except Exception as exc:
            self._error = exc
            self._ready.set()
            if not self._closing.is_set():
                record_exception('Windows input observer IPC reader failure', exc)
                self.on_error(f'{type(exc).__name__}: {exc}')
        finally:
            if child is not None:
                exit_code = child.poll()
                if exit_code not in (None, 0) and not self._closing.is_set():
                    record_failure(f'Windows input observer exited with code {exit_code}')
                    self.on_error(f'Windows input observer exited with code {exit_code}')

    def _read_errors(self) -> None:
        child = self.child
        if child is None or child.stderr is None:
            return
        for line in child.stderr:
            if line.strip() and not self._closing.is_set():
                self.on_error(line.rstrip())

    def close(self) -> None:
        child, self.child = self.child, None
        if child is None:
            return
        self._closing.set()
        try:
            if child.poll() is None and child.stdin is not None:
                child.stdin.write('{"action":"stop"}\n')
                child.stdin.flush()
                child.wait(timeout=2.0)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            if child.poll() is None:
                child.kill()
        finally:
            try:
                child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2.0)
        for thread in (self._reader_thread, self._error_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._reader_thread = None
        self._error_thread = None
