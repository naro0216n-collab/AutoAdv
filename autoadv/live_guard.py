"""Fresh Win32 preflight and physical/injected-input ownership tracking.

A GetLastInputInfo timestamp alone cannot tell our SendInput strokes from a
physical keypress. The low-level keyboard/mouse hooks detect both categories,
recognizing *only* our own injected marker as automatic. Missing hooks fail
closed. The hooks never issue any input or control the Controller directly.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from threading import Event, Thread
import ctypes
import sys
import time
from .screen import ScreenObserver
from .runtime import FreshSnapshot
from .contracts import ScreenState, Playback, ControllerState
from .controller import Controller
from .windows_input import AUTOADV_INPUT_MARK


def is_manual_hook_event(*, keyboard: bool, flags: int, extra_info: int) -> bool:
    injected_bit = 0x10 if keyboard else 0x01
    return not (flags & injected_bit and extra_info == AUTOADV_INPUT_MARK)


class WindowsManualMonitor:
    """Latches genuine/manual or foreign-injected input until the session ends.

    The event hook may receive injected callbacks after SendInput returns.
    Because the input is explicitly tagged, there is no post-advance grace
    interval during which a genuine user action could be accidentally ignored.
    """
    def __init__(self):
        if sys.platform != 'win32':
            raise OSError('Windows required')
        self._manual = Event()
        self._ready = Event()
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
        PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p)==8 else ctypes.c_uint32
        CALLBACK = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)

        class KBD(ctypes.Structure):
            _fields_ = [('vkCode',wintypes.DWORD),('scanCode',wintypes.DWORD),
                        ('flags',wintypes.DWORD),('time',wintypes.DWORD),('dwExtraInfo',PTR)]
        class MOUSE(ctypes.Structure):
            _fields_ = [('pt',wintypes.POINT),('mouseData',wintypes.DWORD),
                        ('flags',wintypes.DWORD),('time',wintypes.DWORD),('dwExtraInfo',PTR)]
        class MSG(ctypes.Structure):
            _fields_ = [('hwnd',wintypes.HWND),('message',wintypes.UINT),
                        ('wParam',ctypes.c_size_t),('lParam',ctypes.c_ssize_t),
                        ('time',wintypes.DWORD),('pt',wintypes.POINT),('lPrivate',wintypes.DWORD)]
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, CALLBACK, ctypes.c_void_p, wintypes.DWORD)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p,ctypes.c_int,ctypes.c_size_t,ctypes.c_ssize_t)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.GetMessageW.argtypes = (ctypes.POINTER(MSG),ctypes.c_void_p,wintypes.UINT,wintypes.UINT)
        user32.GetMessageW.restype = ctypes.c_int
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        hooks = []
        def keyboard(code, wparam, lparam):
            if code >= 0:
                info = ctypes.cast(lparam,ctypes.POINTER(KBD)).contents
                if is_manual_hook_event(keyboard=True,flags=info.flags,extra_info=info.dwExtraInfo):
                    self._manual.set()
            return user32.CallNextHookEx(None,code,wparam,lparam)
        def mouse(code, wparam, lparam):
            if code >= 0:
                info = ctypes.cast(lparam,ctypes.POINTER(MOUSE)).contents
                if is_manual_hook_event(keyboard=False,flags=info.flags,extra_info=info.dwExtraInfo):
                    self._manual.set()
            return user32.CallNextHookEx(None,code,wparam,lparam)
        # Keep callback references alive for the duration of the hook.
        keyboard_cb, mouse_cb = CALLBACK(keyboard), CALLBACK(mouse)
        try:
            self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
            for hook_id, cb in ((13,keyboard_cb),(14,mouse_cb)):
                hook = user32.SetWindowsHookExW(hook_id,cb,None,0)
                if not hook:
                    raise OSError(f'cannot install input hook {hook_id}')
                hooks.append(hook)
            self._ready.set()
            msg = MSG()
            while not self._stop.is_set():
                status = user32.GetMessageW(ctypes.byref(msg),None,0,0)
                if status <= 0:
                    if status == -1:raise OSError('low-level input hook message loop failed')
                    break
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
        return self._manual.is_set() or not self._thread.is_alive() or self._error is not None

    def reset_after_transition(self):
        # Transition to the next line is never permission to erase user input.
        pass

    def close(self):
        self._stop.set()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id,0x0012,0,0)
        if self._thread.is_alive():
            self._thread.join(timeout=2)


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
