"""Fresh Win32 preflight and physical/injected-input ownership tracking.

A GetLastInputInfo timestamp alone cannot tell our SendInput strokes from a
physical keypress. The low-level keyboard/mouse hooks detect both categories,
recognizing *only* our own injected marker as automatic. Missing hooks fail
closed. The hooks never issue any input or control the Controller directly.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from threading import Event, Thread, Lock
import ctypes
import sys
import time
from .runtime import FreshSnapshot
from .contracts import ScreenState, Playback, ControllerState
from .controller import Controller
from .windows_input import AUTOADV_INPUT_MARK
from .progression import ClosedGate


def is_manual_hook_event(*, keyboard: bool, flags: int, extra_info: int) -> bool:
    injected_bit = 0x10 if keyboard else 0x01
    return not (flags & injected_bit and extra_info == AUTOADV_INPUT_MARK)


def is_choice_selection_event(*,keyboard:bool,message:int,flags:int,extra_info:int)->bool:
    """Selection action, not mouse motion or AutoAdv's own tagged SendInput."""
    if not is_manual_hook_event(keyboard=keyboard,flags=flags,extra_info=extra_info):return False
    return message in ((0x0100,0x0104) if keyboard else
                       (0x0201,0x0204,0x0207,0x020A,0x020B))


class InputOriginTracker:
    """Independent last-input timestamp cross-check; never ignores a time span."""
    def __init__(self, initial_tick: int):
        self._last_tick = initial_tick
        self._automatic_ticks: set[int] = set()
        self.manual = False

    def hook_event(self, *, keyboard:bool, flags:int, extra_info:int, tick:int) -> None:
        if is_manual_hook_event(keyboard=keyboard,flags=flags,extra_info=extra_info):
            self.manual = True
        else:
            self._automatic_ticks.add(tick)
            # The most recent two strokes are enough; never allow future
            # unrelated inputs to match a stale own-input timestamp.
            if len(self._automatic_ticks)>4:
                self._automatic_ticks={t for t in self._automatic_ticks if t>=tick-100}

    def os_input_tick(self, tick:int) -> bool:
        if tick != self._last_tick:
            if tick not in self._automatic_ticks:
                self.manual=True
            self._last_tick=tick
            self._automatic_ticks.clear()
        return self.manual


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
        self._choice_input = Event()
        self._origin_lock = Lock()
        self._origin = InputOriginTracker(self._last_input())
        self._ready = Event()
        self._stop = Event()
        self._thread_id = 0
        self._error: Exception | None = None
        self._thread = Thread(target=self._monitor, name='autoadv-input-monitor', daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or self._error is not None:
            self.close()
            raise RuntimeError('unable to install low-level input observation hooks') from self._error

    @staticmethod
    def _last_input() -> int:
        from ctypes import wintypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_=[('cbSize',wintypes.UINT),('dwTime',wintypes.DWORD)]
        state=LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO),0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(state)):
            raise OSError('GetLastInputInfo failed')
        return state.dwTime

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
        user32.TranslateMessage.argtypes = (ctypes.POINTER(MSG),)
        user32.DispatchMessageW.argtypes = (ctypes.POINTER(MSG),)
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        hooks = []
        def check_event(*, keyboard, code, message, lparam):
            if code >= 0:
                try:
                    struct=KBD if keyboard else MOUSE
                    info=ctypes.cast(lparam,ctypes.POINTER(struct)).contents
                    with self._origin_lock:
                        self._origin.hook_event(keyboard=keyboard,flags=info.flags,
                                                extra_info=info.dwExtraInfo,tick=info.time)
                        if self._origin.manual:self._manual.set()
                        if is_choice_selection_event(keyboard=keyboard,message=int(message),
                                                     flags=info.flags,extra_info=info.dwExtraInfo):
                            self._choice_input.set()
                except Exception:
                    # ctypes callbacks otherwise swallow Python exceptions.
                    self._manual.set()
        def keyboard(code, wparam, lparam):
            check_event(keyboard=True,code=code,message=wparam,lparam=lparam)
            return user32.CallNextHookEx(None,code,wparam,lparam)
        def mouse(code, wparam, lparam):
            check_event(keyboard=False,code=code,message=wparam,lparam=lparam)
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
        if self._manual.is_set() or not self._thread.is_alive() or self._error is not None:
            return True
        try:
            latest = self._last_input()
            with self._origin_lock:
                if self._origin.os_input_tick(latest):self._manual.set()
        except Exception:
            self._manual.set()
        return self._manual.is_set()

    def choice_action_seen(self)->bool:
        return self._choice_input.is_set() and self.intervened()

    def reset_choice_actions(self):
        self._choice_input.clear()

    def reset_after_authorization(self):
        # Only explicit new-session authorization or positively verified choice
        # completion can clear physical input observed during setup/selection.
        with self._origin_lock:
            self._origin = InputOriginTracker(self._last_input())
            self._manual.clear()
            self._choice_input.clear()

    def close(self):
        self._stop.set()
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id,0x0012,0,0)
        if self._thread.is_alive():
            self._thread.join(timeout=2)


class LiveGuard:
    def __init__(self, controller:Controller, hwnd:int,pid:int,manual,
                 tts_active:Callable[[],bool], capture_alive:Callable[[],bool],
                 audio_activity:Callable[[],bool]=lambda:True,
                 binding_confirmed:Callable[[],bool]=lambda:False,
                 gate=None, clock:Callable[[],int]=lambda:time.monotonic_ns()//1_000_000):
        self.ctrl,self.hwnd,self.pid=controller,hwnd,pid
        self.manual,self.tts_active,self.capture_alive=manual,tts_active,capture_alive
        self.audio_activity,self.binding_confirmed=audio_activity,binding_confirmed
        self.gate=gate or ClosedGate()
        self.clock=clock

    def observe(self)->FreshSnapshot:
        try:
            if sys.platform!='win32':raise OSError('Windows required')
            from ctypes import wintypes
            user32=ctypes.windll.user32
            user32.GetForegroundWindow.restype=wintypes.HWND
            foreground=user32.GetForegroundWindow()==self.hwnd
            pid=wintypes.DWORD()
            user32.GetWindowThreadProcessId(wintypes.HWND(self.hwnd),ctypes.byref(pid))
            valid=(pid.value==self.pid and bool(user32.IsWindowVisible(self.hwnd))
                   and bool(user32.IsWindowEnabled(self.hwnd)))
            line=self.ctrl.line
            if line is None:raise RuntimeError('no active line')
            proof=self.gate.observe(self.clock()) if valid else None
            allowed=bool(proof and proof.may_advance and self.binding_confirmed())
            return FreshSnapshot(focused=foreground and valid,capture_ok=self.capture_alive(),
                gate_allowed=allowed,gate_proof_id=proof.proof_id if allowed else None,
                native_voice_active=(line.playback is Playback.ACTIVE or self.audio_activity()),
                tts_playing=self.tts_active(),target_window_matches=valid,input_available=valid,
                manual_override=self.manual.intervened(),
                transition_pending=self.ctrl.state is ControllerState.WAIT_TRANSITION_ACK)
        except Exception:
            return FreshSnapshot()
