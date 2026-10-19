"""Windows service wiring for Luna -> Screen -> ProcessLoopback -> ASR -> Controller.

No automatic input without --enable-input plus a calibrated game profile.
Without independently proven native absence, the Controller pauses; there is
no timeout-based bypass of voice assignment.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from threading import Lock
import hashlib
import json
import socket
import sys
import time

from .application import ObservationOrchestrator
from .audio_buffer import AudioHistory
from .audio_recognition import JapaneseASR, AudioRecognitionPipeline, TargetLine
from .controller import Controller, Settings
from .contracts import Envelope, EventKind, TTSPolicy, ControllerState, ScreenState
from .irodori import TTSWorker, IrodoriClient, IrodoriConfig
from .live_guard import LiveGuard, WindowsManualMonitor
from .process_audio import CaptureHealth, ProcessCapture
from .luna_bridge import decode
from .runtime import Runtime
from .screen import ScreenObserver, ScreenProfile, WindowsClientCapture, ScreenSample
from .windows_input import WindowsEnterInput
from .speaker import SpeakerRouter, VoiceRoute


def _clock() -> int:
    return time.monotonic_ns()//1_000_000


@dataclass(frozen=True)
class LiveConfig:
    pid: int
    hwnd: int
    screen_profile: Path
    loopback_helper: Path
    asr_model: Path
    asr_tokens: Path
    tts_url: str
    tts_policy: TTSPolicy
    selected_voice: str | None
    default_voice: str | None
    bridge_port: int
    grace_ms: int
    allow_heuristic_absence: bool
    log_path: Path
    speaker_rules: dict[str, VoiceRoute]
    name_readings: dict[str, str]
    tts_speed: float
    silent_base_ms: int
    silent_min_ms: int
    reading_chars_per_sec: float
    tts_required: bool

    @classmethod
    def load(cls,path:Path) -> 'LiveConfig':
        d=json.loads(path.read_text(encoding='utf-8'))
        base=path.resolve().parent
        abs_path=lambda k:(base / d[k]).resolve()
        if set(d) != {'pid','hwnd','screen_profile','loopback_helper','asr_model',
                       'asr_tokens','tts','bridge_port','grace_ms',
                       'allow_heuristic_absence','log_path','speaker_rules','name_readings',
                       'silent_dwell'}:
            raise ValueError('live configuration fields differ from the defined schema')
        p=cls(int(d['pid']),int(d['hwnd']),abs_path('screen_profile'),
              abs_path('loopback_helper'),abs_path('asr_model'),abs_path('asr_tokens'),
              d['tts']['url'],TTSPolicy[d['tts']['policy']],
              d['tts'].get('selected_voice'),d['tts'].get('default_voice'),
              int(d['bridge_port']),int(d['grace_ms']),
              d['allow_heuristic_absence'],abs_path('log_path'),
              {speaker:VoiceRoute(TTSPolicy[value['policy']],value.get('voice_id'))
               for speaker,value in d['speaker_rules'].items()},d['name_readings'],
              float(d['tts'].get('speed',1)),
              int(d['silent_dwell']['base_ms']),int(d['silent_dwell']['min_ms']),
              float(d['silent_dwell']['reading_chars_per_sec']),
              bool(d['tts'].get('required_for_accessibility',False)))
        if p.pid<=0 or p.hwnd<=0 or not 1<=p.bridge_port<=65535 or p.grace_ms<0:
            raise ValueError('invalid PID, HWND, port or grace')
        if type(p.allow_heuristic_absence) is not bool:
            raise ValueError('allow_heuristic_absence must be boolean')
        return p


class CaptureLiveness:
    def __init__(self,timeout_ms:int=700):
        self.last_ok=-1
        self.timeout_ms=timeout_ms
        self.lock=Lock()
        self.reason='not_started'

    def report(self,x:CaptureHealth):
        with self.lock:
            if x.healthy:self.last_ok=x.at_ms
            else:self.last_ok=-1
            self.reason=x.reason

    def alive(self)->bool:
        with self.lock:return self.last_ok>=0 and _clock()-self.last_ok<self.timeout_ms


class JsonAudit:
    def __init__(self,path:Path):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.output=path.open('a',encoding='utf-8')

    def __call__(self,kind:str,details:dict):
        item={'at_ms':_clock(),'name':kind,**details}
        self.output.write(json.dumps(item,ensure_ascii=False)+'\n')
        self.output.flush()

    def close(self):self.output.close()


class InputDisabled:
    def advance(self):raise RuntimeError('hardware input disabled')


class GuardNoInput:
    def __init__(self,actual:LiveGuard):self.actual=actual
    def observe(self):return replace(self.actual.observe(),input_available=False)


def run_windows(config:LiveConfig, *, enable_input:bool=False)->int:
    if sys.platform!='win32':
        raise OSError('live game integration requires Windows')
    import ctypes
    from ctypes import wintypes
    import psutil
    if any(p.pid == config.pid for p in psutil.Process().parents()):
        raise ValueError('AutoAdv/TTS must not be a child of the game process')
    user32=ctypes.windll.user32
    pid=wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(config.hwnd),ctypes.byref(pid))
    if pid.value!=config.pid:raise ValueError('configured target HWND does not match configured PID')
    if enable_input and not config.screen_profile.is_file():
        raise ValueError('no calibrated screen profile')
    screen=ScreenObserver(ScreenProfile.load(config.screen_profile),
                          WindowsClientCapture(config.hwnd,config.pid))
    manual=WindowsManualMonitor()
    health=CaptureLiveness()
    history=AudioHistory()
    ctrl=Controller(Settings(grace_ms=config.grace_ms,
                             allow_heuristic_absence=config.allow_heuristic_absence,
                             name_readings=tuple(config.name_readings.items()),
                             silent_base_ms=config.silent_base_ms,
                             silent_min_ms=config.silent_min_ms,
                             reading_chars_per_sec=config.reading_chars_per_sec,
                             tts_required=config.tts_required))
    ctrl.configure_tts(config.tts_policy, selected_voice=config.selected_voice,
                       default_voice=config.default_voice)
    audit=JsonAudit(config.log_path)
    tts=None
    asr=None
    capture=None
    sock=None
    try:
        guard=LiveGuard(ctrl,screen,config.hwnd,config.pid,manual,
                        lambda:tts.is_active() if tts else True,health.alive,
                        lambda:asr.potentially_active() if asr else True)
        runtime=Runtime(ctrl,guard if enable_input else GuardNoInput(guard),
                        WindowsEnterInput(config.hwnd) if enable_input else InputDisabled(),
                        # temporarily assigned below, before processing any events
                        _TTSProxy(),audit=audit)
        tts=TTSWorker(IrodoriClient(IrodoriConfig(url=config.tts_url,speed=config.tts_speed)),runtime.post)
        runtime.tts=tts
        recognizer=JapaneseASR(config.asr_model,config.asr_tokens)
        asr=AudioRecognitionPipeline(history,recognizer,runtime.post)
        app=ObservationOrchestrator(runtime,
            on_line_started=lambda epoch,token,text,now:asr.begin(
                TargetLine(ctrl.session_id,epoch,token,text,now)),
            speaker_router=SpeakerRouter(config.speaker_rules,
                VoiceRoute(config.tts_policy,config.selected_voice)),
            heuristic_absence_ready=asr.can_heuristically_exclude_voice)
        capture=ProcessCapture(config.pid,config.loopback_helper,history,
                               asr.feed,health.report)
        # Connect capture before receiving any Luna event. The helper never
        # shares a PID tree with the game or Irodori's playback worker.
        capture.start()
        sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        sock.bind(('127.0.0.1',config.bridge_port)); sock.setblocking(False)
        was_waiting_ack=False
        print('AutoAdv live session started; input '+('ENABLED' if enable_input else 'DISABLED'),flush=True)
        while True:
            while True:
                try:payload,_=sock.recvfrom(65507)
                except BlockingIOError:break
                try:
                    packet=decode(payload)
                except (ValueError,UnicodeDecodeError,json.JSONDecodeError):continue
                app.candidate(packet,_clock())
            runtime.step()
            l=ctrl.line
            expected=(app.pending.raw_text
                      if app.pending is not None and ctrl.state is ControllerState.WAIT_TRANSITION_ACK
                      else l.text.raw if l is not None else
                      app.pending.raw_text if app.pending is not None else '')
            now=_clock()
            try:
                sample=screen.observe(expected,now)
            except Exception as exc:
                audit('screen_failure',{'detail':str(exc)})
                sample=ScreenSample(now,ScreenState.UNKNOWN,
                                    False,False,'',False)
            user32.GetForegroundWindow.restype=wintypes.HWND
            foreground=user32.GetForegroundWindow()==config.hwnd
            app.observe(sample,focused=foreground,
                        capture_ok=health.alive(),manual_intervened=manual.intervened())
            while runtime.step():pass
            if was_waiting_ack and ctrl.state is ControllerState.WAIT_LINE:
                manual.reset_after_transition()
            was_waiting_ack=ctrl.state is ControllerState.WAIT_TRANSITION_ACK
            if ctrl.state is ControllerState.PAUSED:
                audit('paused',{'reason':ctrl.pause_reason,'epoch':l.epoch if l else None})
                print(f'AutoAdv paused: {ctrl.pause_reason}',flush=True)
                return 3
            time.sleep(.08)
    except KeyboardInterrupt:
        return 0
    finally:
        if capture:capture.close()
        if asr:asr.close()
        if tts:tts.cancel_all()
        if sock:sock.close()
        audit.close()


class _TTSProxy:
    def apply(self,e):raise RuntimeError('TTS not wired yet')
