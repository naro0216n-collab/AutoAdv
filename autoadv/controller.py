"""Deterministic, fail-closed, single-owner visual-novel advancement state machine.

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
    EventKind as EK, Evidence, Playback, Presentation, ScreenState,
    TTSPolicy, VoicePresence,
)
from .text import LineText, normalize


@dataclass(frozen=True)
class Settings:
    autoplay: bool = True
    grace_ms: int = 500
    silent_base_ms: int = 700
    silent_min_ms: int = 1000
    reading_chars_per_sec: float = 12.0
    symbolic_short_ms: int = 1500
    symbolic_long_ms: int = 2500
    symbolic_other_ms: int = 1000
    empty_ms: int = 1000
    tts_required: bool = False
    # Until a game profile is independently calibrated, heuristic absence is
    # never sufficient to authorize silent progression.
    allow_heuristic_absence: bool = False
    name_readings: tuple[tuple[str, str], ...] = ()
    transition_timeout_ms: int = 10000
    unresolved_timeout_ms: int = 6000
    native_start_timeout_ms: int = 30000
    native_end_timeout_ms: int = 180000

    def __post_init__(self) -> None:
        if self.grace_ms < 0 or self.silent_min_ms < 0 or self.silent_base_ms < 0:
            raise ValueError("durations must be nonnegative")
        if any(x <= 0 for x in (self.transition_timeout_ms, self.unresolved_timeout_ms,
                                 self.native_start_timeout_ms, self.native_end_timeout_ms)):
            raise ValueError("timeout must be positive")
        if self.reading_chars_per_sec <= 0:
            raise ValueError("reading_chars_per_sec must be positive")


@dataclass
class Line:
    epoch: int
    token: int
    text: LineText
    source_line_id: Optional[str]
    first_seen_ms: int
    source_seq: int
    ready_ms: Optional[int] = None
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
    native_started_at_ms: Optional[int] = None


class Controller:
    """A session has one controller and one owner of line_epoch.

    Initial screen and audio health are UNKNOWN. They must be confirmed by
    independent adapters before an auto-advance request can be emitted.
    """

    def __init__(self, settings: Settings | None = None, session_id: str | None = None):
        self.settings = settings or Settings()
        self.session_id = session_id or str(uuid.uuid4())
        self.state = S.WAIT_LINE
        self.screen = ScreenState.UNKNOWN
        self.focused = False
        self.capture_ok = False
        self.line: Optional[Line] = None
        self._epoch = 0
        self._token = 0
        self._last_source_seq = -1
        self._last_at_ms = -1
        self.pause_reason: Optional[str] = None
        self.tts_policy = TTSPolicy.UNASSIGNED
        self.voice_id: Optional[str] = None
        self.default_voice_id: Optional[str] = None
        self._last_ack_id: Optional[str] = None

    def configure_tts(self, policy: TTSPolicy, *, selected_voice: str | None = None,
                      default_voice: str | None = None) -> None:
        if self.line is not None and self.state not in (S.WAIT_LINE, S.PAUSED):
            raise RuntimeError("change TTS policy only between lines or while paused")
        self.tts_policy = policy
        self.voice_id = selected_voice
        self.default_voice_id = default_voice

    def _effect(self, kind: FX, reason: str = "", voice: str | None = None) -> Effect:
        assert self.line is not None
        return Effect(kind, self.session_id, self.line.epoch, self.line.token,
                      text=self.line.text.spoken, voice_id=voice, reason=reason)

    def _pause(self, reason: str) -> list[Effect]:
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
        self.line = Line(self._epoch, self._token,
                         normalize(e.text or "", empty_confirmed=e.empty_confirmed, names=dict(self.settings.name_readings)),
                         e.source_line_id, e.at_ms, e.source_seq or 0)
        self.state = S.WAIT_TEXT_READY
        self.pause_reason = None
        if e.text_ready:
            self.line.ready_ms = e.at_ms
            self.state = S.RESOLVE_PRESENTATION

    def _check_event(self, e: Envelope) -> bool:
        if e.session_id != self.session_id or e.at_ms < self._last_at_ms:
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
        self._last_at_ms = e.at_ms
        if e.kind is EK.MANUAL_OVERRIDE:
            effects = self._pause("manual_override")
            self._token += 1
            if self.line is not None:
                # CANCEL_TTS was emitted with the OLD token. After this point,
                # callbacks from the invalidated epoch cannot match any line.
                self.line.token = self._token
            return effects
        if e.kind is EK.SCREEN_CHANGED:
            if e.screen is None:
                return self._pause("invalid_screen_observation")
            self.screen = e.screen
            if self.screen is not ScreenState.DIALOGUE and self.line is not None:
                # After an input has been emitted, a verified scene TRANSITION
                # is normal; never send again before an independently observed
                # boundary ACK. Choices/menus/maps still require user action.
                if not (self.state is S.WAIT_TRANSITION_ACK and
                        self.screen is ScreenState.TRANSITION):
                    return self._pause(f"screen_{self.screen.name.lower()}")
        elif e.kind is EK.FOCUS_CHANGED:
            self.focused = e.focused is True
            if not self.focused and self.line is not None:
                return self._pause("window_unfocused")
        elif e.kind is EK.CAPTURE_HEALTH:
            self.capture_ok = e.capture_ok is True
            if not self.capture_ok and self.line is not None:
                return self._pause("audio_capture_unhealthy")
        elif e.kind is EK.LINE_CANDIDATE:
            if e.source_seq is None or e.source_seq <= self._last_source_seq:
                return []
            self._last_source_seq = e.source_seq
            if self.line is None and self.state is S.WAIT_LINE:
                self._new_line(e)
            elif self.line is not None:
                # A repeated origin text does NOT mean a second line.
                # Only a confirmed transition ACK may authorize a new epoch.
                if self.state is S.WAIT_TRANSITION_ACK:
                    if e.source_line_id and self.line.source_line_id and e.source_line_id != self.line.source_line_id:
                        self._new_line(e)
                    else:
                        return []
                elif e.source_line_id and e.source_line_id == self.line.source_line_id:
                    # A partial replacement is still the same line; never alter
                    # text after readiness without invalidating the decision.
                    if self.line.ready_ms is not None and e.text != self.line.text.raw:
                        return self._pause("text_changed_after_ready")
                    self.line.text = normalize(e.text or "", empty_confirmed=e.empty_confirmed, names=dict(self.settings.name_readings))
                elif e.source_line_id and self.line.source_line_id and e.source_line_id != self.line.source_line_id:
                    return self._pause("unexpected_line_boundary")
                elif e.text != self.line.text.raw:
                    return self._pause("unattributed_line_change")
                else:
                    return []
        elif e.kind is EK.TRANSITION_ACK:
            if self.line is None or self.state is not S.WAIT_TRANSITION_ACK:
                return []
            if e.line_epoch != self.line.epoch or e.cancel_token != self.line.token:
                return []
            if not e.transition_id or e.transition_id == self._last_ack_id:
                return self._pause("ambiguous_transition_ack")
            self._last_ack_id = e.transition_id
            self.line = None
            self.state = S.WAIT_LINE
            return []
        elif e.kind is EK.LINE_UPDATED:
            assert self.line is not None
            if self.line.ready_ms is not None:
                return self._pause("text_changed_after_ready")
            self.line.text = normalize(e.text or "", empty_confirmed=e.empty_confirmed, names=dict(self.settings.name_readings))
        elif e.kind is EK.LINE_READY:
            assert self.line is not None
            if self.line.text.kind is ContentKind.NON_DIALOGUE:
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
            if e.voice is VoicePresence.PRESENT and self.line.presentation is Presentation.TTS:
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
            if self.line.tts_job_started and not self.line.tts_canceled and not self.line.tts_finished:
                self.line.tts_canceled = True
                effects.append(self._effect(FX.CANCEL_TTS, "late_native_onset"))
            self.line.presentation = Presentation.NATIVE
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
            self.line.presentation = Presentation.NATIVE
        elif e.kind is EK.TTS_FINISHED:
            assert self.line is not None
            if not self.line.tts_job_started or self.line.tts_canceled or self.line.presentation is not Presentation.TTS:
                return []
            if e.tts_success is True:
                self.line.tts_finished = True
            else:
                if self.settings.tts_required or self.line.voice is not VoicePresence.ABSENT:
                    return self._pause("tts_failed")
                self.line.tts_canceled = True
                self.line.presentation = Presentation.SILENT
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
        if line.kind is ContentKind.EMPTY_EVENT:
            return self.settings.empty_ms
        if line.kind is ContentKind.SYMBOLIC:
            if "…" in line.raw or "・" in line.raw:
                return (self.settings.symbolic_long_ms if line.raw.count("…") >= 4
                        else self.settings.symbolic_short_ms)
            return self.settings.symbolic_other_ms
        return max(self.settings.silent_min_ms,
                   self.settings.silent_base_ms + round(1000 * line.effective_chars / self.settings.reading_chars_per_sec))

    def _eligible_for_request(self) -> bool:
        l = self.line
        return bool(l and self.settings.autoplay and self.focused and
                    self.screen is ScreenState.DIALOGUE and self.capture_ok and
                    l.ready_ms is not None and not l.sent and not l.request_pending and
                    l.presentation is not Presentation.UNRESOLVED and
                    l.playback is not Playback.ACTIVE and not (l.tts_job_started and not (l.tts_finished or l.tts_canceled)) and
                    self.state not in (S.PAUSED, S.WAIT_TRANSITION_ACK))

    def _evaluate(self, at_ms: int) -> list[Effect]:
        l = self.line
        if l is None or self.state in (S.PAUSED, S.WAIT_TRANSITION_ACK):
            return []
        if self.screen is not ScreenState.DIALOGUE or not self.focused or not self.capture_ok:
            if at_ms >= l.first_seen_ms + self.settings.unresolved_timeout_ms:
                return self._pause("observation_unavailable")
            return []
        if l.ready_ms is None:
            self.state = S.WAIT_TEXT_READY
            return []
        if l.text.kind is ContentKind.NON_DIALOGUE:
            return self._pause("non_dialogue")
        if l.playback is Playback.ACTIVE:
            l.presentation = Presentation.NATIVE
            self.state = S.WAIT_NATIVE_END
            if (l.native_started_at_ms is not None and
                at_ms >= l.native_started_at_ms + self.settings.native_end_timeout_ms):
                return self._pause("native_end_unconfirmed")
            return []
        if l.voice is VoicePresence.PRESENT:
            l.presentation = Presentation.NATIVE
            if l.playback is not Playback.COMPLETE:
                self.state = S.WAIT_NATIVE_END
                if at_ms >= l.first_seen_ms + self.settings.native_start_timeout_ms:
                    return self._pause("native_start_or_end_unconfirmed")
                return []
        elif not self._voice_can_resolve(at_ms):
            self.state = S.RESOLVE_PRESENTATION
            if at_ms >= l.first_seen_ms + self.settings.unresolved_timeout_ms:
                return self._pause("native_voice_unresolved")
            return []
        else:
            if l.presentation not in (Presentation.TTS, Presentation.SILENT):
                voice = self.voice_id if self.tts_policy is TTSPolicy.SELECTED else self.default_voice_id if self.tts_policy is TTSPolicy.DEFAULT else None
                if self.tts_policy is TTSPolicy.ERROR:
                    return self._pause("tts_configuration_error")
                if voice and l.text.kind is ContentKind.SPEAKABLE:
                    l.presentation = Presentation.TTS
                    l.tts_job_started = True
                    self.state = S.WAIT_TTS_END
                    return [self._effect(FX.START_TTS, voice=voice)]
                if self.settings.tts_required and l.text.kind is ContentKind.SPEAKABLE:
                    return self._pause("tts_required_unassigned")
                l.presentation = Presentation.SILENT
            if l.presentation is Presentation.TTS:
                if not l.tts_finished:
                    self.state = S.WAIT_TTS_END
                    return []
            else:
                # Onset grace and reading time run concurrently.
                grace_until = l.first_seen_ms + self.settings.grace_ms
                dwell_until = l.ready_ms + self._dwell()
                if at_ms < max(grace_until, dwell_until):
                    self.state = S.WAIT_SILENT_DWELL
                    return []
        if not self._eligible_for_request():
            return []
        l.request_pending = True
        self.state = S.READY_TO_ADVANCE
        return [self._effect(FX.REQUEST_ADVANCE, "presentation_complete")]

    def confirm_advance(self, *, line_epoch: int, cancel_token: int,
                        fresh_screen: ScreenState, fresh_focused: bool,
                        fresh_text_ready: bool, fresh_capture_ok: bool,
                        fresh_native_voice_active: bool = True,
                        fresh_tts_playing: bool = True,
                        fresh_line_matches: bool = False,
                        fresh_target_window_matches: bool = False,
                        fresh_input_available: bool = False,
                        fresh_manual_override: bool = True,
                        fresh_transition_pending: bool = True,
                        at_ms: int = -1) -> bool:
        """Called synchronously by the sole input executor after fresh observation.

        Marks as sent *before* hardware I/O; failures must pause, not retry.
        """
        l = self.line
        if (l is None or l.epoch != line_epoch or l.token != cancel_token or
            self.state is not S.READY_TO_ADVANCE or not l.request_pending or
            self.screen is not ScreenState.DIALOGUE or not self.focused or not self.capture_ok or
            not fresh_focused or fresh_screen is not ScreenState.DIALOGUE or
            not fresh_text_ready or not fresh_capture_ok or
            fresh_native_voice_active or fresh_tts_playing or
            not fresh_line_matches or not fresh_target_window_matches or
            not fresh_input_available or fresh_manual_override or
            fresh_transition_pending or at_ms < self._last_at_ms or
            l.sent or l.playback is Playback.ACTIVE):
            return False
        l.sent = True
        l.sent_at_ms = at_ms
        l.request_pending = False
        self.state = S.WAIT_TRANSITION_ACK
        self._last_at_ms = at_ms
        return True

    def input_failed(self) -> list[Effect]:
        return self._pause("input_failed_no_retry")

    def transition_timed_out(self, now_ms: int) -> list[Effect]:
        if (self.state is S.WAIT_TRANSITION_ACK and self.line is not None and
            self.line.sent_at_ms is not None and
            now_ms - self.line.sent_at_ms >= self.settings.transition_timeout_ms):
            return self._pause("transition_ack_timeout_no_retry")
        return []