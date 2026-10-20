"""Single live Application path shared by GUI and diagnostic entrypoint."""
from __future__ import annotations
from dataclasses import dataclass,field,replace
from pathlib import Path
from threading import Lock,Event,Thread
from queue import Queue,Empty
from typing import Callable
import hashlib
import json
import sys
import time

from .application import ObservationOrchestrator
from .audio_buffer import AudioHistory
from .audio_recognition import JapaneseASR,AudioRecognitionPipeline,TargetLine
from .controller import Controller,Settings
from .contracts import Envelope,EventKind,TTSPolicy,ControllerState,ScreenState,EffectKind
from .irodori import TTSWorker,IrodoriClient,IrodoriConfig
from .live_guard import LiveGuard,WindowsManualMonitor
from .process_audio import CaptureHealth,ProcessCapture
from .luna import OriginObservation,run_origin_listener
from .runtime import Runtime
from .screen import ScreenObserver,ScreenProfile,WindowsClientCapture,ScreenSample
from .windows_input import WindowsAdvancementInput
from .text import TextInterpreter,GameTextConfig
from .preferences import Preferences


def _clock() -> int:
    return time.monotonic_ns()//1_000_000


@dataclass(frozen=True)
class LiveConfig:
    pid:int
    hwnd:int
    screen_profile:Path
    loopback_helper:Path
    asr_model:Path
    asr_tokens:Path
    grace_ms:int
    allow_heuristic_absence:bool
    log_path:Path
    tts_speed:float
    silent_base_ms:int
    silent_min_ms:int
    reading_chars_per_sec:float
    tts_required:bool
    input_mode:str
    input_virtual_key:int
    config_sha256:str

    @classmethod
    def load(cls,path:Path) -> 'LiveConfig':
        data=json.loads(path.read_text(encoding='utf-8'))
        base=path.resolve().parent
        required={'pid','hwnd','screen_profile','loopback_helper','asr_model','asr_tokens','grace_ms',
                  'allow_heuristic_absence','log_path','tts_speed','silent_dwell','tts_required','advance_input'}
        if set(data)!=required:raise ValueError('live hardware configuration does not match the expected schema')
        absolute=lambda x:(base/str(data[x])).resolve()
        d=data['silent_dwell'];advance=data['advance_input']
        value=cls(int(data['pid']),int(data['hwnd']),absolute('screen_profile'),
                  absolute('loopback_helper'),absolute('asr_model'),absolute('asr_tokens'),
                  int(data['grace_ms']),data['allow_heuristic_absence'],absolute('log_path'),
                  float(data['tts_speed']),int(d['base_ms']),int(d['min_ms']),
                  float(d['reading_chars_per_sec']),bool(data['tts_required']),
                  str(advance['mode']),int(advance['virtual_key']),hashlib.sha256(path.read_bytes()).hexdigest())
        if (value.pid<=0 or value.hwnd<=0 or value.grace_ms<0 or
            type(value.allow_heuristic_absence) is not bool or
            value.input_mode not in ('keyboard','mouse_left') or not 1<=value.input_virtual_key<=254):
            raise ValueError('invalid game window or input settings')
        return value


@dataclass
class LiveControl:
    stop:Event=field(default_factory=Event)
    authorize:Event=field(default_factory=Event)
    pause:Event=field(default_factory=Event)
    # App receives a positive user binding confirmation; never infer game identity from WS.
    bound:Event=field(default_factory=Event)
    config_lock:Lock=field(default_factory=Lock)
    next_config:GameTextConfig|None=None

    def queue_config(self,config:GameTextConfig)->None:
        with self.config_lock:self.next_config=config

    def consume_safe_config(self)->GameTextConfig|None:
        with self.config_lock:
            config=self.next_config
            self.next_config=None
            return config

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
    """Thread-safe JSONL with per-line model identity and ASR tail evidence."""
    def __init__(self,path:Path,*,context:dict | None=None):
        path.parent.mkdir(parents=True,exist_ok=True)
        self.output=path.open('a',encoding='utf-8')
        self.context=context or {}
        self.lock=Lock()
        self._asr_by_line:dict[tuple[str,int],dict]={}

    def __call__(self,kind:str,details:dict):
        with self.lock:
            if kind=='asr_progress':
                key=(details['session_id'],details['line_epoch'])
                self._asr_by_line[key]={
                    k:details[k] for k in ('asr_coverage','asr_tail_confidence',
                        'asr_order_consistent','vad_tail_silence_ms','inference_samples',
                        'native_attributed') if k in details}
            if kind=='line_summary':
                details={**self.context,**details,**self._asr_by_line.pop(
                    (details['session_id'],details['line_epoch']),{})}
            item={'at_ms':_clock(),'name':kind,**details}
            self.output.write(json.dumps(item,ensure_ascii=False)+'\n')
            self.output.flush()

    def close(self):
        with self.lock:self.output.close()


