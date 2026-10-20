"""Single live Application path shared by GUI and diagnostic entrypoint."""
from __future__ import annotations
from dataclasses import dataclass,field,replace
from pathlib import Path
from threading import Lock,Event,Thread
from queue import Queue,Empty
from typing import Callable
import hashlib
import json
import math
import sys
import time

from .application import ObservationOrchestrator
from .audio_buffer import AudioHistory
from .audio_recognition import JapaneseASR,AudioRecognitionPipeline,TargetLine
from .controller import Controller,Settings
from .contracts import Envelope,EventKind,TTSPolicy,ControllerState,EffectKind
from .irodori import TTSWorker,IrodoriClient,IrodoriConfig
from .live_guard import LiveGuard,WindowsManualMonitor
from .process_audio import CaptureHealth,ProcessCapture
from .observation import OriginObservation
from .hook_host import HookHost
from .hook_application import HookApplication
from .runtime import Runtime
from .screen import WindowsClientCapture
from .acquisition import SourceKind,AcquisitionSession,TextCandidate
from .ocr import TesseractOCR,OCRWorker
from .windows_input import WindowsAdvancementInput
from .text import TextInterpreter,GameTextConfig
from .preferences import Preferences


def list_target_windows() -> list[tuple[int,int,str]]:
    """Enumerate visible selectable Windows; user explicitly binds one to Luna.

    This is only a picker, not a claim that Luna's hook belongs to this PID.
    """
    if sys.platform!='win32':raise OSError('Target window selection requires Windows')
    import ctypes
    import os
    from ctypes import wintypes
    user32=ctypes.windll.user32
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user32.EnumWindows.argtypes=(callback_type,wintypes.LPARAM)
    user32.EnumWindows.restype=wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes=(wintypes.HWND,ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowTextLengthW.argtypes=(wintypes.HWND,)
    user32.GetWindowTextLengthW.restype=ctypes.c_int
    user32.GetWindowTextW.argtypes=(wintypes.HWND,wintypes.LPWSTR,ctypes.c_int)
    user32.IsWindowVisible.argtypes=(wintypes.HWND,)
    windows=[]
    @callback_type
    def collect(hwnd,_):
        if not user32.IsWindowVisible(hwnd):return True
        length=user32.GetWindowTextLengthW(hwnd)
        if length<=0:return True
        title=ctypes.create_unicode_buffer(length+1)
        if not user32.GetWindowTextW(hwnd,title,length+1):return True
        pid=wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        if pid.value and pid.value!=os.getpid():
            windows.append((int(pid.value),int(hwnd),title.value))
        return True
    if not user32.EnumWindows(collect,0):raise OSError('EnumWindows failed')
    return windows


def _clock() -> int:
    return time.monotonic_ns()//1_000_000


@dataclass(frozen=True)
class LiveConfig:
    pid:int
    hwnd:int
    loopback_helper:Path | None
    asr_model:Path | None
    asr_tokens:Path | None
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
        required={'pid','hwnd','loopback_helper','asr_model','asr_tokens','grace_ms',
                  'allow_heuristic_absence','log_path','tts_speed','silent_dwell','tts_required','advance_input'}
        if set(data)!=required:raise ValueError('live hardware configuration does not match the expected schema')
        d=data['silent_dwell'];advance=data['advance_input']
        if (not isinstance(d,dict) or set(d)!={'base_ms','min_ms','reading_chars_per_sec'}
            or not isinstance(advance,dict) or set(advance)!={'mode','virtual_key'}
            or any(type(data[name]) is not int for name in ('pid','hwnd','grace_ms'))
            or any(type(d[name]) is not int for name in ('base_ms','min_ms'))
            or type(advance['virtual_key']) is not int
            or type(data['allow_heuristic_absence']) is not bool
            or type(data['tts_required']) is not bool
            or any(not isinstance(data[name],str)
                   for name in ('loopback_helper','asr_model','asr_tokens'))
            or not isinstance(data['log_path'],str) or not data['log_path'].strip()):
            raise ValueError('invalid monitor field types / required paths')
        audio_configured=[bool(data[name].strip()) for name in ('loopback_helper','asr_model','asr_tokens')]
        if any(audio_configured) and not all(audio_configured):
            raise ValueError('native audio monitoring requires loopback helper, ASR model and tokens together')
        absolute=lambda x:(base/data[x]).resolve() if data[x].strip() else None
        value=cls(int(data['pid']),int(data['hwnd']),
                  absolute('loopback_helper'),absolute('asr_model'),absolute('asr_tokens'),
                  int(data['grace_ms']),data['allow_heuristic_absence'],absolute('log_path'),
                  float(data['tts_speed']),int(d['base_ms']),int(d['min_ms']),
                  float(d['reading_chars_per_sec']),bool(data['tts_required']),
                  str(advance['mode']),int(advance['virtual_key']),hashlib.sha256(path.read_bytes()).hexdigest())
        if (value.pid<=0 or value.hwnd<=0 or value.grace_ms<0 or
            type(value.allow_heuristic_absence) is not bool or
            value.input_mode not in ('keyboard','mouse_left') or not 1<=value.input_virtual_key<=254
            or not math.isfinite(value.tts_speed) or not .25<=value.tts_speed<=4.0
            or not math.isfinite(value.reading_chars_per_sec) or value.reading_chars_per_sec<=0
            or value.silent_base_ms<0 or value.silent_min_ms<0):
            raise ValueError('invalid game window or input settings')
        return value

    @property
    def native_audio_enabled(self) -> bool:
        return self.loopback_helper is not None


def utterance_view(line) -> dict | None:
    """Publish the exact active Utterance without interpreting Luna text again."""
    if line is None:return None
    u=line.text
    return {'observed_original':u.observed_original,'speaker':u.speaker,
            'body':u.body,'structure_rule_id':u.structure_rule_id,
            'content_kind':u.content_kind.name,'tts_text':u.tts_text,
            'asr_target':u.asr_target,'effective_chars':u.effective_chars,
            'tts_route':u.tts_route.name,'audio_route':u.audio_route.name,'voice_id':u.voice_id,
            'config_revision':u.config_revision,'conflicts':list(u.conflicts)}


@dataclass
class LiveControl:
    stop:Event=field(default_factory=Event)
    authorize:Event=field(default_factory=Event)
    pause:Event=field(default_factory=Event)
    # Positive binding comes from exact managed native HOOK source, not a WS guess.
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
    manual=WindowsManualMonitor(config.hwnd)
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
        'asr_model_id':config.asr_model.name if config.asr_model else None,
        'asr_tokens_id':config.asr_tokens.name if config.asr_tokens else None,
        'source_policy': 'hook-preferred'})
    tts=None;asr=None;runtime=None;capture=None
    packets=Queue()
    source_connected=Event()
    last_state=None
    active=preferences.catalog.active()
    if active is None:raise ValueError('registered game and profile required for native acquisition')
    game,profile=active
    acquisition=profile.source()
    if acquisition.kind is SourceKind.HOOK and not profile.keys():
        raise ValueError('select and save a HOOK before HOOK monitoring')
    if acquisition.kind is SourceKind.HOOK and not preferences.luna_host_dll:
        raise ValueError('select LunaHost DLL first')
    if acquisition.kind is SourceKind.HOOK and len(profile.keys()) > 1:
        # A collection of independent HOOK outputs does not prove a merged
        # utterance until verified pairing/line boundaries are implemented.
        raise ValueError('multiple HOOK combination is not verified for automation')
    session=AcquisitionSession(game.game_id,profile.profile_id,acquisition)
    def received(observation:OriginObservation):
        if acquisition.kind is SourceKind.HOOK:
            candidate=TextCandidate(game.game_id,profile.profile_id,SourceKind.HOOK,
                session.generation,observation.sequence,_clock(),observation.text,observation.origin or 'managed-lunahost')
            packets.put(('source',candidate))
    def received_ocr(candidate:TextCandidate):
        packets.put(('source',candidate))
    app = None
    def invalidated(reason:str):
        # Discard any queued event from the previous Host/HOOK/connection epoch.
        # A still valid game/profile ID alone does not make a delayed packet current.
        session.change(game.game_id,profile.profile_id,acquisition)
        source_connected.clear()
        control.bound.clear()
        status({'hook':'not_verified','hook_reason':reason})
        # During initial binding there is no authorized line to revoke.
        # Once a line or authorization exists, cancel it synchronously, ahead
        # of any later events already present in the Host input queue.
        if app is not None and (ctrl.autoplay_authorized or ctrl.line is not None):
            app.disconnected(_clock())
    hook_app=None;host=None;ocr_worker=None
    if acquisition.kind is SourceKind.HOOK:
        hook_app=HookApplication(game.game_id,profile.profile_id,received,lambda _:None)
        hook_app.update_roles(hook_pids={config.pid},audio_pids={config.pid},
                              input_pid=config.pid,input_hwnd=config.hwnd)
        hook_app.select(profile.keys())
        hook_app.on_invalidate=invalidated
        host=HookHost(Path(preferences.luna_host_dll),
            lambda event:packets.put(('hook',event)),
            lambda reason:packets.put(('host_closed',reason)))
    else:
        ocr_worker=OCRWorker(reader=TesseractOCR(acquisition,WindowsClientCapture(config.hwnd,config.pid)),
            game_id=game.game_id,profile_id=profile.profile_id,generation=session.generation,
            on_candidate=received_ocr,on_failure=lambda reason:packets.put(('source_error',reason)),
            on_blank=lambda:packets.put(('source_blank',session.generation)))
    try:
        guard=LiveGuard(ctrl,config.hwnd,config.pid,manual,
            lambda:tts.is_active() if tts else True,
            health.alive,lambda:asr.potentially_active() if asr else False,
            binding_confirmed=lambda:control.bound.is_set() and source_connected.is_set())
        runtime=Runtime(ctrl,guard if enable_input else GuardNoInput(guard),
                        WindowsAdvancementInput(config.hwnd,mode=config.input_mode,
                              virtual_key=config.input_virtual_key) if enable_input else InputDisabled(),
                        _TTSProxy(),audit=audit)
        client=IrodoriClient(IrodoriConfig(url=preferences.irodori_url.rstrip('/')+'/v1/audio/speech',
                                          speed=config.tts_speed))
        tts=TTSWorker(client,runtime.post)
        runtime.tts=tts
        if config.native_audio_enabled:
            assert config.asr_model is not None and config.asr_tokens is not None and config.loopback_helper is not None
            asr=AudioRecognitionPipeline(history,JapaneseASR(config.asr_model,config.asr_tokens),runtime.post,audit=audit)
        app=ObservationOrchestrator(runtime,
            on_line_started=(lambda epoch,token,text,now:asr.begin(TargetLine(ctrl.session_id,epoch,token,text,now))) if asr else None,
            on_line_updated=(lambda epoch,token,text:asr.retarget(epoch,token,text)) if asr else None,
            on_line_boundary=asr.mark_boundary if asr else None,
            heuristic_absence_ready=asr.can_heuristically_exclude_voice if asr else None,
            binding_ok=lambda:control.bound.is_set() and source_connected.is_set(),
            stable_ms=acquisition.stable_ms)
        if asr is not None:
            capture=ProcessCapture(config.pid,config.loopback_helper,history,asr.feed,health.report)
            capture.start()
        if host is not None:
            host.start()
            host.command('connect',pid=config.pid)
        else:
            source_connected.set();control.bound.set()
            ocr_worker.start()
        status({'monitor':'running','acquisition':acquisition.kind.value,'advance':'disabled',
                'reason':'Awaiting selected source; native audio monitoring '+('enabled' if asr else 'disabled')})
        while not control.stop.is_set():
            while True:
                try:kind,payload=packets.get_nowait()
                except Empty:break
                if kind=='hook' and hook_app is not None:
                    hook_app.handle(payload)
                    state=hook_app.status()
                    if state.state=='observing':
                        source_connected.set()
                        control.bound.set()
                    status({'hook':state.state,'hook_reason':state.reason,
                            'hook_candidates':state.candidate_count})
                elif kind=='host_closed' and hook_app is not None:
                    hook_app.host_closed(str(payload))
                    audit('native_hook_invalidated',{'reason':str(payload)})
                    status({'hook':'stopped','hook_reason':str(payload)})
                    if ctrl.state is ControllerState.PAUSED:return 3
                elif kind=='source':
                    if session.accept(payload):
                        status({'original':payload.text,'acquisition':payload.kind.value})
                        app.candidate(OriginObservation(payload.sequence,payload.text,payload.text,
                            kind=payload.kind.value,source_generation=payload.source_generation,
                            game_id=payload.game_id,profile_id=payload.profile_id,origin=payload.origin),
                            _clock(),manual_intervened=ctrl.autoplay_authorized and manual.intervened())
                elif kind=='source_blank':
                    if payload==session.generation:app.unstable(_clock())
                elif kind=='source_error':
                    invalidated(str(payload))
                    status({'acquisition_error':str(payload)})
            if control.pause.is_set():
                ctrl.pause('explicit_pause')
                status({'advance':'disabled','reason':'明示一時停止（取得・音声処理は継続）'})
                control.pause.clear()
            if control.authorize.is_set():
                control.authorize.clear()
                if enable_input and source_connected.is_set() and control.bound.is_set():
                    try:
                        ctrl.authorize()
                    except RuntimeError as exc:
                        status({'advance':'disabled','reason':str(exc)})
                    else:
                        manual.reset_after_authorization()
                        status({'advance':'authorized',
                                'reason':'許可済み（現行台詞の提示完了と入力直前確認を待機）'})
                else:
                    status({'advance':'disabled','reason':'選択した取得元・入力対象が未確認'})
            while runtime.step():pass
            if ctrl.state is ControllerState.WAIT_LINE and ctrl.line is None:
                config_update=control.consume_safe_config()
                if config_update is not None:
                    ctrl.interpreter=TextInterpreter(config_update)
                    audit('game_config_updated',{'revision':config_update.revision})
            now=_clock()
            user32.GetForegroundWindow.restype=wintypes.HWND
            focused=user32.GetForegroundWindow()==config.hwnd
            if ctrl.autoplay_authorized and manual.intervened():
                app.disconnected(now)
            app.tick(now,focused=focused,capture_ok=health.alive())
            while runtime.step():pass
            line=ctrl.line
            view=(ctrl.state.name,ctrl.pause_reason,ctrl.autoplay_authorized,
                  line.epoch if line else None,line.presentation.name if line else 'NONE',
                  line.text.speaker if line else None,line.text.body if line else None,
                  line.sent if line else False, focused,
                  line.text if line else None)
            if view!=last_state:
                status({'controller':ctrl.state.name,'reason':(ctrl.pause_reason or
                                  ('選択中の音声方針は原音判定を要求します。音声監視を設定してください'
                                   if line and line.text.audio_route.name in ('AUTO','NATIVE') and not config.native_audio_enabled else '')),
                        'epoch':line.epoch if line else None,
                        'presentation':line.presentation.name if line else 'NONE',
                        'speaker':line.text.speaker if line else None,
                        'body':line.text.body if line else None,
                        'sent':line.sent if line else False,'focused':focused,
                        'capture_ok':health.alive(),'native_audio_enabled':config.native_audio_enabled,'advance':'authorized' if ctrl.autoplay_authorized else 'disabled','awaiting_next_hook':ctrl.state is ControllerState.WAIT_NEXT_HOOK,
                        'utterance':utterance_view(line)})
                last_state=view
            if ctrl.state is ControllerState.PAUSED:
                audit('paused',{'reason':ctrl.pause_reason})
                return 3
            time.sleep(.08)
        return 0
    finally:
        control.stop.set()
        if host is not None:host.close()
        if ocr_worker is not None:ocr_worker.close()
        if capture:capture.close()
        if asr:asr.close()
        if tts:tts.cancel_all()
        if runtime:runtime.finalize()
        manual.close()
        audit.close()


class _TTSProxy:
    def apply(self,e):raise RuntimeError('TTS not wired yet')
