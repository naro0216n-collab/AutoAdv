"""Windows foreground-window bound SendInput; one-way advance authority.

Do not instantiate outside Runtime. SendInput guarantees no visibility into
whether the game accepted a press, hence partial I/O failures are not retried.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import sys


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


class WindowsEnterInput:
    def __init__(self, expected_hwnd: int):
        if sys.platform != "win32":
            raise OSError("WindowsEnterInput requires Windows")
        if expected_hwnd <= 0:
            raise ValueError("target HWND is required")
        self.hwnd = expected_hwnd

    def advance(self) -> None:
        user32=ctypes.windll.user32
        user32.GetForegroundWindow.restype=ctypes.c_void_p
        if user32.GetForegroundWindow() != self.hwnd:
            raise RuntimeError("target window is not foreground")
        INPUT, INPUTUNION, KEYBDINPUT = input_types()
        VK_RETURN, KEYEVENTF_KEYUP, INPUT_KEYBOARD = 0x0D, 0x0002, 1
        strokes=(INPUT(INPUT_KEYBOARD, INPUTUNION(ki=KEYBDINPUT(VK_RETURN,0,0,0,0))),
                 INPUT(INPUT_KEYBOARD, INPUTUNION(ki=KEYBDINPUT(VK_RETURN,0,KEYEVENTF_KEYUP,0,0))))
        arr=(INPUT*2)(*strokes)
        user32.SendInput.argtypes=(ctypes.c_uint32,ctypes.POINTER(INPUT),ctypes.c_int)
        user32.SendInput.restype=ctypes.c_uint32
        written=user32.SendInput(2,arr,ctypes.sizeof(INPUT))
        if written!=2:
            raise RuntimeError(f"partial SendInput: {written}/2; no retry")