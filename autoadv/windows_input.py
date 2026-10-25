"""Windows advancement input used by AutoAdv.

Advancement is posted directly to the selected game HWND so it does not steal
focus or depend on the foreground window.
"""
from __future__ import annotations

import ctypes
import sys


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_MENU = 0x12
VK_F10 = 0x79

_EXTENDED_KEYS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E, 0x6F, 0x90, 0x91,
}


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
    """Post one configured keyboard press to the selected game window."""

    def __init__(self, expected_hwnd: int, *, virtual_key: int = 0x0D):
        if sys.platform != 'win32':
            raise OSError('WindowsAdvancementInput requires Windows')
        if expected_hwnd <= 0 or not 1 <= virtual_key <= 254:
            raise ValueError('invalid target or advance input configuration')
        self.hwnd = expected_hwnd
        self.virtual_key = virtual_key

    def _post(self, message: int, lparam: int) -> None:
        user32 = ctypes.windll.user32
        user32.PostMessageW.argtypes = (
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t)
        user32.PostMessageW.restype = ctypes.c_int
        if not user32.PostMessageW(
                ctypes.c_void_p(self.hwnd), message, self.virtual_key, lparam):
            raise RuntimeError('targeted key message failed; no retry')

    def advance(self) -> None:
        user32 = ctypes.windll.user32
        user32.MapVirtualKeyW.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
        user32.MapVirtualKeyW.restype = ctypes.c_uint32
        scan = user32.MapVirtualKeyW(self.virtual_key, 0)  # MAPVK_VK_TO_VSC
        if not scan or scan > 0xFF:
            raise RuntimeError('configured key has no Windows scancode')
        down_message, down_lparam, up_message, up_lparam = key_message_parameters(
            self.virtual_key, scan)
        # PostMessage preserves queue order without changing global input state.
        # There is no physical key-state polling to satisfy, so an artificial
        # held-key delay would only postpone acknowledgement.
        self._post(down_message, down_lparam)
        self._post(up_message, up_lparam)
