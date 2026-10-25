"""Single owner of line identity, presentation completion and advance authority."""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from collections.abc import Callable, Sequence
from .hook_protocol import HookEvent
from .g2p import G2PBackend
from .utterance_progress import UtteranceProgress
from .model import (AdvanceMode, Profile, Utterance, assemble_dialogue_fields,
                    configured_speaker_names, extract_source_fields, interpret,
                    resolve_speaker_enclosed_body)


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
                 target_ready: Callable[[Target], bool], g2p: G2PBackend):
        self.input_send = input_send
        self.target_ready = target_ready
        self.g2p = g2p
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
        self.native_progress = UtteranceProgress()
        self.audio_deadline_elapsed = False
        self.sent = False
        self.hold_reason: str | None = None
        self.line_no = 0
        self.awaiting_next = False
        self.ocr_blank = False
        self.external_transition_ns = 0

    def begin(self, profile: Profile, target: Target, host_generation: str, connection_generation: int) -> int:
        profile.validate()
        if profile.source == 'hook' and not profile.body_source:
            raise ValueError('select at least one body source to monitor')
        if not host_generation or connection_generation <= 0:
            raise ValueError('Host connection must be identified')
        self.close('session_changed')
        self.profile, self.target = deepcopy(profile), target
        self.pending_profile = None
        self.source_generation, self.connection_generation = host_generation, connection_generation
        self.hold_reason = None
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
        self.external_transition_ns = 0
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = False
        self.native_progress = UtteranceProgress()
        self.audio_deadline_elapsed = False
        self.hold_reason = reason

    def stage_profile(self, profile: Profile) -> None:
        """Apply non-capture edits at the next utterance boundary."""
        profile.validate()
        if self.profile is None:
            raise ValueError('monitoring is not active')
        if (profile.id != self.profile.id or profile.source != self.profile.source or
                profile.hooks != self.profile.hooks or
                profile.body_source != self.profile.body_source or
                profile.speaker_source != self.profile.speaker_source or
                profile.source_rules != self.profile.source_rules or
                profile.unwrap_speaker_enclosed_body != self.profile.unwrap_speaker_enclosed_body or
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
        if identity != self.profile.body_source or not text.strip():
            self.highest_sequence = max(self.highest_sequence, sequence)
            return None
        fields = extract_source_fields(text, self.profile, identity)
        if not fields.body:
            self.highest_sequence = max(self.highest_sequence, sequence)
            return None
        self.highest_sequence = sequence
        speaker = (fields.speaker if self.profile.speaker_source in ('', identity) else '')
        if self.profile.unwrap_speaker_enclosed_body:
            speaker, body = resolve_speaker_enclosed_body(
                fields.body, speaker, configured_speaker_names(self.profile))
        else:
            body = fields.body
        return self._accept_line(sequence=sequence, raw=text, body=body, speaker=speaker,
                                 origin=identity, verified_boundary=False, received_ns=received_ns)

    def observe_group(self, events: Sequence[HookEvent]) -> Token | None:
        """Assemble one dialogue from independently extracted source fields.

        Luna-selected outputs are correlated only inside this bounded group.  A
        body and speaker may come from the same source or different explicitly
        configured sources; no provider-priority fallback exists.
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

        extracted = [
            (event.source.identity,
             extract_source_fields(event.text, self.profile, event.source.identity))
            for event in matching
        ]
        assembly = assemble_dialogue_fields(
            extracted, body_source=self.profile.body_source,
            speaker_source=self.profile.speaker_source)
        if assembly.problem:
            self.highest_sequence = ordered[-1].sequence
            if assembly.problem == 'ambiguous_body':
                self.pause('ambiguous_multihook_lines')
            elif assembly.problem == 'ambiguous_speaker':
                self.pause('ambiguous_multihook_speakers')
            return None
        assert assembly.body_index is not None
        body_event = matching[assembly.body_index]

        speaker = assembly.speaker
        if self.profile.unwrap_speaker_enclosed_body:
            speaker, body = resolve_speaker_enclosed_body(
                assembly.body, speaker, configured_speaker_names(self.profile))
        else:
            body = assembly.body
        result = self._accept_line(
            sequence=body_event.sequence, raw=body_event.text, body=body,
            speaker=speaker, origin=self.profile.body_source, verified_boundary=False,
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
        speaker = fields.speaker
        if self.profile.unwrap_speaker_enclosed_body:
            speaker, body = resolve_speaker_enclosed_body(
                fields.body, speaker, configured_speaker_names(self.profile))
        else:
            body = fields.body
        return self._accept_line(sequence=sequence, raw=text, body=body,
                                 speaker=speaker, origin='ocr',
                                 verified_boundary=had_blank)

    def _accept_line(self, *, sequence: int, raw: str, body: str, speaker: str, origin: str,
                     verified_boundary: bool = False, received_ns: int = 0) -> Token | None:
        old = self.utterance
        external_transition = bool(
            old and not self.sent and received_ns > 0 and
            (verified_boundary or raw != old.raw or self.profile.repeat_rule == 'each_flush'))
        if (old and not self.sent and old.raw == raw and not verified_boundary and
                self.profile.repeat_rule != 'each_flush'):
            return None
        if self.pending_profile is not None:
            self.profile = self.pending_profile
            self.pending_profile = None
        if (old and self.sent and not verified_boundary and raw == old.raw and
                self.profile.repeat_rule != 'each_flush'):
            self.pause('ambiguous_identical_line')
            return None
        self.external_transition_ns = received_ns if external_transition else 0
        self.line_no += 1
        self.utterance = interpret(
            raw, self.profile, session=self.session, line=self.line_no, source=origin,
            sequence=sequence, g2p=self.g2p, speaker_override=speaker, body_override=body)
        self.completed = False
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = False
        self.native_progress = UtteranceProgress()
        self.audio_deadline_elapsed = False
        self.sent = False
        self.awaiting_next = False
        self.hold_reason = None
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
        if event == 'audio_deadline_elapsed':
            if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return False
            self.audio_deadline_elapsed = True
            if self.native_progress.ended:
                self._resolve_pause('audio_judgment_unknown_hold')
                self._resolve_pause('audio_judgment_active_hold')
                return self._presentation_done('utterance_progress')
            if self.native_progress.active:
                self.pause('audio_judgment_active_hold')
                return False
            if u.uncertain_text_fallback:
                return self._presentation_done('uncertain_text_fallback')
            self.pause('audio_judgment_unknown_hold')
            return False
        if event in ('native_unavailable', 'native_failed'):
            if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
                return False
            # Loss of the acoustic path makes the current judgment unknown; it
            # does not prove that an earlier ACTIVE target has ended.
            self.native_progress = UtteranceProgress()
            if u.uncertain_text_fallback and self.audio_deadline_elapsed:
                return self._presentation_done('uncertain_text_fallback')
            if not u.uncertain_text_fallback:
                self.pause('native_audio_unavailable_hold' if event == 'native_unavailable'
                           else 'native_audio_failure')
            return False
        raise ValueError(f'unknown presentation event: {event}')


    def apply_utterance_progress(self, token: Token, progress: UtteranceProgress) -> bool:
        """Consume the unified audio judgment without reinterpreting evidence."""
        if token != self.token or self.sent or self.completed or not self.utterance:
            return False
        u = self.utterance
        if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
            return False
        self.native_progress = progress
        if progress.ended:
            if self.hold_reason in {
                    'audio_judgment_unknown_hold',
                    'audio_judgment_active_hold',
                    'native_audio_unavailable_hold',
                    'native_audio_failure'}:
                self.hold_reason = None
            return self._presentation_done('utterance_progress')
        if not self.audio_deadline_elapsed:
            return False
        if progress.active:
            if self.hold_reason in {
                    'audio_judgment_unknown_hold',
                    'native_audio_unavailable_hold',
                    'native_audio_failure'}:
                self.hold_reason = None
            self.pause('audio_judgment_active_hold')
            return False
        if u.uncertain_text_fallback:
            if self.hold_reason in {
                    'audio_judgment_unknown_hold',
                    'audio_judgment_active_hold',
                    'native_audio_unavailable_hold',
                    'native_audio_failure'}:
                self.hold_reason = None
            return self._presentation_done('uncertain_text_fallback')
        if self.hold_reason in {'audio_judgment_active_hold', 'native_audio_failure'}:
            self.hold_reason = None
        self.pause('audio_judgment_unknown_hold')
        return False

    def _presentation_done(self, reason: str) -> bool:
        self.completed = True
        self.presentation_reason = reason
        return True

    @property
    def token(self) -> Token | None:
        if self.utterance is None:
            return None
        return Token(self.session, self.utterance.line)

    @property
    def pause_reason(self) -> str | None:
        """Runtime hold reason, independent of the user's AUTO ON/OFF choice."""
        return self.hold_reason

    def permit(self, allowed: bool) -> None:
        """Change only the user's automatic-advance authorization."""
        self.authorized = bool(allowed)

    def pause(self, reason: str) -> None:
        """Record the first runtime fact that blocks this Utterance."""
        if self.hold_reason is None:
            self.hold_reason = reason

    def _resolve_pause(self, reason: str) -> bool:
        """Resolve one runtime hold only when its own recovery fact arrives."""
        if self.hold_reason != reason:
            return False
        self.hold_reason = None
        return True

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
        if not (self.authorized and self.completed and self.utterance and self.target and not self.sent):
            return False
        ready = None
        if self.hold_reason == 'target_unavailable':
            ready = self.target_ready(self.target)
            if not ready:
                return False
            self._resolve_pause('target_unavailable')
        elif self.hold_reason is not None:
            return False
        if ready is None:
            ready = self.target_ready(self.target)
        if not ready:
            self.pause('target_unavailable')
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
        return True
