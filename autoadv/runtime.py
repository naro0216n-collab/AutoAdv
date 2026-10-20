"""Serial effect executor with fresh pre-input guard and single input authority."""
from __future__ import annotations
from dataclasses import dataclass
from queue import Queue, Empty
from typing import Callable, Protocol
import hashlib
import time

from .contracts import Effect, EffectKind, Envelope, EventKind
from .controller import Controller


@dataclass(frozen=True)
class FreshSnapshot:
    focused: bool = False
    capture_ok: bool = False
    # Unknown blockers conservatively prohibit input.
    native_voice_active: bool = True
    tts_playing: bool = True
    target_window_matches: bool = False
    input_available: bool = False
    manual_override: bool = True


class Guard(Protocol):
    def observe(self) -> FreshSnapshot: ...


class InputDriver(Protocol):
    def advance(self) -> None: ...


class TTSExecutor(Protocol):
    def apply(self, effect: Effect) -> None: ...


class Runtime:
    """All callbacks post to the same serial queue; never call InputDriver.

    The fresh guard is deliberately independent of controller's cached state.
    The guard checks current window/audio/manual state immediately before
    input; a selected HOOK candidate alone does not grant permission.
    """

    def __init__(self, controller: Controller, guard: Guard, input_driver: InputDriver,
                 tts: TTSExecutor, *, clock: Callable[[], int] | None = None,
                 audit: Callable[[str, dict], None] | None = None):
        self.controller = controller
        self.guard = guard
        self.input_driver = input_driver
        self.tts = tts
        self.clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self.queue: Queue[Envelope] = Queue()
        self.audit = audit or (lambda name, details: None)
        self._reported_state: tuple | None = None
        self._finalized: set[int] = set()

    def post(self, event: Envelope) -> None:
        self.queue.put(event)

    def step(self, timeout: float = 0) -> bool:
        try:
            event = self.queue.get(timeout=timeout)
        except Empty:
            return False
        if event.kind is not EventKind.TIMER:
            self.audit("event", {"kind": event.kind.name, "epoch": event.line_epoch,
                                 "session": event.session_id, "at_ms": event.at_ms,
                                 "native_evidence": event.evidence.name,
                                 "voice_presence": event.voice.name if event.voice else None,
                                 "capture_ok": event.capture_ok,
                                 "tts_success": event.tts_success})
        previous = self.controller.line
        effects = self.controller.accept(event)
        for effect in effects:
            self.audit("effect", {"kind": effect.kind.name, "epoch": effect.line_epoch,
                                  "reason": effect.reason})
            if effect.kind in (EffectKind.START_TTS, EffectKind.CANCEL_TTS):
                try:
                    self.tts.apply(effect)
                except Exception as exc:
                    self.audit("tts_dispatch_failed", {"epoch": effect.line_epoch,
                                                       "error_type": type(exc).__name__})
                    for stop in self.controller._pause('tts_dispatch_failed'):
                        self.audit("effect", {"kind": stop.kind.name, "reason": stop.reason})
            elif effect.kind is EffectKind.REQUEST_ADVANCE:
                # An already-queued request is harmless after explicit pause,
                # manual intervention or a new utterance generation. Do not
                # turn ordinary revocation into a latched hardware failure.
                current = self.controller.line
                if (not self.controller.autoplay_authorized or current is None
                    or current.epoch != effect.line_epoch
                    or current.token != effect.cancel_token
                    or not current.request_pending):
                    self.audit('advance_revoked', {'epoch': effect.line_epoch})
                    continue
                # Read guard synchronously immediately before SendInput.
                snapshot = self.guard.observe()
                if not self.controller.confirm_advance(
                    line_epoch=effect.line_epoch, cancel_token=effect.cancel_token,
                    fresh_focused=snapshot.focused,
                    fresh_capture_ok=snapshot.capture_ok,
                    fresh_native_voice_active=snapshot.native_voice_active,
                    fresh_tts_playing=snapshot.tts_playing,
                    fresh_target_window_matches=snapshot.target_window_matches,
                    fresh_input_available=snapshot.input_available,
                    fresh_manual_override=snapshot.manual_override,
                    at_ms=self.clock(),
                ):
                    self.audit("advance_rejected", {"epoch": effect.line_epoch})
                    # A failed preflight MUST NOT retry automatically.
                    for stop in self.controller._pause("advance_preflight_rejected"):
                        self.audit("effect", {"kind": stop.kind.name, "reason": stop.reason})
                        if stop.kind is EffectKind.CANCEL_TTS:
                            self.tts.apply(stop)
                    continue
                try:
                    self.input_driver.advance()
                    self.audit('input_injected',{
                        'session_id':effect.session_id,
                        'line_epoch':effect.line_epoch,
                        'cancel_token':effect.cancel_token,
                        'at_ms':self.clock(),
                    })
                except Exception:
                    for stop in self.controller.input_failed():
                        self.audit("effect", {"kind": stop.kind.name, "reason": stop.reason})
                    self.audit("input_failed", {"epoch": effect.line_epoch})
        self._audit_state(event,previous)
        return True

    def _line_fields(self, line) -> dict:
        c = self.controller
        return {
            'session_id':c.session_id, 'line_epoch':line.epoch,
            'source_line_id':line.source_line_id,
            'source_kind':line.source_kind,
            'source_origin':line.source_origin,
            'source_generation':line.source_generation,
            'raw_sha256':hashlib.sha256(line.text.observed_original.encode('utf-8')).hexdigest(),
            'content_kind':line.text.content_kind.name,
            'tts_policy':line.text.tts_route.name,
            'tts_selected_voice':line.text.voice_id,
            'audio_route':line.text.audio_route.name,
            'presentation':line.presentation.name,
            'native_presence':line.voice.name,
            'native_evidence':line.evidence.name,
            'native_utterance_id':line.utterance_id,
            'native_asset_id':line.native_asset_id,
            'text_ready_at_ms':line.ready_ms,
            'native_onset_ms':line.native_started_at_ms,
            'native_end_ms':line.native_completed_at_ms,
            'tts_started_ms':line.tts_started_at_ms,
            'tts_completed_ms':line.tts_ended_at_ms,
            'tts_cancelled':line.tts_canceled,
            'silent_dwell_started_ms':line.silent_started_at_ms,
            'presentation_complete_at_ms':line.presentation_complete_at_ms,
            'input_at_ms':line.sent_at_ms,
            'pause_reason':c.pause_reason,
        }

    def _finalize_line(self, line, status:str, event:Envelope | None=None) -> None:
        if line.epoch in self._finalized:return
        self._finalized.add(line.epoch)
        details=self._line_fields(line)
        details['final_status']=status
        self.audit('line_summary',details)

    def _audit_state(self, event:Envelope, previous) -> None:
        line = self.controller.line
        if line is not None:
            snapshot=(line.epoch,self.controller.state,line.presentation,line.voice,line.evidence,
                      line.ready_ms,line.sent_at_ms,line.native_started_at_ms,
                      line.native_completed_at_ms,line.tts_ended_at_ms,line.tts_canceled,
                      self.controller.pause_reason)
            if snapshot != self._reported_state:
                self.audit('line_state',{'state':self.controller.state.name,**self._line_fields(line)})
                self._reported_state=snapshot
            if self.controller.state.name=='PAUSED':
                self._finalize_line(line,'paused')
        if (previous is not None and line is not None and line is not previous
                and event.kind is EventKind.LINE_CANDIDATE):
            self._finalize_line(previous,'advanced_to_new_hook_candidate',event)
            self._reported_state=None

    def finalize(self, reason:str='session_ended') -> None:
        line=self.controller.line
        if line is not None:
            self._finalize_line(line,reason)

    def poll(self) -> None:
        for effect in self.controller.next_hook_timed_out(self.clock()):
            self.audit("effect", {"kind": effect.kind.name, "reason": effect.reason})
            if effect.kind is EffectKind.CANCEL_TTS:
                self.tts.apply(effect)
        l = self.controller.line
        if l is not None:
            self.post(Envelope(EventKind.TIMER, self.controller.session_id, self.clock(),
                               line_epoch=l.epoch, cancel_token=l.token))