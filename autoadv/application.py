"""Single text-source observation -> Utterance and audio lifecycle.

A source's stable output may start presentation. It never supplies an advance
permission or certainty about a game scene.
"""
from __future__ import annotations
from typing import Callable
from .contracts import Envelope, EventKind as EK, Evidence, VoicePresence, ControllerState
from .observation import OriginObservation
from .runtime import Runtime


class ObservationOrchestrator:
    def __init__(self, runtime: Runtime,
                 on_line_started: Callable[[int,int,str,int],None] | None=None,
                 on_line_updated: Callable[[int,int,str],None] | None=None,
                 on_line_boundary: Callable[[int],None] | None=None,
                 heuristic_absence_ready: Callable[[],bool] | None=None,
                 binding_ok: Callable[[],bool]=lambda:False,
                 stable_ms: int = 350):
        self.runtime=runtime
        self.ctrl=runtime.controller
        self.on_line_started=on_line_started or (lambda *_:None)
        self.on_line_updated=on_line_updated or (lambda *_:None)
        self.on_line_boundary=on_line_boundary or (lambda *_:None)
        self.heuristic_absence_ready=heuristic_absence_ready or (lambda:False)
        self.binding_ok=binding_ok
        self.stable_ms=stable_ms
        self.pending: OriginObservation | None=None
        self.pending_at_ms: int | None=None
        self._last_source_seq=0
        self._settled_text: str | None=None

    def _post(self, kind: EK, now: int, **fields):
        line=self.ctrl.line
        self.runtime.post(Envelope(kind,self.ctrl.session_id,now,
             line_epoch=line.epoch if line else None,
             cancel_token=line.token if line else None,**fields))
        self.runtime.step()

    def disconnected(self,at_ms:int):
        self.pending=None
        self.pending_at_ms=None
        self._settled_text=None
        self._post(EK.MANUAL_OVERRIDE,at_ms)

    def unstable(self,at_ms:int,reason:str='selected_source_unstable'):
        """Blank/failed selected source cancels candidates; never creates a line."""
        self.pending=None
        self.pending_at_ms=None
        self._settled_text=None
        if self.ctrl.line is not None and self.ctrl.state is not ControllerState.PAUSED:
            self._post(EK.LINE_INVALIDATED,at_ms)

    def candidate(self,packet:OriginObservation,at_ms:int,*,manual_intervened:bool=False):
        if packet.sequence<=self._last_source_seq:return
        self._last_source_seq=packet.sequence
        if not packet.text.strip() or not self.binding_ok():return
        if manual_intervened:
            self.disconnected(at_ms)
            return
        if self.ctrl.state is ControllerState.PAUSED:
            return
        if self.ctrl.state is ControllerState.WAIT_NEXT_HOOK:
            old=self.ctrl.line
            if (old is None or packet.kind != 'hook' or old.source_kind != 'hook'
                or packet.origin != old.source_origin
                or packet.source_generation != old.source_generation):
                self.disconnected(at_ms)
                return
            if packet.text == old.text.observed_original:
                return
            previous_epoch=old.epoch
            self._post(EK.LINE_CANDIDATE,at_ms,source_seq=packet.sequence,text=packet.text,
                source_kind=packet.kind,source_origin=packet.origin,
                source_generation=packet.source_generation)
            line=self.ctrl.line
            if line is not None and line.epoch != previous_epoch:
                self.pending_at_ms=at_ms
                self.on_line_boundary(at_ms)
                self.on_line_started(line.epoch,line.token,line.text.matching,at_ms)
            return
        if self.ctrl.line is not None:
            line=self.ctrl.line
            if packet.text==line.text.observed_original:return
            if line.ready_ms is None and packet.text.startswith(line.text.observed_original):
                self._post(EK.LINE_UPDATED,at_ms,text=packet.text)
                self.pending=packet
                self.pending_at_ms=at_ms
                self.on_line_updated(line.epoch,line.token,line.text.matching)
            else:
                self._post(EK.LINE_INVALIDATED,at_ms)
            return
        if self.pending is None or self.pending.text!=packet.text:
            self.pending=packet
            self.pending_at_ms=at_ms
        # Identical text does not prove a new line; never re-send on a timer.

    def tick(self,now:int,*,focused:bool,capture_ok:bool):
        if not self.binding_ok():
            if self.ctrl.line is not None or self.pending is not None:
                self.disconnected(now)
            return
        if focused != self.ctrl.focused:self._post(EK.FOCUS_CHANGED,now,focused=focused)
        if capture_ok != self.ctrl.capture_ok:self._post(EK.CAPTURE_HEALTH,now,capture_ok=capture_ok)
        if self.ctrl.state is ControllerState.PAUSED:return
        if self.ctrl.state is ControllerState.WAIT_LINE and self.ctrl.line is None and self.pending:
            packet=self.pending
            first=self.pending_at_ms if self.pending_at_ms is not None else now
            self.pending=None
            self.pending_at_ms=first
            self._post(EK.LINE_CANDIDATE,now,source_seq=packet.sequence,text=packet.text,
                 source_kind=packet.kind,source_origin=packet.origin,
                 source_generation=packet.source_generation)
            line=self.ctrl.line
            if line is not None:
                self.on_line_started(line.epoch,line.token,line.text.matching,first)
        line=self.ctrl.line
        if line is None:return
        if line.ready_ms is None and self.pending_at_ms is not None and now-self.pending_at_ms>=self.stable_ms:
            # Buffered HOOK text / stable OCR observation: presentation-only
            # readiness. It says nothing about game input or visual display.
            self._post(EK.LINE_READY,now)
            self._settled_text=line.text.observed_original
        if (self.ctrl.settings.allow_heuristic_absence and line.voice is VoicePresence.UNRESOLVED
            and line.ready_ms is not None and capture_ok
            and now>=line.first_seen_ms+self.ctrl.settings.grace_ms
            and self.heuristic_absence_ready()):
            self._post(EK.VOICE_PRESENCE,now,voice=VoicePresence.ABSENT,evidence=Evidence.HEURISTIC)
        self.runtime.poll()
        self.runtime.step()
