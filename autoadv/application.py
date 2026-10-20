"""Origin-observation to Controller bridge; all effects execute via Runtime.

A WS packet is a mutable candidate, never an authoritative line event.
Only positive visual evidence, binding, and observed transitions release lines.
"""
from __future__ import annotations
from typing import Callable
from .contracts import Envelope,EventKind as EK,Evidence,VoicePresence,ScreenState,ControllerState
from .luna import OriginObservation
from .screen import ScreenSample
from .runtime import Runtime


class ObservationOrchestrator:
    def __init__(self,runtime:Runtime,
                 on_line_started:Callable[[int,int,str,int],None]|None=None,
                 on_line_updated:Callable[[int,int,str],None]|None=None,
                 on_line_boundary:Callable[[int],None]|None=None,
                 heuristic_absence_ready:Callable[[],bool]|None=None,
                 binding_ok:Callable[[],bool]=lambda:False):
        self.runtime=runtime
        self.ctrl=runtime.controller
        self.on_line_started=on_line_started or (lambda *_:None)
        self.on_line_updated=on_line_updated or (lambda *_:None)
        self.on_line_boundary=on_line_boundary or (lambda *_:None)
        self.heuristic_absence_ready=heuristic_absence_ready or (lambda:False)
        self.binding_ok=binding_ok
        self.pending:OriginObservation|None=None
        self.pending_at_ms:int|None=None
        self._seen_source_seq=0
        self._last_screen=ScreenState.UNKNOWN
        self._saw_fall_after_sent=False
        self._fall_at_ms:int|None=None
        self._saw_fresh_candidate_after_sent=False
        self._last_manual_seq=0
        self._line_counter=0

    def _post(self,kind:EK,now:int,**fields):
        line=self.ctrl.line
        self.runtime.post(Envelope(kind,self.ctrl.session_id,now,
            line_epoch=line.epoch if line else None,
            cancel_token=line.token if line else None,**fields))
        self.runtime.step()

    def disconnected(self,at_ms:int):
        self.pending=None
        self.pending_at_ms=None
        self._post(EK.MANUAL_OVERRIDE,at_ms)

    def candidate(self,packet:OriginObservation,at_ms:int):
        if packet.sequence<=self._seen_source_seq:return
        self._seen_source_seq=packet.sequence
        if not packet.text.strip():return  # no invented empty event
        if not self.binding_ok():return
        if self.ctrl.state in (ControllerState.PAUSED,ControllerState.WAIT_MANUAL_CHOICE):return
        if self.ctrl.state is ControllerState.WAIT_TRANSITION_ACK:
            if self._saw_fall_after_sent:
                if self.pending is None:self.pending_at_ms=at_ms
                self.pending=packet
                self._saw_fresh_candidate_after_sent=True
        elif self.ctrl.state is ControllerState.WAIT_LINE and self.ctrl.line is None:
            if self.pending is None:self.pending_at_ms=at_ms
            self.pending=packet
        elif self.ctrl.line is not None:
            line=self.ctrl.line
            if packet.text==line.text.observed_original:return
            if (line.ready_ms is None and self.ctrl.state is not ControllerState.PAUSED
                and packet.text.startswith(line.text.observed_original)):
                self._post(EK.LINE_UPDATED,at_ms,text=packet.text)
                if self.ctrl.state is not ControllerState.PAUSED:
                    self.on_line_updated(line.epoch,line.token,line.text.matching)
            else:
                self._post(EK.LINE_INVALIDATED,at_ms)

    def observe(self,sample:ScreenSample,*,focused:bool,capture_ok:bool,manual_intervened:bool=False):
        now=sample.at_ms
        if not self.binding_ok():
            if self.ctrl.line is not None:self.disconnected(now)
            return
        if manual_intervened and sample.state is not ScreenState.MANUAL_REQUIRED and self.ctrl.state is not ControllerState.WAIT_MANUAL_CHOICE:
            self._post(EK.MANUAL_OVERRIDE,now)
            return
        was_manual=self.ctrl.state is ControllerState.WAIT_MANUAL_CHOICE
        if sample.state!=self._last_screen:
            self._last_screen=sample.state
            self._post(EK.SCREEN_CHANGED,now,screen=sample.state)
        if sample.state is ScreenState.MANUAL_REQUIRED:
            self.pending=None
            self.pending_at_ms=None
            self._last_manual_seq=self._seen_source_seq
        if focused!=self.ctrl.focused:self._post(EK.FOCUS_CHANGED,now,focused=focused)
        if capture_ok!=self.ctrl.capture_ok:self._post(EK.CAPTURE_HEALTH,now,capture_ok=capture_ok)
        if self.ctrl.state in (ControllerState.PAUSED,ControllerState.WAIT_MANUAL_CHOICE):return
        if was_manual:
            # Require a fresh post-choice candidate; leaving a choice by itself proves nothing.
            self.pending=None
            self.pending_at_ms=None
            self._last_manual_seq=self._seen_source_seq
            return
        if self.ctrl.state is ControllerState.WAIT_TRANSITION_ACK:
            if not sample.ready:
                if not self._saw_fall_after_sent:self._fall_at_ms=now
                self._saw_fall_after_sent=True
            if (self._saw_fall_after_sent and self._saw_fresh_candidate_after_sent
                and sample.state is ScreenState.DIALOGUE and sample.ready and sample.positive
                and self.pending is not None):
                transition_floor=self._fall_at_ms if self._fall_at_ms is not None else now
                self._line_counter+=1
                self._post(EK.TRANSITION_ACK,now,transition_id=f'visual-ready-cycle-{self._line_counter}')
                if self.ctrl.state is ControllerState.WAIT_LINE:self.on_line_boundary(transition_floor)
                self._saw_fall_after_sent=False
                self._fall_at_ms=None
                self._saw_fresh_candidate_after_sent=False
            else:return
        if self.ctrl.state is ControllerState.WAIT_LINE and self.pending is not None:
            if sample.state is not ScreenState.DIALOGUE or not sample.positive:return
            packet=self.pending
            first_seen=self.pending_at_ms if self.pending_at_ms is not None else now
            self.pending=None
            self.pending_at_ms=None
            self._post(EK.LINE_CANDIDATE,now,source_seq=packet.sequence,
                       text=packet.text,text_ready=False)
            line=self.ctrl.line
            if line is not None and line.ready_ms is None and self.ctrl.state is not ControllerState.PAUSED:
                self.on_line_started(line.epoch,line.token,line.text.matching,first_seen)
        line=self.ctrl.line
        if line is None:return
        if sample.state is ScreenState.DIALOGUE and sample.ready and sample.positive and line.ready_ms is None:
            self._post(EK.LINE_READY,now)
        if (self.ctrl.settings.allow_heuristic_absence and line.voice is VoicePresence.UNRESOLVED
            and sample.ready and capture_ok and now>=line.first_seen_ms+self.ctrl.settings.grace_ms
            and self.heuristic_absence_ready()):
            self._post(EK.VOICE_PRESENCE,now,voice=VoicePresence.ABSENT,evidence=Evidence.HEURISTIC)
        self.runtime.poll()
        self.runtime.step()