class InputDisabled:
    def advance(self):raise RuntimeError('hardware input disabled')


class GuardNoInput:
    def __init__(self,actual:LiveGuard):self.actual=actual
    def observe(self):return replace(self.actual.observe(),input_available=False)


def run_windows(config:LiveConfig, *, preferences:Preferences|None=None,
                game_config:GameTextConfig|None=None,
                control:LiveControl|None=None,
                status:Callable[[dict],None]|None=None,
                enable_input:bool=False) -> int:
    """Run in a worker thread, never in Qt's event loop.

    enable_input only enables guarded hardware capability; explicit per-session
    authorization and positive binding are required before a line is processed.
    """
    if sys.platform!='win32':raise OSError('live game integration requires Windows')
    import ctypes
    from ctypes import wintypes
    import psutil
    preferences=preferences or Preferences.load()
    game_config=game_config or preferences.game_config()
    control=control or LiveControl()
    status=status or (lambda _:None)
    user32=ctypes.windll.user32
    pid=wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(config.hwnd),ctypes.byref(pid))
    if pid.value!=config.pid:raise ValueError('HWND does not belong to target game PID')
    if any(parent.pid==config.pid for parent in psutil.Process().parents()):
        raise ValueError('AutoAdv must not be a child of target game')
    screen=ScreenObserver(ScreenProfile.load(config.screen_profile),
                          WindowsClientCapture(config.hwnd,config.pid))
    manual=WindowsManualMonitor()
    health=CaptureLiveness()
    history=AudioHistory()
    ctrl=Controller(Settings(grace_ms=config.grace_ms,
                             allow_heuristic_absence=config.allow_heuristic_absence,
                             silent_base_ms=config.silent_base_ms,
                             silent_min_ms=config.silent_min_ms,
                             reading_chars_per_sec=config.reading_chars_per_sec,
                             tts_required=config.tts_required),
                    interpreter=TextInterpreter(game_config))
    audit=JsonAudit(config.log_path,context={
        'config_version':config.config_sha256,
        'text_config_revision':game_config.revision,
        'profile_schema':'live-profile-v2',
        'asr_model_id':config.asr_model.name,
        'asr_tokens_id':config.asr_tokens.name,
        'screen_profile_id':config.screen_profile.name})
    tts=None;asr=None;runtime=None;capture=None
    packets=Queue()
    listener_stop=Event()
    listener_connected=Event()
    last_state=None
    def connected(ok:bool,message:str):
        if ok:listener_connected.set()
        else:listener_connected.clear()
        packets.put(('connection',(ok,message)))
    def received(observation:OriginObservation):
        packets.put(('text',observation))
    listener=Thread(target=run_origin_listener,
                    args=(preferences.luna_url,listener_stop,received,connected),
                    daemon=True,name='autoadv-luna-origin')
    try:
        guard=LiveGuard(ctrl,screen,config.hwnd,config.pid,manual,
            lambda:tts.is_active() if tts else True,
            health.alive,lambda:asr.potentially_active() if asr else True,
            binding_confirmed=lambda:control.bound.is_set() and listener_connected.is_set())
        runtime=Runtime(ctrl,guard if enable_input else GuardNoInput(guard),
                        WindowsAdvancementInput(config.hwnd,mode=config.input_mode,
                              virtual_key=config.input_virtual_key) if enable_input else InputDisabled(),
                        _TTSProxy(),audit=audit)
        client=IrodoriClient(IrodoriConfig(url=preferences.irodori_url.rstrip('/')+'/v1/audio/speech',
                                          speed=config.tts_speed))
        tts=TTSWorker(client,runtime.post)
        runtime.tts=tts
        asr=AudioRecognitionPipeline(history,JapaneseASR(config.asr_model,config.asr_tokens),runtime.post,audit=audit)
        app=ObservationOrchestrator(runtime,
            on_line_started=lambda epoch,token,text,now:asr.begin(TargetLine(ctrl.session_id,epoch,token,text,now)),
            on_line_updated=lambda epoch,token,text:asr.retarget(epoch,token,text),
            on_line_boundary=asr.mark_boundary,
            heuristic_absence_ready=asr.can_heuristically_exclude_voice,
            binding_ok=lambda:control.bound.is_set() and listener_connected.is_set())
        capture=ProcessCapture(config.pid,config.loopback_helper,history,asr.feed,health.report)
        capture.start()
        listener.start()
        status({'monitor':'running','advance':'disabled','reason':'Session requires explicit binding and authorization'})
        while not control.stop.is_set():
            while True:
                try:kind,payload=packets.get_nowait()
                except Empty:break
                if kind=='connection':
                    ok,message=payload
                    audit('luna_connection',{'connected':ok,'reason':message})
                    status({'luna':'connected' if ok else 'disconnected','luna_reason':message})
                    if not ok:
                        control.bound.clear()
                        app.disconnected(_clock())
                elif kind=='text':
                    status({'original':payload.text})
                    app.candidate(payload,_clock())
            if control.pause.is_set():
                control.bound.clear()
                for effect in ctrl.pause('explicit_pause'):
                    if effect.kind is EffectKind.CANCEL_TTS:tts.apply(effect)
                control.pause.clear()
            if control.authorize.is_set():
                control.authorize.clear()
                if (enable_input and listener_connected.is_set() and control.bound.is_set() and
                    ctrl.state is ControllerState.WAIT_LINE):
                    manual.reset_after_authorization()
                    ctrl.authorize()
                    status({'advance':'authorized','reason':'Waiting for verified normal line'})
                else:
                    status({'advance':'disabled','reason':'Target / Luna / controller not ready for authorization'})
            while runtime.step():pass
            if ctrl.state is ControllerState.WAIT_LINE and ctrl.line is None:
                config_update=control.consume_safe_config()
                if config_update is not None:
                    ctrl.interpreter=TextInterpreter(config_update)
                    audit('game_config_updated',{'revision':config_update.revision})
            now=_clock()
            try:sample=screen.observe(now)
            except Exception as exc:
                audit('screen_failure',{'error_type':type(exc).__name__})
                sample=ScreenSample(now,ScreenState.UNKNOWN,False,False,'capture_failure')
            user32.GetForegroundWindow.restype=wintypes.HWND
            focused=user32.GetForegroundWindow()==config.hwnd
            was_manual=ctrl.state is ControllerState.WAIT_MANUAL_CHOICE
            app.observe(sample,focused=focused,capture_ok=health.alive(),
                manual_intervened=manual.intervened() and not was_manual)
            if was_manual and ctrl.state is ControllerState.WAIT_LINE:
                manual.reset_after_authorization()  # only after positively recognized manual-choice exit
                audit('manual_choice_completed',{'at_ms':now})
            while runtime.step():pass
            line=ctrl.line
            view=(ctrl.state.name,ctrl.pause_reason,
                  line.epoch if line else None,line.presentation.name if line else 'NONE',
                  line.text.speaker if line else None,line.text.body if line else None,
                  line.sent if line else False, focused)
            if view!=last_state:
                status({'controller':view[0],'reason':view[1] or '',
                        'epoch':view[2],'presentation':view[3],'speaker':view[4],
                        'body':view[5],'sent':view[6],'focused':focused,
                        'capture_ok':health.alive(),'awaiting_ack':ctrl.state is ControllerState.WAIT_TRANSITION_ACK})
                last_state=view
            if ctrl.state is ControllerState.PAUSED:
                audit('paused',{'reason':ctrl.pause_reason})
                return 3
            time.sleep(.08)
        return 0
    finally:
        control.stop.set();listener_stop.set()
        if capture:capture.close()
        if asr:asr.close()
        if tts:tts.cancel_all()
        if runtime:runtime.finalize()
        manual.close()
        if listener.is_alive():listener.join(timeout=3)
        audit.close()


class _TTSProxy:
    def apply(self,e):raise RuntimeError('TTS not wired yet')
