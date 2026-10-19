"""One vertical path from Luna events and observed game state to Controller.

The external line candidate queue and visual probes never issue input. Only
Runtime may call the Win32 driver after synchronous fresh preflight. A visual
ready marker must be observed falling AFTER input and rising AGAIN, alongside a
new post-input line candidate, before transition ACK. Identical lines without
that evidence are not invented.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
import time

from .contracts import Envelope, EventKind as EK, Evidence, VoicePresence, ScreenState, ControllerState
from .controller import Controller
from .luna_bridge import Candidate
from .screen import ScreenSample
from .runtime import Runtime
from .speaker import SpeakerRouter


@dataclass(frozen=True)
class MetadataVoice:
    # supplied only by game-side voice-asset assignment adapter
    asset_assigned: bool
    asset_id: str | None = None
    verified: bool = False


class ObservationOrchestrator:
    def __init__(self, runtime:Runtime,
                 on_line_started:Callable[[int,int,str,int],None] | None=None,
                 on_line_updated:Callable[[int,int,str],None] | None=None,
                 on_line_boundary:Callable[[int],None] | None=None,
                 speaker_router:SpeakerRouter | None=None,
                 heuristic_absence_ready:Callable[[],bool] | None=None):
        self.runtime=runtime
        self.ctrl=runtime.controller
        self.on_line_started=on_line_started or (lambda *_:None)
        self.on_line_updated=on_line_updated or (lambda *_:None)
        self.on_line_boundary=on_line_boundary or (lambda *_:None)
        self.speaker_router=speaker_router
        self.heuristic_absence_ready=heuristic_absence_ready or (lambda:False)
        self.pending:Candidate | None=None
        self.pending_at_ms: int | None=None
        self._seen_source_seq=0
        self._active_candidate_seq=0
        self._last_screen=ScreenState.UNKNOWN
        self._ready_was_present=False
        self._saw_fall_after_sent=False
        self._fall_at_ms: int | None = None
        self._saw_fresh_candidate_after_sent=False
        self._line_counter=0
        self._last_metadata:tuple | None=None

    def _post(self,kind:EK,now:int,**fields)->None:
        line=self.ctrl.line
        self.runtime.post(Envelope(kind,self.ctrl.session_id,now,
            line_epoch=line.epoch if line else None,
            cancel_token=line.token if line else None,**fields))
        self.runtime.step()

    def candidate(self,packet:Candidate,at_ms:int)->None:
        if packet.source_seq<=self._seen_source_seq:return
        self._seen_source_seq=packet.source_seq
        # The Luna bridge's clock and controller clock need not share an epoch.
        # Never use the producer's monotonic timestamp for ordering callbacks.
        if self.ctrl.state is ControllerState.WAIT_TRANSITION_ACK:
            if self._saw_fall_after_sent:
                if self.pending is None:
                    self.pending_at_ms=at_ms
                self.pending=packet
                self._saw_fresh_candidate_after_sent=True
        elif self.ctrl.state is ControllerState.WAIT_LINE and self.ctrl.line is None:
            if self.pending is None:
                self.pending_at_ms=at_ms
            self.pending=packet
        elif self.ctrl.line is not None:
            line=self.ctrl.line
            if packet.raw_text==line.text.raw:
                return
            if (line.ready_ms is None and self.ctrl.state is not ControllerState.PAUSED and
                packet.raw_text.startswith(line.text.raw)):
                self._post(EK.LINE_UPDATED,at_ms,text=packet.raw_text)
                if self.ctrl.state is not ControllerState.PAUSED:
                    self.on_line_updated(line.epoch,line.token,line.text.matching)
            else:
                self._post(EK.LINE_INVALIDATED,at_ms)

    def observe(self,sample:ScreenSample, *, focused:bool, capture_ok:bool,
                manual_intervened:bool=False, voice_metadata:MetadataVoice | None=None)->None:
        now=sample.at_ms
        if manual_intervened:
            self._post(EK.MANUAL_OVERRIDE,now)
            return
        if sample.state!=self._last_screen:
            self._last_screen=sample.state
            self._post(EK.SCREEN_CHANGED,now,screen=sample.state)
        if focused!=self.ctrl.focused:
            self._post(EK.FOCUS_CHANGED,now,focused=focused)
        if capture_ok!=self.ctrl.capture_ok:
            self._post(EK.CAPTURE_HEALTH,now,capture_ok=capture_ok)
        if self.ctrl.state is ControllerState.PAUSED:
            return
        if self.ctrl.state is ControllerState.WAIT_TRANSITION_ACK:
            if not sample.ready:
                if not self._saw_fall_after_sent:
                    self._fall_at_ms=now
                self._saw_fall_after_sent=True
            if (self._saw_fall_after_sent and self._saw_fresh_candidate_after_sent
                and sample.state is ScreenState.DIALOGUE and sample.ready
                and sample.text_matches and self.pending is not None):
                transition_floor=self._fall_at_ms if self._fall_at_ms is not None else now
                self._line_counter+=1
                self._post(EK.TRANSITION_ACK,now,transition_id=f'visual-ready-cycle-{self._line_counter}')
                if self.ctrl.state is ControllerState.WAIT_LINE:
                    self.on_line_boundary(transition_floor)
                self._saw_fall_after_sent=False
                self._fall_at_ms=None
                self._saw_fresh_candidate_after_sent=False
            else:
                return
        if self.ctrl.state is ControllerState.WAIT_LINE and self.pending is not None:
            if not (sample.state is ScreenState.DIALOGUE and sample.text_matches):
                return
            packet=self.pending
            first_seen=self.pending_at_ms if self.pending_at_ms is not None else now
            self.pending=None
            self.pending_at_ms=None
            self._active_candidate_seq=packet.source_seq
            self._post(EK.LINE_CANDIDATE,now,source_seq=packet.source_seq,
                       source_line_id=f'visual:{self._line_counter}:{packet.source_seq}',
                       text=packet.raw_text,text_ready=False,
                       empty_confirmed=sample.empty_event_confirmed)
            line=self.ctrl.line
            if line is not None and line.ready_ms is None and self.ctrl.state is not ControllerState.PAUSED:
                self.on_line_started(line.epoch,line.token,line.text.matching,first_seen)
        line=self.ctrl.line
        if line is None:return
        if sample.state is ScreenState.DIALOGUE and sample.ready and sample.text_matches and line.ready_ms is None:
            # Only a completed display can establish the actual speaker;
            # the first partial candidate may not contain their full name.
            if self.speaker_router is not None:
                route=self.speaker_router.resolve(line.text.raw)
                self.ctrl.configure_tts(route.policy, selected_voice=route.voice_id,
                                        default_voice=self.ctrl.default_voice_id)
            self._post(EK.LINE_READY,now)
        if (voice_metadata is None and self.ctrl.settings.allow_heuristic_absence and
            line.voice is VoicePresence.UNRESOLVED and sample.ready and
            capture_ok and now>=line.first_seen_ms+self.ctrl.settings.grace_ms and
            self.heuristic_absence_ready()):
            # An explicitly calibrated (game-specific) hypothesis; never
            # represent silence, failed ASR or capture failure as metadata.
            self._post(EK.VOICE_PRESENCE,now,voice=VoicePresence.ABSENT,
                       evidence=Evidence.HEURISTIC)
        if voice_metadata is not None and voice_metadata.verified:
            signature=(line.epoch,line.token,voice_metadata.asset_assigned,voice_metadata.asset_id)
            if signature != self._last_metadata:
                if voice_metadata.asset_assigned and voice_metadata.asset_id:
                    self._post(EK.VOICE_PRESENCE,now,voice=VoicePresence.PRESENT,
                               evidence=Evidence.METADATA,native_asset_id=voice_metadata.asset_id)
                    self._last_metadata=signature
                elif not voice_metadata.asset_assigned:
                    self._post(EK.VOICE_PRESENCE,now,voice=VoicePresence.ABSENT,evidence=Evidence.METADATA)
                    self._last_metadata=signature
        self.runtime.poll()
        self.runtime.step()
