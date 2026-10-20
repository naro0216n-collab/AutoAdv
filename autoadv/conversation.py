"""Single owner of line identity, presentation completion and advance authority."""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from collections.abc import Callable, Sequence
from .hook_protocol import HookEvent
from .model import Profile, Utterance, interpret


@dataclass(frozen=True)
class Target:
    pid: int
    hwnd: int

    def __post_init__(self):
        if self.pid <= 0 or self.hwnd <= 0:
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
        self.source_generation = ''
        self.connection_generation = 0
        self.highest_sequence = 0
        self.utterance: Utterance | None = None
        self.authorized = False
        self.completed = False
        self.sent = False
        self.reason = 'not_monitoring'
        self.line_no = 0
        self.pending_speakers: dict[str, tuple[str, int, int]] = {}
        self.awaiting_next = False
        self.ocr_blank = False
        self.active_text_source: str | None = None

    def begin(self, profile: Profile, target: Target, host_generation: str, connection_generation: int,
              available_sources: tuple[str, ...] = ()) -> int:
        profile.validate()
        if profile.source == 'hook' and not any(h.role == 'text' for h in profile.hooks):
            raise ValueError('select at least one text HOOK to monitor')
        if not host_generation or connection_generation <= 0:
            raise ValueError('Host connection must be identified')
        self.close('session_changed')
        self.profile, self.target = deepcopy(profile), target
        self.source_generation, self.connection_generation = host_generation, connection_generation
        # Only one selected dialogue stream may own the utterance sequence.
        # Prefer the highest-priority saved source that is already observed.
        self.active_text_source = next((h.identity for h in profile.hooks
            if h.role == 'text' and h.identity in available_sources), None)
        self.reason = 'advance_not_authorized'
        return self.session

    def close(self, reason: str = 'stopped') -> None:
        self.session += 1
        self.profile = None
        self.target = None
        self.source_generation = ''
        self.connection_generation = 0
        self.highest_sequence = 0
        self.utterance = None
        self.line_no = 0
        self.authorized = self.completed = self.sent = False
        self.awaiting_next = False
        self.pending_speakers.clear()
        self.ocr_blank = False
        self.active_text_source = None
        self.reason = reason

    def observe(self, *, generation: str, connection: int, sequence: int,
                source: object, text: str, pid: int, received_ns: int = 0,
                paired_speaker: str | None = None) -> Token | None:
        if not self.profile or not self.target or self.profile.source != 'hook':
            return None
        if pid != self.target.pid:
            return None
        if generation != self.source_generation or connection != self.connection_generation:
            self.pause('host_changed')
            return None
        if sequence <= self.highest_sequence:
            return None
        self.highest_sequence = sequence
        matched = [item for item in self.profile.hooks if item.matches(source)]
        if len(matched) != 1:
            self.reason = 'unselected_or_ambiguous_hook'
            return None
        role = matched[0].role
        if role == 'speaker':
            # Each speaker HOOK remains source-scoped until a corresponding text HOOK arrives.
            identity = matched[0].identity
            if text.strip():
                self.pending_speakers[identity] = (text.strip(), sequence, received_ns)
            else:
                self.pending_speakers.pop(identity, None)
            return None
        identity = matched[0].identity
        if not text.strip():
            return None
        if self.active_text_source is None:
            # Once chosen, another source cannot silently replace the line owner.
            self.active_text_source = identity
        if identity != self.active_text_source:
            return None
        return self._accept_line(sequence=sequence, text=text, origin=identity,
                                 side_speaker=(paired_speaker if paired_speaker is not None else
                                               self._paired_speaker(matched[0].identity, sequence, received_ns)),
                                 verified_boundary=False)

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
        if any(e.process_id != self.target.pid for e in ordered):
            return None
        if ordered[0].sequence <= self.highest_sequence:
            return None
        roles = {h.identity: h.role for h in self.profile.hooks}
        matching = [e for e in ordered if e.source.identity in roles and e.text.strip()]
        text_events = [e for e in matching if roles[e.source.identity] == 'text']
        speaker_events = [e for e in matching if roles[e.source.identity] == 'speaker']
        if len(text_events) > 1:
            self.highest_sequence = ordered[-1].sequence
            self.pause('ambiguous_multihook_lines')
            return None
        if not text_events:
            # Persist an unambiguous speaker for a subsequent flush group.
            if len(speaker_events) == 1:
                e = speaker_events[0]
                self.observe(generation=e.host_generation, connection=e.connection_generation,
                             sequence=e.sequence, source=e.source, text=e.text,
                             pid=e.process_id, received_ns=e.received_ns)
            elif len(speaker_events) > 1:
                self.pause('ambiguous_multihook_speakers')
            self.highest_sequence = ordered[-1].sequence
            return None
        text_event = text_events[0]
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
        side_speaker = linked[0].text.strip() if linked else self._paired_speaker(
            selected.identity, text_event.sequence, text_event.received_ns)
        # Do not update high-water before feeding the selected dialogue output.
        result = self.observe(generation=text_event.host_generation,
                              connection=text_event.connection_generation,
                              sequence=text_event.sequence, source=text_event.source,
                              text=text_event.text, pid=text_event.process_id,
                              received_ns=text_event.received_ns,
                              paired_speaker=side_speaker)
        self.highest_sequence = max(self.highest_sequence, ordered[-1].sequence)
        return result

    def _paired_speaker(self, text_source: str, sequence: int, received_ns: int) -> str:
        if not self.profile:
            return ''
        speaker_source = self.profile.speaker_links.get(text_source)
        if not speaker_source:
            speaker_hooks = [h.identity for h in self.profile.hooks if h.role == 'speaker']
            speaker_source = speaker_hooks[0] if len(speaker_hooks) == 1 else ''
        pending = self.pending_speakers.pop(speaker_source, None) if speaker_source else None
        if not pending:
            return ''
        name, previous, previous_ns = pending
        if (previous >= sequence or
                (received_ns > 0 and previous_ns > 0 and received_ns - previous_ns > 2_000_000_000) or
                (received_ns == 0 and sequence != previous + 1)):
            return ''
        return name

    def observe_ocr(self, *, sequence: int, text: str, pid: int, session: int) -> Token | None:
        if (not self.profile or self.profile.source != 'ocr' or not self.target
                or pid != self.target.pid or session != self.session
                or sequence <= self.highest_sequence):
            return None
        self.highest_sequence = sequence
        if not text.strip():
            self.ocr_blank = True
            return None
        if self.utterance and text == self.utterance.raw and not self.ocr_blank:
            return None
        had_blank = self.ocr_blank
        self.ocr_blank = False
        return self._accept_line(sequence=sequence, text=text, origin='ocr', side_speaker='', verified_boundary=had_blank)

    def _accept_line(self, *, sequence: int, text: str, origin: str, side_speaker: str,
                     verified_boundary: bool = False) -> Token | None:
        old = self.utterance
        if old and not self.sent and old.raw == text and not verified_boundary:
            return None
        if old and self.sent and not verified_boundary:
            if text == old.raw:
                if self.profile.repeat_rule != 'each_after_input':
                    self.pause('ambiguous_identical_line')
                    return None
        self.line_no += 1
        self.pending_speakers.clear()
        self.utterance = interpret(text, self.profile, session=self.session,
                                   line=self.line_no, source=origin,
                                   sequence=sequence, side_speaker=side_speaker)
        self.completed = False
        self.sent = False
        self.awaiting_next = False
        self.reason = 'presenting'
        return Token(self.session, self.line_no)

    def mark_complete(self, token: Token, *, success: bool = True) -> bool:
        if token != self.token or not success:
            if token == self.token:
                self.pause('presentation_failed')
            return False
        self.completed = True
        self.reason = 'ready' if self.authorized else 'awaiting_permission'
        return True

    @property
    def token(self) -> Token | None:
        if self.utterance is None:
            return None
        return Token(self.session, self.utterance.line)

    def permit(self, allowed: bool) -> None:
        self.authorized = bool(allowed) and self.profile is not None
        self.reason = ('ready' if self.completed else 'presenting') if self.authorized else 'advance_not_authorized'

    def pause(self, reason: str = 'manual_intervention') -> None:
        self.authorized = False
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
        if not (self.authorized and self.completed and self.utterance and self.target and not self.sent):
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
