"""Single owner of line identity, presentation completion and advance authority."""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from collections.abc import Callable, Sequence
from .hook_protocol import HookEvent
from .model import AdvanceMode, Profile, Utterance, interpret


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
        self.active_text_source: str | None = None
        self.manual_advance_pending = False
        self.manual_boundary_ns = 0
        self.accepted_manual_boundary_ns = 0

    def begin(self, profile: Profile, target: Target, host_generation: str, connection_generation: int,
              available_sources: tuple[str, ...] = ()) -> int:
        profile.validate()
        if profile.source == 'hook' and not any(h.role == 'text' for h in profile.hooks):
            raise ValueError('select at least one text HOOK to monitor')
        if not host_generation or connection_generation <= 0:
            raise ValueError('Host connection must be identified')
        self.close('session_changed')
        self.profile, self.target = deepcopy(profile), target
        self.pending_profile = None
        self.source_generation, self.connection_generation = host_generation, connection_generation
        # Only one selected dialogue stream may own the utterance sequence.
        # Prefer the highest-priority saved source that is already observed.
        self.active_text_source = next((h.identity for h in profile.hooks
            if h.role == 'text' and h.identity in available_sources), None)
        self.pause_reason = None
        self.reason = 'advance_not_authorized'
        return self.session

    def close(self, reason: str = 'stopped') -> None:
        self.session += 1
        self.profile = None
        self.pending_profile = None
        self.target = None
        self.source_generation = ''
        self.connection_generation = 0
        self.highest_sequence = 0
        self.utterance = None
        self.line_no = 0
        self.authorized = self.completed = self.sent = False
        self.awaiting_next = False
        self.ocr_blank = False
        self.active_text_source = None
        self.manual_advance_pending = False
        self.manual_boundary_ns = 0
        self.accepted_manual_boundary_ns = 0
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = self.native_playing = self.fallback_elapsed = False
        self.pause_reason = None
        self.reason = reason

    def manual_advance(self, observed_ns: int) -> bool:
        """Record a user advance candidate without inventing the next line.

        The old presentation can no longer authorize input. A later selected
        HOOK flush at or after this boundary is allowed to establish the next
        Utterance, including an identical line.
        """
        if self.profile is None or observed_ns <= 0:
            return False
        self.manual_advance_pending = True
        self.manual_boundary_ns = max(self.manual_boundary_ns, observed_ns)
        self.accepted_manual_boundary_ns = 0
        self.authorized = False
        self.completed = False
        self.presentation_reason = None
        self.pause_reason = 'manual_intervention'
        self.reason = 'manual_advance_waiting'
        return True

    def stage_profile(self, profile: Profile) -> None:
        """Apply compatible setting edits at the next utterance boundary.

        The active Utterance and its input key remain frozen. Capture-source or
        HOOK ownership changes require a new monitoring session and therefore
        are rejected here rather than hidden behind compatibility behavior.
        """
        profile.validate()
        if self.profile is None:
            raise ValueError('monitoring is not active')
        if (profile.id != self.profile.id or profile.source != self.profile.source or
                profile.hooks != self.profile.hooks or profile.speaker_links != self.profile.speaker_links or
                (profile.source == 'ocr' and profile.ocr != self.profile.ocr)):
            raise ValueError('capture ownership changes require a new monitoring session')
        staged = deepcopy(profile)
        if self.utterance is None:
            self.profile = staged
            self.pending_profile = None
        else:
            self.pending_profile = staged

    def observe(self, *, generation: str, connection: int, sequence: int,
                source: object, text: str, pid: int, received_ns: int = 0,
                paired_speaker: str | None = None) -> Token | None:
        if not self.profile or not self.target or self.profile.source != 'hook':
            return None
        if pid != (self.target.hook_pid or self.target.pid):
            return None
        if generation != self.source_generation or connection != self.connection_generation:
            self.pause('host_changed')
            return None
        matched = [item for item in self.profile.hooks if item.matches(source)]
        if len(matched) != 1:
            # Candidate HOOKs may emit freely during monitoring, but they
            # never own the active line's sequence or stop reason.
            return None
        if sequence <= self.highest_sequence:
            return None
        self.highest_sequence = sequence
        role = matched[0].role
        if role == 'speaker':
            # Speaker-only notifications are handled only by the synchronized
            # multi-HOOK collector. There is no second cross-batch pairing path.
            return None
        identity = matched[0].identity
        if not text.strip():
            return None
        if self.active_text_source is None:
            # Once chosen, another source cannot silently replace the line owner.
            self.active_text_source = identity
        if identity != self.active_text_source:
            return None
        if (self.manual_advance_pending and received_ns > 0 and
                received_ns < self.manual_boundary_ns):
            # The Host had already received this output before the observed
            # manual advance. It belongs to the state the user just dismissed.
            return None
        return self._accept_line(sequence=sequence, text=text, origin=identity,
                                 side_speaker=paired_speaker or '',
                                 verified_boundary=False, received_ns=received_ns)

    def observe_group(self, events: Sequence[HookEvent]) -> Token | None:
        """Join selected Luna-flushed sources like Luna's multi-HOOK collector.

        This groups *outputs*, not bytes or incremental text. Never guess a
        pairing when a batch has multiple possible lines or speakers.
        """
        if not self.profile or self.profile.source != 'hook' or not events:
            return None
        ordered = sorted(events, key=lambda e: e.sequence)
        if any(e.kind != 'output' or e.source is None for e in ordered):
            return None
        if any(e.host_generation != self.source_generation or
               e.connection_generation != self.connection_generation for e in ordered):
            self.pause('host_changed')
            return None
        if not self.target or any(e.process_id != (self.target.hook_pid or self.target.pid) for e in ordered):
            return None
        if ordered[0].sequence <= self.highest_sequence:
            return None
        roles = {h.identity: h.role for h in self.profile.hooks}
        matching = [e for e in ordered if e.source.identity in roles and e.text.strip()]
        text_events = [e for e in matching if roles[e.source.identity] == 'text']
        speaker_events = [e for e in matching if roles[e.source.identity] == 'speaker']
        if not text_events:
            # A speaker-only batch has no proven dialogue partner. The next
            # flushed dialogue might be a different utterance.
            self.highest_sequence = ordered[-1].sequence
            if speaker_events:
                self.pause('unpaired_speaker_output')
            return None
        # Luna maintains selected output priority. Only the chosen text stream
        # owns line identity; auxiliary selected streams are previews, not new
        # dialogues. Distinct flushes *of that same owner* in one batch are
        # ambiguous and cannot be joined into a synthetic line.
        chosen = self.active_text_source
        if chosen is None:
            chosen = next((h.identity for h in self.profile.hooks if h.role == 'text'
                           and any(e.source.identity == h.identity for e in text_events)), None)
        primary = [e for e in text_events if e.source.identity == chosen]
        if not primary:
            self.highest_sequence = ordered[-1].sequence
            return None
        if len(primary) != 1:
            self.highest_sequence = ordered[-1].sequence
            self.pause('ambiguous_multihook_lines')
            return None
        text_event = primary[0]
        selected = next((h for h in self.profile.hooks if h.matches(text_event.source)), None)
        if selected is None:
            return None
        speaker_identity = self.profile.speaker_links.get(selected.identity)
        if not speaker_identity:
            speaker_hooks = [h.identity for h in self.profile.hooks if h.role == 'speaker']
            speaker_identity = speaker_hooks[0] if len(speaker_hooks) == 1 else ''
        linked = [e for e in speaker_events if e.source.identity == speaker_identity]
        if len(linked) > 1 or (speaker_events and not speaker_identity):
            self.highest_sequence = ordered[-1].sequence
            self.pause('ambiguous_multihook_speakers')
            return None
        # A speaker from an earlier flush batch is not evidence that it belongs
        # to this text. Matching is limited to the same synchronized batch.
        side_speaker = linked[0].text.strip() if linked else ''
        # Do not update high-water before feeding the selected dialogue output.
        result = self.observe(generation=text_event.host_generation,
                              connection=text_event.connection_generation,
                              sequence=text_event.sequence, source=text_event.source,
                              text=text_event.text, pid=text_event.process_id,
                              received_ns=text_event.received_ns,
                              paired_speaker=side_speaker)
        self.highest_sequence = max(self.highest_sequence, ordered[-1].sequence)
        if speaker_events and not linked:
            self.pause('unpaired_speaker_output')
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
                # OCR can flicker between variants of the same visible line.
                # Without a blank interval or the user's advance, the change
                # does not establish a new dialogue boundary.
                self.pause('ocr_unverified_change')
            return None
        if self.utterance and text == self.utterance.raw and not self.ocr_blank:
            return None
        had_blank = self.ocr_blank
        self.ocr_blank = False
        return self._accept_line(sequence=sequence, text=text, origin='ocr', side_speaker='', verified_boundary=had_blank)

    def _accept_line(self, *, sequence: int, text: str, origin: str, side_speaker: str,
                     verified_boundary: bool = False, received_ns: int = 0) -> Token | None:
        old = self.utterance
        manual_boundary = bool(self.manual_advance_pending and received_ns > 0 and
                               received_ns >= self.manual_boundary_ns)
        if old and not self.sent and old.raw == text and not verified_boundary and not manual_boundary:
            # A duplicate notification is still the same Utterance unless an
            # observed manual advance precedes this new selected-HOOK flush.
            return None
        # Once the incoming output can establish a new boundary, settings staged
        # during the previous Utterance become the active profile before the new
        # line is interpreted. This also makes a newly selected repeat policy the
        # one that decides whether a post-input identical line is admissible.
        if self.pending_profile is not None:
            self.profile = self.pending_profile
            self.pending_profile = None
        if old and self.sent and not verified_boundary and not manual_boundary:
            if text == old.raw:
                if self.profile.repeat_rule != 'each_after_input':
                    self.pause('ambiguous_identical_line')
                    return None
        if manual_boundary:
            self.accepted_manual_boundary_ns = self.manual_boundary_ns
            self.manual_advance_pending = False
            self.manual_boundary_ns = 0
        else:
            self.accepted_manual_boundary_ns = 0
        self.line_no += 1
        self.utterance = interpret(text, self.profile, session=self.session,
                                   line=self.line_no, source=origin,
                                   sequence=sequence, side_speaker=side_speaker)
        self.completed = False
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = self.native_playing = self.fallback_elapsed = False
        self.sent = False
        self.awaiting_next = False
        self.reason = self.pause_reason or 'presenting'
        return Token(self.session, self.line_no)

    def take_manual_audio_boundary(self, token: Token) -> int:
        """Return a confirmed manual-input boundary for the accepted token once."""
        if token != self.token or self.accepted_manual_boundary_ns <= 0:
            return 0
        value = self.accepted_manual_boundary_ns
        self.accepted_manual_boundary_ns = 0
        return value

    def presentation_event(self, token: Token, event: str, *,
                           success: bool = True) -> bool:
        """All presentation evidence is owned by this Controller, never by GUI callbacks.

        A completion event returns True only on the first transition to ready.
        Events from another line/session or after input are inert.
        """
        if (token != self.token or self.sent or self.completed or not self.utterance
                or self.manual_advance_pending):
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
                self.pause('native_inconclusive_manual_handoff')
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
                    self.pause('native_inconclusive_manual_handoff')
            return False
        raise ValueError(f'unknown presentation event: {event}')

    def _presentation_done(self, reason: str) -> bool:
        self.completed = True
        self.presentation_reason = reason
        self.reason = 'ready' if self.authorized else (self.pause_reason or 'awaiting_permission')
        return True

    @property
    def token(self) -> Token | None:
        if self.utterance is None:
            return None
        return Token(self.session, self.utterance.line)

    def permit(self, allowed: bool) -> None:
        if allowed and self.manual_advance_pending:
            self.authorized = False
            self.pause_reason = 'manual_intervention'
            self.reason = 'manual_advance_waiting'
            return
        self.authorized = bool(allowed) and self.profile is not None
        self.pause_reason = None if self.authorized else 'advance_not_authorized'
        self.reason = ('ready' if self.completed else 'presenting') if self.authorized else 'advance_not_authorized'

    def pause(self, reason: str = 'manual_intervention') -> None:
        self.authorized = False
        self.pause_reason = reason
        self.reason = reason

    def expire_next_line(self, token: Token) -> bool:
        """Return input authority to the user after an unanswered advance.

        A late response may still be displayed, but cannot reauthorize input.
        """
        if token != self.token or not self.sent or not self.awaiting_next:
            return False
        self.awaiting_next = False
        self.pause('next_line_timeout')
        return True

    def try_advance(self) -> bool:
        if not (self.authorized and self.completed and self.utterance and self.target and not self.sent
                and not self.manual_advance_pending):
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
