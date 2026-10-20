"""Single-owner HOOK and native-audio synchronized advancement state machine.

No hardware driver or ML model is allowed to issue advancement directly.
All asynchronous events carry the session, line epoch, cancel token and a
monotonic timestamp. Drivers must revalidate a fresh game snapshot before
honouring REQUEST_ADVANCE.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import uuid

from .contracts import (
    ContentKind, ControllerState as S, Effect, EffectKind as FX, Envelope,
    EventKind as EK, Evidence, Playback, Presentation,
    AudioRoute, TTSPolicy, VoicePresence,
)
from .text import Utterance, TextInterpreter, GameTextConfig


@dataclass(frozen=True)
class Settings:
    autoplay: bool = False
    grace_ms: int = 500
    silent_base_ms: int = 700
    silent_min_ms: int = 1000
    reading_chars_per_sec: float = 12.0
    symbolic_short_ms: int = 1500
    symbolic_long_ms: int = 2500
    symbolic_other_ms: int = 1000
    tts_required: bool = False
    # Heuristic native absence needs positively verified ASR silence, never a timer alone.
    allow_heuristic_absence: bool = True
    name_readings: tuple[tuple[str, str], ...] = ()
    next_hook_timeout_ms: int = 10000
    unresolved_timeout_ms: int = 6000
    native_start_timeout_ms: int = 30000
    native_end_timeout_ms: int = 180000

    def __post_init__(self) -> None:
        if self.grace_ms < 0 or self.silent_min_ms < 0 or self.silent_base_ms < 0:
            raise ValueError("durations must be nonnegative")
        if any(x <= 0 for x in (self.next_hook_timeout_ms, self.unresolved_timeout_ms,
                                 self.native_start_timeout_ms, self.native_end_timeout_ms)):
            raise ValueError("timeout must be positive")
        if self.reading_chars_per_sec <= 0:
            raise ValueError("reading_chars_per_sec must be positive")


@dataclass
class Line:
    epoch: int
    token: int
    text: Utterance
    source_line_id: Optional[str]
    first_seen_ms: int
    source_seq: int
    source_kind: Optional[str] = None
    source_origin: Optional[str] = None
    source_generation: Optional[int] = None
    ready_ms: Optional[int] = None
    observation_since_ms: Optional[int] = None
    voice: VoicePresence = VoicePresence.UNRESOLVED
    evidence: Evidence = Evidence.NONE
    playback: Playback = Playback.NOT_STARTED
    utterance_id: Optional[str] = None
    presentation: Presentation = Presentation.UNRESOLVED
    tts_job_started: bool = False
    tts_finished: bool = False
    tts_canceled: bool = False
    sent: bool = False
    request_pending: bool = False
    sent_at_ms: Optional[int] = None
    native_asset_id: Optional[str] = None
    native_started_at_ms: Optional[int] = None
    native_completed_at_ms: Optional[int] = None
    tts_started_at_ms: Optional[int] = None
    tts_ended_at_ms: Optional[int] = None
    silent_started_at_ms: Optional[int] = None
    presentation_complete_at_ms: Optional[int] = None


class Controller:
    """A session has one controller and one owner of line_epoch.

    Input is permitted only after explicit authorization, stable HOOK text,
    resolved audio playback, and fresh window/manual preflight.
    """

    def __init__(self, settings: Settings | None = None, session_id: str | None = None, interpreter: TextInterpreter | None = None):
        self.settings = settings or Settings()
        self.interpreter = interpreter or TextInterpreter(GameTextConfig(name_readings=dict(self.settings.name_readings)))
        self.session_id = session_id or str(uuid.uuid4())
        self.state = S.WAIT_LINE
        self.focused = False
        self.capture_ok = False
        self.line: Optional[Line] = None
        self._epoch = 0
        self._token = 0
        self._last_source_seq = -1
        self._last_at_ms = -1
        # Producer-local ordering, not a total order across unrelated sources.
        # A polling timer must never erase delayed audio or focus events.
        self._last_kind_at_ms: dict[tuple[EK, int | None], int] = {}
        self.pause_reason: Optional[str] = None
        self.autoplay_authorized = bool(self.settings.autoplay)
    def authorize(self) -> None:
        """Permit advancement of the currently observed utterance or next one.

        Monitoring and presentation do not depend on this permission. A
        fault-latched session cannot be resumed by changing a GUI button.
        """
        if self.state is S.PAUSED:
            raise RuntimeError('stopped session requires a new verified session')
        self.autoplay_authorized = True

    def pause(self, reason: str = 'explicit_pause') -> list[Effect]:
        """Revoke game-input authority without stopping observation or audio.

        A pending REQUEST_ADVANCE may already be queued, so preflight must
        also independently check autoplay_authorized before any input.
        """
        self.autoplay_authorized = False
        if self.line is not None:
            self.line.request_pending = False
            if self.state is S.READY_TO_ADVANCE:
                self.state = S.RESOLVE_PRESENTATION
        return []

    def _invalidate_for_manual_input(self) -> list[Effect]:
        """Drop the old utterance and its callbacks; continue monitoring.

        A fresh selected-source observation may be presented after manual
        interaction, but it never inherits the old advance permission.
        """
        effects: list[Effect] = []
        if self.line is not None and self.line.tts_job_started and not self.line.tts_finished and not self.line.tts_canceled:
            self.line.tts_canceled = True
            effects.append(self._effect(FX.CANCEL_TTS, 'manual_override'))
        self.autoplay_authorized = False
        self._token += 1
        self.line = None
        self.pause_reason = 'manual_override'
        self.state = S.WAIT_LINE
        return effects

    def _effect(self, kind: FX, reason: str = "", voice: str | None = None) -> Effect:
        assert self.line is not None
        return Effect(kind, self.session_id, self.line.epoch, self.line.token,
                      text=self.line.text.tts_text, voice_id=voice, reason=reason)

    def _pause(self, reason: str) -> list[Effect]:
        self.autoplay_authorized=False
        if self.line is None:
            self.state = S.PAUSED
            self.pause_reason = reason
            return []
        effects: list[Effect] = []
        if self.line.tts_job_started and not self.line.tts_finished and not self.line.tts_canceled:
            self.line.tts_canceled = True
            effects.append(self._effect(FX.CANCEL_TTS, reason))
        self.line.request_pending = False
        self.state = S.PAUSED
        self.pause_reason = reason
        effects.append(self._effect(FX.STOPPED, reason))
        return effects

    def _new_line(self, e: Envelope) -> None:
        self._epoch += 1
        self._token += 1
        utterance=self.interpreter.parse(e.text or "")
        self.line = Line(self._epoch, self._token, utterance,
                         e.source_line_id, e.at_ms, e.source_seq or 0,
                         e.source_kind,e.source_origin,e.source_generation,
                         observation_since_ms=e.at_ms if self.focused and self.capture_ok else None)
        self.state = S.WAIT_TEXT_READY
        self.pause_reason = None
        if e.text_ready:
            self.line.ready_ms = e.at_ms
            self.state = S.RESOLVE_PRESENTATION

    def _check_event(self, e: Envelope) -> bool:
        if e.session_id != self.session_id:
            return False
        # Candidate packets carry an independent source_seq; their producer
        # clock is not a reliable ordering oracle for other observations.
        order_key = (e.kind, e.line_epoch if e.kind in (
            EK.VOICE_PRESENCE, EK.NATIVE_STARTED, EK.NATIVE_COMPLETED,
            EK.TTS_FINISHED, EK.TIMER, EK.LINE_READY, EK.LINE_UPDATED,
            EK.LINE_INVALIDATED) else None)
        if (e.kind is not EK.MANUAL_OVERRIDE and e.kind is not EK.LINE_CANDIDATE and
            e.at_ms < self._last_kind_at_ms.get(order_key, -1)):
            return False
        # Callback events and their cancellation guards may never reach a new line.
        if e.kind in (EK.VOICE_PRESENCE, EK.NATIVE_STARTED, EK.NATIVE_COMPLETED,
                      EK.TTS_FINISHED, EK.TIMER, EK.LINE_READY, EK.LINE_UPDATED,
                      EK.LINE_INVALIDATED):
            if self.line is None or e.line_epoch != self.line.epoch or e.cancel_token != self.line.token:
                return False
        return True

    def accept(self, e: Envelope) -> list[Effect]:
        if not self._check_event(e):
            return []
        if self.state is S.PAUSED:
            # Safety faults remain latched even if a later manual action occurs.
            # Only a new verified session can recover from a hard stop.
            return []
        self._last_at_ms = max(self._last_at_ms, e.at_ms)
        order_key = (e.kind, e.line_epoch if e.kind in (
            EK.VOICE_PRESENCE, EK.NATIVE_STARTED, EK.NATIVE_COMPLETED,
            EK.TTS_FINISHED, EK.TIMER, EK.LINE_READY, EK.LINE_UPDATED,
            EK.LINE_INVALIDATED) else None)
        self._last_kind_at_ms[order_key] = max(
            e.at_ms, self._last_kind_at_ms.get(order_key, -1))
        if e.kind is EK.MANUAL_OVERRIDE:
            return self._invalidate_for_manual_input()
        if e.kind is EK.FOCUS_CHANGED:
            # The user must be able to inspect the GUI and grant permission
            # while the game is in the background. Fresh input preflight still
            # prohibits sending anything to an unfocused target.
            self.focused = e.focused is True
        elif e.kind is EK.CAPTURE_HEALTH:
            self.capture_ok = e.capture_ok is True
        if e.kind in (EK.FOCUS_CHANGED, EK.CAPTURE_HEALTH) and self.line is not None:
            if self.focused and self.capture_ok:
                if self.line.observation_since_ms is None:
                    self.line.observation_since_ms = e.at_ms
            else:
                self.line.observation_since_ms = None
        if e.kind is EK.LINE_CANDIDATE:
            if e.source_seq is None or e.source_seq <= self._last_source_seq:
                return []
            self._last_source_seq = e.source_seq
            if self.line is None and self.state is S.WAIT_LINE:
                self._new_line(e)
            elif self.line is not None:
                # A repeated origin text does NOT mean a second line.
                # A post-input, changed HOOK candidate may begin the next epoch.
                if self.state is S.WAIT_NEXT_HOOK:
                    old=self.line
                    # No visual ACK and no time-only assumption: a new, different
                    # selected HOOK observation after our one input starts a new line.
                    if (e.source_kind != 'hook' or old.source_kind != 'hook'
                        or e.source_generation != old.source_generation
                        or e.source_origin != old.source_origin
                        or not e.text or e.text == old.text.observed_original
                        or not old.sent_at_ms or e.at_ms <= old.sent_at_ms
                        or e.source_seq <= old.source_seq):
                        return []
                    self._new_line(e)
                    return []
                elif e.source_line_id and e.source_line_id == self.line.source_line_id:
                    # A partial replacement is still the same line; never alter
                    # text after readiness without invalidating the decision.
                    if self.line.ready_ms is not None and e.text != self.line.text.observed_original:
                        return self._pause("text_changed_after_ready")
                    self.line.text = self.interpreter.parse(e.text or "")
                elif e.source_line_id and self.line.source_line_id and e.source_line_id != self.line.source_line_id:
                    return self._pause("unexpected_line_boundary")
                elif e.text != self.line.text.observed_original:
                    return self._pause("unattributed_line_change")
                else:
                    return []
        elif e.kind is EK.LINE_UPDATED:
            assert self.line is not None
            if self.line.ready_ms is not None:
                return self._pause("text_changed_after_ready")
            self.line.text = self.interpreter.parse(e.text or "")
        elif e.kind is EK.LINE_READY:
            assert self.line is not None
            if self.line.text.content_kind is ContentKind.NON_DIALOGUE:
                return self._pause("no_confirmed_dialogue_event")
            if self.line.ready_ms is None:
                self.line.ready_ms = e.at_ms
                self.state = S.RESOLVE_PRESENTATION
        elif e.kind is EK.LINE_INVALIDATED:
            return self._pause("line_invalidated")
        elif e.kind is EK.VOICE_PRESENCE:
            assert self.line is not None
            if e.voice is None or e.evidence is Evidence.NONE:
                return []
            # Heuristic absence must never replace a stronger confirmed presence.
            if self.line.voice is VoicePresence.PRESENT and e.voice is VoicePresence.ABSENT:
                return []
            if self.line.voice is VoicePresence.ABSENT and self.line.evidence is Evidence.METADATA and e.evidence is Evidence.HEURISTIC:
                return []
            self.line.voice = e.voice
            self.line.evidence = e.evidence
            if e.evidence is Evidence.METADATA:
                self.line.native_asset_id = e.native_asset_id if e.voice is VoicePresence.PRESENT else None
            if (e.voice is VoicePresence.PRESENT and self.line.presentation is Presentation.TTS
                and self.line.text.audio_route is AudioRoute.AUTO):
                effects: list[Effect] = []
                if self.line.tts_job_started and not self.line.tts_canceled and not self.line.tts_finished:
                    self.line.tts_canceled = True
                    effects.append(self._effect(FX.CANCEL_TTS, "native_voice_metadata_arrived"))
                self.line.presentation = Presentation.NATIVE
                self.line.request_pending = False
                self.state = S.WAIT_NATIVE_END
                return effects
        elif e.kind is EK.NATIVE_STARTED:
            assert self.line is not None
            if self.line.sent:
                # Hardware input was irreversible. No post hoc "yield" can
                # protect the already advanced line: surface the race.
                return self._pause("native_started_after_advance")
            # Audio must be positively attributed to the current line.
            if not self.capture_ok or e.evidence not in (Evidence.AUDIO_CONFIRMED, Evidence.METADATA) or not e.native_utterance_id:
                return []
            self.line.voice = VoicePresence.PRESENT
            self.line.evidence = e.evidence
            self.line.playback = Playback.ACTIVE
            self.line.native_started_at_ms = e.at_ms
            self.line.utterance_id = e.native_utterance_id
            effects: list[Effect] = []
            if self.line.text.audio_route in (AudioRoute.AUTO,AudioRoute.NATIVE):
                if self.line.tts_job_started and not self.line.tts_canceled and not self.line.tts_finished:
                    self.line.tts_canceled = True
                    effects.append(self._effect(FX.CANCEL_TTS, "late_native_onset"))
                self.line.presentation = Presentation.NATIVE
            # Explicit TTS never loses its playback choice to native metadata.
            # An attributed, active native clip still blocks advancing over it.
            self.line.request_pending = False
            self.state = S.WAIT_NATIVE_END
            return effects
        elif e.kind is EK.NATIVE_COMPLETED:
            assert self.line is not None
            if self.line.sent:
                return []
            if (not self.capture_ok or self.line.playback is not Playback.ACTIVE or
                not e.native_utterance_id or e.native_utterance_id != self.line.utterance_id or
                e.evidence not in (Evidence.AUDIO_CONFIRMED, Evidence.METADATA)):
                return []
            self.line.playback = Playback.COMPLETE
            self.line.native_completed_at_ms = e.at_ms
            if self.line.text.audio_route in (AudioRoute.AUTO,AudioRoute.NATIVE):
                self.line.presentation = Presentation.NATIVE
        elif e.kind is EK.TTS_FINISHED:
            assert self.line is not None
            if not self.line.tts_job_started or self.line.tts_canceled or self.line.presentation is not Presentation.TTS:
                return []
            if e.tts_success is True:
                self.line.tts_finished = True
                self.line.tts_ended_at_ms = e.at_ms
            else:
                # A requested voice that failed synthesis/playback is not
                # evidence that silent viewing is an authorized substitute.
                return self._pause("tts_failed")
        elif e.kind is not EK.TIMER:
            return []
        return self._evaluate(e.at_ms)

    def _voice_can_resolve(self, at_ms: int) -> bool:
        assert self.line is not None
        if self.line.voice is VoicePresence.PRESENT:
            return True
        if self.line.voice is not VoicePresence.ABSENT:
            return False
        if self.line.evidence not in (Evidence.METADATA, Evidence.HEURISTIC):
            return False
        if self.line.evidence is Evidence.HEURISTIC:
            # Capture health and grace window are necessary but not sufficient:
            # external detection must have positively reported heuristic absence.
            return (self.settings.allow_heuristic_absence and self.capture_ok and
                    at_ms >= self.line.first_seen_ms + self.settings.grace_ms)
        return True

    def _dwell(self) -> int:
        assert self.line is not None
        line = self.line.text
        if line.content_kind is ContentKind.SYMBOLIC:
            if "…" in line.body or "・" in line.body:
                return (self.settings.symbolic_long_ms if line.body.count("…") >= 4
                        else self.settings.symbolic_short_ms)
            return self.settings.symbolic_other_ms
        return max(self.settings.silent_min_ms,
                   self.settings.silent_base_ms + round(1000 * line.effective_chars / self.settings.reading_chars_per_sec))

    def _requires_capture(self) -> bool:
        return bool(self.line and self.line.text.audio_route in (AudioRoute.AUTO,AudioRoute.NATIVE))

    def _eligible_for_request(self) -> bool:
        l = self.line
        return bool(l and self.autoplay_authorized and self.focused and
                    (self.capture_ok or not self._requires_capture()) and
                    l.ready_ms is not None and not l.sent and not l.request_pending and
                    l.presentation is not Presentation.UNRESOLVED and
                    l.playback is not Playback.ACTIVE and
                    not (l.tts_job_started and not (l.tts_finished or l.tts_canceled)) and
                    self.state not in (S.PAUSED, S.WAIT_NEXT_HOOK))

    def _evaluate(self, at_ms: int) -> list[Effect]:
        l = self.line
        if l is None or self.state in (S.PAUSED, S.WAIT_NEXT_HOOK):
            return []
        if l.ready_ms is None:
            self.state = S.WAIT_TEXT_READY
            return []
        if l.text.content_kind is ContentKind.NON_DIALOGUE:
            return self._pause("non_dialogue")
        route = l.text.audio_route
        if l.text.tts_route is TTSPolicy.ERROR:
            return self._pause("tts_configuration_error")

        # An attributed clip currently playing is an independent safety blocker,
        # not permission to override an explicit TTS/SILENT preference.
        if l.playback is Playback.ACTIVE:
            self.state = S.WAIT_NATIVE_END
            if (self.autoplay_authorized and self.focused and l.native_started_at_ms is not None and
                at_ms >= l.native_started_at_ms + self.settings.native_end_timeout_ms):
                return self._pause("native_end_unconfirmed")
            return []

        if route in (AudioRoute.AUTO, AudioRoute.NATIVE):
            # Native routing needs capture and attributed evidence. A transient
            # capture failure does not destroy the text-only monitor.
            if not self.focused or not self.capture_ok:
                return []
            if l.voice is VoicePresence.PRESENT:
                l.presentation = Presentation.NATIVE
                if l.playback is not Playback.COMPLETE:
                    self.state = S.WAIT_NATIVE_END
                    if (self.autoplay_authorized and
                        at_ms >= (l.observation_since_ms or at_ms) + self.settings.native_start_timeout_ms):
                        return self._pause("native_start_or_end_unconfirmed")
                    return []
            elif route is AudioRoute.NATIVE or not self._voice_can_resolve(at_ms):
                self.state = S.RESOLVE_PRESENTATION
                if (self.autoplay_authorized and
                    at_ms >= (l.observation_since_ms or at_ms) + self.settings.unresolved_timeout_ms):
                    return self._pause("native_voice_unresolved")
                return []
        # Explicit TTS/SILENT do not require ASR or Loopback just to present
        # and advance. AUTO may use TTS only after absence is evidenced.
        if l.presentation is not Presentation.NATIVE:
            if l.presentation not in (Presentation.TTS,Presentation.SILENT):
                voice=l.text.voice_id
                if route in (AudioRoute.TTS,AudioRoute.AUTO) and voice and l.text.content_kind is ContentKind.SPEAKABLE:
                    l.presentation=Presentation.TTS
                    l.tts_job_started=True
                    l.tts_started_at_ms=at_ms
                    self.state=S.WAIT_TTS_END
                    return [self._effect(FX.START_TTS,voice=voice)]
                if (route is AudioRoute.TTS or
                    (route is AudioRoute.AUTO and self.settings.tts_required)) and l.text.content_kind is ContentKind.SPEAKABLE:
                    return self._pause("tts_required_unassigned")
                l.presentation=Presentation.SILENT
                l.silent_started_at_ms=l.ready_ms
            if l.presentation is Presentation.TTS:
                if not l.tts_finished:
                    self.state=S.WAIT_TTS_END
                    return []
            else:
                # The original metadata grace is relevant only in AUTO mode;
                # explicit silent viewing is timed from text readiness.
                grace_until=l.first_seen_ms+self.settings.grace_ms if route is AudioRoute.AUTO else l.ready_ms
                dwell_until=l.ready_ms+self._dwell()
                if at_ms < max(grace_until,dwell_until):
                    self.state=S.WAIT_SILENT_DWELL
                    return []
        if not self._eligible_for_request():
            return []
        l.request_pending=True
        l.presentation_complete_at_ms=at_ms
        self.state=S.READY_TO_ADVANCE
        return [self._effect(FX.REQUEST_ADVANCE,"presentation_complete")]

    def confirm_advance(self, *, line_epoch: int, cancel_token: int,
                        fresh_focused: bool, fresh_capture_ok: bool,
                        fresh_native_voice_active: bool = True,
                        fresh_tts_playing: bool = True,
                        fresh_target_window_matches: bool = False,
                        fresh_input_available: bool = False,
                        fresh_manual_override: bool = True,
                        at_ms: int = -1) -> bool:
        """Called synchronously by the sole input executor after fresh observation.

        Marks as sent *before* hardware I/O; failures must pause, not retry.
        """
        l = self.line
        if (l is None or l.epoch != line_epoch or l.token != cancel_token or
            self.state is not S.READY_TO_ADVANCE or not l.request_pending or
            not self.autoplay_authorized or not self.focused or
            (self._requires_capture() and (not self.capture_ok or not fresh_capture_ok)) or
            not fresh_focused or
            fresh_native_voice_active or fresh_tts_playing or
            not fresh_target_window_matches or
            not fresh_input_available or fresh_manual_override or
            at_ms < self._last_at_ms or
            l.sent or l.playback is Playback.ACTIVE):
            return False
        l.sent = True
        l.sent_at_ms = at_ms
        l.request_pending = False
        self.state = S.WAIT_NEXT_HOOK
        self._last_at_ms = at_ms
        return True

    def input_failed(self) -> list[Effect]:
        return self._pause("input_failed_no_retry")

    def next_hook_timed_out(self, now_ms: int) -> list[Effect]:
        if (self.state is S.WAIT_NEXT_HOOK and self.line is not None and
            self.line.sent_at_ms is not None and
            now_ms - self.line.sent_at_ms >= self.settings.next_hook_timeout_ms):
            return self._pause("next_hook_timeout_no_retry")
        return []