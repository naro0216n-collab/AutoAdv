"""Serial effect executor with fresh pre-input guard and single input authority."""
from __future__ import annotations
from dataclasses import dataclass
from queue import Queue, Empty
from typing import Callable, Protocol
import time

from .contracts import Effect, EffectKind, Envelope, EventKind, ScreenState
from .controller import Controller


@dataclass(frozen=True)
class FreshSnapshot:
    screen: ScreenState
    focused: bool
    text_ready: bool
    capture_ok: bool
    # Conservative defaults: unknown blocker state forbids an advance.
    native_voice_active: bool = True
    tts_playing: bool = True
    line_matches: bool = False
    target_window_matches: bool = False
    input_available: bool = False
    manual_override: bool = True
    transition_pending: bool = True


class Guard(Protocol):
    def observe(self) -> FreshSnapshot: ...


class InputDriver(Protocol):
    def advance(self) -> None: ...


class TTSExecutor(Protocol):
    def apply(self, effect: Effect) -> None: ...


class Runtime:
    """All callbacks post to the same serial queue; never call InputDriver.

    The fresh guard is deliberately independent of controller's cached state.
    The guard must derive screen classification and text-ready from real game
    observations (not from receiving a Luna-origin packet).
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

    def post(self, event: Envelope) -> None:
        self.queue.put(event)

    def step(self, timeout: float = 0) -> bool:
        try:
            event = self.queue.get(timeout=timeout)
        except Empty:
            return False
        self.audit("event", {"kind": event.kind.name, "epoch": event.line_epoch,
                             "session": event.session_id, "at_ms": event.at_ms})
        effects = self.controller.accept(event)
        for effect in effects:
            self.audit("effect", {"kind": effect.kind.name, "epoch": effect.line_epoch,
                                  "reason": effect.reason})
            if effect.kind in (EffectKind.START_TTS, EffectKind.CANCEL_TTS):
                self.tts.apply(effect)
            elif effect.kind is EffectKind.REQUEST_ADVANCE:
                # Read guard synchronously immediately before SendInput.
                snapshot = self.guard.observe()
                if not self.controller.confirm_advance(
                    line_epoch=effect.line_epoch, cancel_token=effect.cancel_token,
                    fresh_screen=snapshot.screen, fresh_focused=snapshot.focused,
                    fresh_text_ready=snapshot.text_ready, fresh_capture_ok=snapshot.capture_ok,
                    fresh_native_voice_active=snapshot.native_voice_active,
                    fresh_tts_playing=snapshot.tts_playing,
                    fresh_line_matches=snapshot.line_matches,
                    fresh_target_window_matches=snapshot.target_window_matches,
                    fresh_input_available=snapshot.input_available,
                    fresh_manual_override=snapshot.manual_override,
                    fresh_transition_pending=snapshot.transition_pending,
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
                except Exception:
                    self.controller.input_failed()
                    self.audit("input_failed", {"epoch": effect.line_epoch})
        return True

    def poll(self) -> None:
        self.controller.transition_timed_out(self.clock())
        l = self.controller.line
        if l is not None:
            self.post(Envelope(EventKind.TIMER, self.controller.session_id, self.clock(),
                               line_epoch=l.epoch, cancel_token=l.token))