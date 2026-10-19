"""Actual Win32 state + calibrated visual evidence at last possible moment."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
import ctypes
import sys
import time
from .screen import ScreenObserver
from .runtime import FreshSnapshot
from .contracts import ScreenState, Playback, ControllerState
from .controller import Controller

class WindowsManualMonitor:
    def __init__(self):
        if sys.platform!='win32':raise OSError('Windows required')
        self._baseline=self._last_input()

    @staticmethod
    def _last_input() -> int:
        from ctypes import wintypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_=[('cbSize',wintypes.UINT),('dwTime',wintypes.DWORD)]
        data=LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO),0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(data)):
            raise OSError('GetLastInputInfo failed')
        return data.dwTime

    def reset_after_transition(self):
        self._baseline=self._last_input()

    def intervened(self) -> bool:
        return self._baseline!=self._last_input()


class LiveGuard:
    def __init__(self,controller:Controller, screen:ScreenObserver, hwnd:int, pid:int,
                 manual:WindowsManualMonitor, tts_active:Callable[[],bool],
                 capture_alive:Callable[[],bool],
                 audio_activity:Callable[[],bool] = lambda: True,
                 clock:Callable[[],int]=lambda:time.monotonic_ns()//1_000_000):
        self.ctrl,self.screen,self.hwnd,self.pid=controller,screen,hwnd,pid
        self.manual,self.tts_active,self.capture_alive,self.clock=manual,tts_active,capture_alive,clock
        self.audio_activity=audio_activity

    def observe(self)->FreshSnapshot:
        # Observation failure returns an entirely blocking snapshot, never
        # values left over from the previous game frame.
        try:
            if sys.platform!='win32':raise OSError('Windows required')
            from ctypes import wintypes
            user32=ctypes.windll.user32
            user32.GetForegroundWindow.restype=wintypes.HWND
            foreground=user32.GetForegroundWindow()==self.hwnd
            pid=wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(self.hwnd),ctypes.byref(pid))
            valid=pid.value==self.pid and bool(user32.IsWindowVisible(self.hwnd)) and bool(user32.IsWindowEnabled(self.hwnd))
            line=self.ctrl.line
            if line is None:raise RuntimeError('no current line')
            s=self.screen.observe(line.text.raw,self.clock())
            return FreshSnapshot(
                screen=s.state,focused=foreground and valid,
                text_ready=s.ready,capture_ok=self.capture_alive(),
                native_voice_active=(line.playback is Playback.ACTIVE or self.audio_activity()),
                tts_playing=self.tts_active(),line_matches=s.text_matches,
                target_window_matches=valid,input_available=valid,
                manual_override=self.manual.intervened(),
                transition_pending=self.ctrl.state is ControllerState.WAIT_TRANSITION_ACK,
            )
        except Exception:
            return FreshSnapshot(ScreenState.UNKNOWN,False,False,False)
