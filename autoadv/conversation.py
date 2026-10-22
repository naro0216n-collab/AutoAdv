"""Single owner of line identity, presentation completion and advance authority."""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from collections.abc import Callable, Sequence
from .hook_protocol import HookEvent
from .model import AdvanceMode, Profile, Utterance, extract_source_fields, interpret


@dataclass(frozen=True)
class Target:
    pid: int
    hwnd: int
    hook_pid: int | None = None
    audio_pid: int | None = None

    def __post_init__(self):
        if self.pid <= 0 or self.hwnd <= 0 or (self.hook_pid is not None and self.hook_pid <= 0) or (self.audio_pid is not None and self.audio_pid <= 0):
            raise ValueError('target must have positive PID and HWND')


@dataclass(frozen=True)
class Token:
    session: int
    line: int


class Controller:
    """Callbacks from devices never send input; only try_advance may do that."""
    def __init__(self, input_send: Callable[[Target, int], None],
                 preflight: Callable[[Target], bool]):
        self.input_send = input_send
        self.preflight = preflight
        self.session = 0
        self.target: Target | None = None
        self.profile: Profile | None = None
        self.pending_profile: Profile | None = None
        self.source_generation = ''
        self.connection_generation = 0
        self.highest_sequence = 0
        self.utterance: Utterance | None = None
        self.authorized = False
        self.completed = False
        self.presentation_reason: str | None = None
        self.text_elapsed = False
        self.tts_finished = False
        self.native_playing = False
        self.fallback_elapsed = False
        self.sent = False
        self.reason = 'not_monitoring'
        self.pause_reason: str | None = None
        self.line_no = 0
        self.awaiting_next = False
        self.ocr_blank = False
        self.active_body_source: str | None = None
        self.external_transition_ns = 0

    def begin(self, profile: Profile, target: Target, host_generation: str, connection_generation: int,
              available_sources: tuple[str, ...] = ()) -> int:
        profile.validate()
        if profile.source == 'hook' and not profile.body_sources:
            raise ValueError('select at least one body source to monitor')
        if not host_generation or connection_generation <= 0:
            raise ValueError('Host connection must be identified')
        self.close('session_changed')
        self.profile, self.target = deepcopy(profile), target
        self.pending_profile = None
        self.source_generation, self.connection_generation = host_generation, connection_generation
        self.active_body_source = next(
            (identity for identity in profile.body_sources if identity in available_sources), None)
        self.pause_reason = None if self.authorized else 'advance_not_authorized'
        self.reason = 'presenting' if self.authorized else 'advance_not_authorized'
        return self.session

    def close(self, reason: str = 'stopped') -> None:
        """End monitoring without rewriting the user's AUTO ON/OFF choice."""
        self.session += 1
        self.profile = None
        self.pending_profile = None
        self.target = None
        self.source_generation = ''
        self.connection_generation = 0
        self.highest_sequence = 0
        self.utterance = None
        self.line_no = 0
        self.completed = self.sent = False
        self.awaiting_next = False
        self.ocr_blank = False
        self.active_body_source = None
        self.external_transition_ns = 0
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = self.native_playing = self.fallback_elapsed = False
        self.pause_reason = reason if self.authorized else 'advance_not_authorized'
        self.reason = self.pause_reason

    def stage_profile(self, profile: Profile) -> None:
        """Apply non-capture edits at the next utterance boundary."""
        profile.validate()
        if self.profile is None:
            raise ValueError('monitoring is not active')
        if (profile.id != self.profile.id or profile.source != self.profile.source or
                profile.hooks != self.profile.hooks or
                profile.body_sources != self.profile.body_sources or
                profile.speaker_sources != self.profile.speaker_sources or
                profile.source_rules != self.profile.source_rules or
                (profile.source == 'ocr' and profile.ocr != self.profile.ocr)):
            raise ValueError('capture ownership changes require a new monitoring session')
        staged = deepcopy(profile)
        if self.utterance is None:
            self.profile = staged
            self.pending_profile = None
        else:
            self.pending_profile = staged

    def observe(self, *, generation: str, connection: int, sequence: int,
                source: object, text: str, pid: int, received_ns: int = 0) -> Token | None:
        if not self.profile or not self.target or self.profile.source != 'hook':
            return None
        if pid != (self.target.hook_pid or self.target.pid):
            return None
        if generation != self.source_generation or connection != self.connection_generation:
            self.pause('host_changed')
            return None
        matched = [item for item in self.profile.hooks if item.matches(source)]
        if len(matched) != 1 or sequence <= self.highest_sequence:
            return None
        identity = matched[0].identity
        if identity not in self.profile.body_sources or not text.strip():
            self.highest_sequence = max(self.highest_sequence, sequence)
            return None
        fields = extract_source_fields(text, self.profile, identity)
        if not fields.body:
            self.highest_sequence = max(self.highest_sequence, sequence)
            return None
        if self.active_body_source is None:
            self.active_body_source = identity
        if identity != self.active_body_source:
            self.highest_sequence = max(self.highest_sequence, sequence)
            return None
        self.highest_sequence = sequence
        if self.profile.speaker_sources:
            speaker = fields.speaker if identity == self.profile.speaker_sources[0] else ''
        else:
            speaker = fields.speaker
        return self._accept_line(sequence=sequence, raw=text, body=fields.body, speaker=speaker,
                                 origin=identity, verified_boundary=False, received_ns=received_ns)

    def observe_group(self, events: Sequence[HookEvent]) -> Token | None:
        """Assemble one dialogue from independently extracted source fields.

        Luna-selected outputs are correlated only inside this bounded group.  A
        body and speaker may come from the same source or different sources;
        the first usable configured provider for each field wins.
        """
        if not self.profile or self.profile.source != 'hook' or not events:
            return None
        ordered = sorted(events, key=lambda event: event.sequence)
        if any(event.kind != 'output' or event.source is None for event in ordered):
            return None
        if any(event.host_generation != self.source_generation or
               event.connection_generation != self.connection_generation for event in ordered):
            self.pause('host_changed')
            return None
        if not self.target or any(event.process_id != (self.target.hook_pid or self.target.pid) for event in ordered):
            return None
        if ordered[0].sequence <= self.highest_sequence:
            return None

        selected = {hook.identity for hook in self.profile.hooks}
        matching = [event for event in ordered
                    if event.source.identity in selected and event.text.strip()]
        if not matching:
            self.highest_sequence = ordered[-1].sequence
            return None

        extracted: dict[int, object] = {
            id(event): extract_source_fields(event.text, self.profile, event.source.identity)
            for event in matching
        }
        chosen = self.active_body_source
        if chosen is None:
            chosen = next((identity for identity in self.profile.body_sources
                           if any(event.source.identity == identity and extracted[id(event)].body
                                  for event in matching)), None)
        if chosen is None:
            self.highest_sequence = ordered[-1].sequence
            return None
        primary = [event for event in matching
                   if event.source.identity == chosen and extracted[id(event)].body]
        if not primary:
            self.highest_sequence = ordered[-1].sequence
            return None
        if len(primary) != 1:
            self.highest_sequence = ordered[-1].sequence
            self.pause('ambiguous_multihook_lines')
            return None
        self.active_body_source = chosen
        body_event = primary[0]
        body_fields = extracted[id(body_event)]

        speaker = ''
        if self.profile.speaker_sources:
            for identity in self.profile.speaker_sources:
                candidates = [event for event in matching
                              if event.source.identity == identity and extracted[id(event)].speaker]
                if len(candidates) > 1:
                    self.highest_sequence = ordered[-1].sequence
                    self.pause('ambiguous_multihook_speakers')
                    return None
                if candidates:
                    speaker = extracted[id(candidates[0])].speaker
                    break
        else:
            speaker = body_fields.speaker

        result = self._accept_line(
            sequence=body_event.sequence, raw=body_event.text, body=body_fields.body,
            speaker=speaker, origin=chosen, verified_boundary=False,
            received_ns=body_event.received_ns)
        self.highest_sequence = max(self.highest_sequence, ordered[-1].sequence)
        return result

    def observe_ocr(self, *, sequence: int, text: str, pid: int, session: int) -> Token | None:
        if (not self.profile or self.profile.source != 'ocr' or not self.target
                or pid != self.target.pid or session != self.session
                or sequence <= self.highest_sequence):
            return None
        self.highest_sequence = sequence
        if not text.strip():
            self.ocr_blank = True
            return None
        if self.utterance and not self.ocr_blank and not self.sent:
            if text != self.utterance.raw:
                self.pause('ocr_unverified_change')
            return None
        if self.utterance and text == self.utterance.raw and not self.ocr_blank:
            return None
        had_blank = self.ocr_blank
        self.ocr_blank = False
        fields = extract_source_fields(text, self.profile, 'ocr')
        if not fields.body:
            return None
        return self._accept_line(sequence=sequence, raw=text, body=fields.body,
                                 speaker=fields.speaker, origin='ocr',
                                 verified_boundary=had_blank)

    def _accept_line(self, *, sequence: int, raw: str, body: str, speaker: str, origin: str,
                     verified_boundary: bool = False, received_ns: int = 0) -> Token | None:
        old = self.utterance
        external_transition = bool(old and not self.sent and raw != old.raw and received_ns > 0)
        if old and not self.sent and old.raw == raw and not verified_boundary:
            return None
        if self.pending_profile is not None:
            self.profile = self.pending_profile
            self.pending_profile = None
        if old and self.sent and not verified_boundary and raw == old.raw:
            if self.profile.repeat_rule != 'each_after_input':
                self.pause('ambiguous_identical_line')
                return None
        self.external_transition_ns = received_ns if external_transition else 0
        self.line_no += 1
        self.utterance = interpret(
            raw, self.profile, session=self.session, line=self.line_no, source=origin,
            sequence=sequence, speaker_override=speaker, body_override=body)
        self.completed = False
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = self.native_playing = self.fallback_elapsed = False
        self.sent = False
        self.awaiting_next = False
        if self.authorized:
            self.pause_reason = None
            self.reason = 'presenting'
        else:
            self.pause_reason = 'advance_not_authorized'
            self.reason = 'advance_not_authorized'
        return Token(self.session, self.line_no)

    def take_external_transition(self, token: Token) -> int:
        """Return the HOOK arrival time that replaced an unsent line once.

        The Controller intentionally does not infer whether the game advanced
        because of a click, key, script, or the game's own auto mode.
        """
        if token != self.token or self.external_transition_ns <= 0:
            return 0
        value = self.external_transition_ns
        self.external_transition_ns = 0
        return value

    def presentation_event(self, token: Token, event: str, *,
                           success: bool = True) -> bool:
        """All presentation evidence is owned by this Controller, never by GUI callbacks.

        A completion event returns True only on the first transition to ready.
        Events from another line/session or after input are inert.
        """
        if token != self.token or self.sent or self.completed or not self.utterance:
            return False
        u = self.utterance
        if event == 'tts_finished':
            if not u.use_tts:
                return False
            if not success:
                self.pause('tts_playback_failed')
                return False
            self.tts_finished = True
            if u.advance_mode == AdvanceMode.AUDIO or self.text_elapsed:
                return self._presentation_done('tts' if u.advance_mode == AdvanceMode.AUDIO else 'tts_and_text')
            return False
        if event == 'text_elapsed':
            if u.advance_mode != AdvanceMode.TEXT:
                return False
            self.text_elapsed = True
            if not u.use_tts:
                return self._presentation_done('text')
            if self.tts_finished:
                return self._presentation_done('tts_and_text')
            return False
        if event == 'native_started':
            if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return False
            self.native_playing = True
            return False
        if event == 'native_quiet':
            if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return False
            self.native_playing = False
            if self.fallback_elapsed and u.uncertain_text_fallback:
                return self._presentation_done('uncertain_text_fallback')
            return False
        if event == 'native_evidence_stale':
            # Expired positive evidence is not a VAD-observed end, and cannot
            # justify a native completion. It only releases an uncertain hold.
            if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return False
            self.native_playing = False
            if self.fallback_elapsed:
                if u.uncertain_text_fallback:
                    return self._presentation_done('uncertain_text_fallback')
                self.pause('native_inconclusive_hold')
            return False
        if event == 'native_finished':
            if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return False
            return self._presentation_done('native')
        if event == 'fallback_elapsed':
            if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return False
            self.fallback_elapsed = True
            if not self.native_playing:
                if u.uncertain_text_fallback:
                    return self._presentation_done('uncertain_text_fallback')
                # No automatic input: give the user a useful stopped state
                # instead of an indefinitely ambiguous "presenting" state.
                if self.authorized:
                    self.pause('native_inconclusive_hold')
            return False
        raise ValueError(f'unknown presentation event: {event}')

    def _presentation_done(self, reason: str) -> bool:
        self.completed = True
        self.presentation_reason = reason
        self.reason = ('ready' if self.authorized and self.pause_reason is None
                       else (self.pause_reason or 'awaiting_permission'))
        return True

    @property
    def token(self) -> Token | None:
        if self.utterance is None:
            return None
        return Token(self.session, self.utterance.line)

    def permit(self, allowed: bool) -> None:
        """Change only the user's automatic-advance authorization.

        Runtime uncertainty is represented by ``pause_reason`` and must not
        silently turn the user's mode switch off.
        """
        if allowed:
            self.authorized = True
            # Re-enabling is an explicit user operation and may retry a
            # previously held line. With no active monitor it is simply an ON
            # preference waiting for a session; no input can be emitted.
            self.pause_reason = None if self.profile is not None else 'not_monitoring'
        else:
            self.authorized = False
            self.pause_reason = 'advance_not_authorized'
        if not self.authorized:
            self.reason = 'advance_not_authorized'
        elif self.pause_reason:
            self.reason = self.pause_reason
        else:
            self.reason = 'ready' if self.completed else 'presenting'

    def pause(self, reason: str) -> None:
        """Hold automatic input without changing the user's mode choice."""
        self.pause_reason = reason
        self.reason = reason

    def expire_next_line(self, token: Token) -> bool:
        """Hold further input after an unanswered advance.

        The user's auto-mode choice remains ON; a later valid line may resume
        it without fabricating another input for the timed-out utterance.
        """
        if token != self.token or not self.sent or not self.awaiting_next:
            return False
        self.awaiting_next = False
        self.pause('next_line_timeout')
        return True

    def try_advance(self) -> bool:
        if not (self.authorized and self.completed and self.utterance and self.target and not self.sent
                and self.pause_reason is None):
            return False
        if not self.preflight(self.target):
            self.pause('target_not_foreground')
            return False
        # Reserve the line before external I/O. An exception never causes an unsafe retry.
        self.sent = True
        self.completed = False
        try:
            self.input_send(self.target, self.profile.input_key)  # type: ignore[union-attr]
        except Exception:
            self.pause('input_failure_no_retry')
            return False
        self.awaiting_next = True
        self.reason = 'awaiting_next_line'
        return True
