"""Capture the selected Windows game's client area for optional OCR only."""
from __future__ import annotations

class WindowsClientCapture:
    def __init__(self,hwnd:int,expected_pid:int):
        if hwnd<=0 or expected_pid<=0:raise ValueError('HWND and PID are required')
        self.hwnd,self.pid=hwnd,expected_pid

    def __call__(self):
        import sys
        if sys.platform!='win32':raise OSError('Windows screenshot capture required')
        import ctypes
        from ctypes import wintypes
        import numpy as np
        import mss
        user32=ctypes.windll.user32
        current_pid=wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(self.hwnd),ctypes.byref(current_pid))
        if current_pid.value!=self.pid or not user32.IsWindowVisible(self.hwnd):
            raise RuntimeError('target HWND/PID/visibility changed')
        rect=wintypes.RECT()
        if not user32.GetClientRect(self.hwnd,ctypes.byref(rect)):raise OSError('GetClientRect failed')
        p=wintypes.POINT(0,0)
        if not user32.ClientToScreen(self.hwnd,ctypes.byref(p)):raise OSError('ClientToScreen failed')
        if rect.right<=0 or rect.bottom<=0:raise RuntimeError('no visible client area')
        with mss.mss() as s:
            bgra=np.asarray(s.grab(dict(left=p.x,top=p.y,width=rect.right,height=rect.bottom)))
        return bgra[:,:,:3].copy()
