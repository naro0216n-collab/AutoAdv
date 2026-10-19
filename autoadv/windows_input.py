"""Windows foreground-window bound SendInput; one-way advance authority.

Do not instantiate outside Runtime. SendInput guarantees no visibility into
whether the game accepted a press, hence partial I/O failures are not retried.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import sys

# A source-identification tag, NOT permission to ignore foreign injected input.
# Only this process's explicitly tagged, injected key/mouse events are automatic.
AUTOADV_INPUT_MARK = 0x41564456


def input_types():
    """ABI-complete Win32 INPUT union (40 bytes on 64-bit Windows)."""
    # wintypes.ULONG_PTR does not exist in some Python distributions.
    ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
    LONG, DWORD, WORD, UINT = ctypes.c_int32, ctypes.c_uint32, ctypes.c_uint16, ctypes.c_uint32

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx",LONG),("dy",LONG),
                    ("mouseData",DWORD),("dwFlags",DWORD),
                    ("time",DWORD),("dwExtraInfo",ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk",WORD),("wScan",WORD),
                    ("dwFlags",DWORD),("time",DWORD),
                    ("dwExtraInfo",ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg",DWORD),("wParamL",WORD),
                    ("wParamH",WORD)]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("mi",MOUSEINPUT),("ki",KEYBDINPUT),("hi",HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type",DWORD),("u",INPUTUNION)]

    return INPUT, INPUTUNION, KEYBDINPUT


class WindowsAdvancementInput:
    """Inject one calibrated keyboard key or one mouse-left click.

    Mouse mode uses the cursor's existing position; the screen/foreground
    preflight remains mandatory. No retry after partial SendInput.
    """
    def __init__(self, expected_hwnd: int, *, mode: str = 'keyboard', virtual_key: int = 0x0D):
        if sys.platform != 'win32':
            raise OSError('WindowsAdvancementInput requires Windows')
        if expected_hwnd <= 0 or mode not in ('keyboard', 'mouse_left') or not 1 <= virtual_key <= 254:
            raise ValueError('invalid target or advance input configuration')
        self.hwnd, self.mode, self.virtual_key = expected_hwnd, mode, virtual_key

    def advance(self) -> None:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        if user32.GetForegroundWindow() != self.hwnd:
            raise RuntimeError('target window is not foreground')
        INPUT, INPUTUNION, KEYBDINPUT = input_types()
        if self.mode == 'keyboard':
            strokes = (
                INPUT(1, INPUTUNION(ki=KEYBDINPUT(self.virtual_key, 0, 0, 0, AUTOADV_INPUT_MARK))),
                INPUT(1, INPUTUNION(ki=KEYBDINPUT(self.virtual_key, 0, 2, 0, AUTOADV_INPUT_MARK))),
            )
        else:
            mouse_type = INPUTUNION._fields_[0][1]
            strokes = (
                INPUT(0, INPUTUNION(mi=mouse_type(0, 0, 0, 0x0002, 0, AUTOADV_INPUT_MARK))),
                INPUT(0, INPUTUNION(mi=mouse_type(0, 0, 0, 0x0004, 0, AUTOADV_INPUT_MARK))),
            )
        buffer = (INPUT * 2)(*strokes)
        user32.SendInput.argtypes = (ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = ctypes.c_uint32
        n = user32.SendInput(2, buffer, ctypes.sizeof(INPUT))
        if n != 2:
            raise RuntimeError(f'partial SendInput: {n}/2; no retry')
