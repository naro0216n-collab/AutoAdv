"""Single owner of line identity, presentation completion and advance authority."""
from __future__ import annotations
from dataclasses import dataclass
from copy import deepcopy
from collections.abc import Callable, Sequence
from threading import Lock
from .hook_protocol import HookEvent
from .g2p import G2PBackend
from .utterance_progress import UtteranceProgress
from .model import (AdvanceMode, Profile, Token, Utterance, assemble_dialogue_fields,
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
class PresentationTiming:
    """Input/acquisition facts attached to one admitted Presentation."""
    received_ns: int = 0
    causal_lower_bound_ms: int | None = None
    input_candidates_ms: tuple[int, ...] = ()


class Controller:
    """Callbacks from devices never send input; only try_advance may do that."""
    def __init__(self, input_send: Callable[[Target, int], int | None],
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
        self.fallback_due = False
        self.sent = False
        self.hold_reason: str | None = None
        self.line_no = 0
        self.ocr_blank = False
        self._input_facts_lock = Lock()
        self._external_input_times: list[int] = []
        self._pending_auto_input_ms: int | None = None
        self._presentation_timing = PresentationTiming()

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
        self.ocr_blank = False
        with self._input_facts_lock:
            self._external_input_times.clear()
            self._pending_auto_input_ms = None
            self._presentation_timing = PresentationTiming()
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = False
        self.native_progress = UtteranceProgress()
        self.fallback_due = False
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

    def observe_ocr(self, *, sequence: int, text: str, pid: int, session: int,
                    received_ns: int = 0) -> Token | None:
        if (not self.profile or self.profile.source != 'ocr' or not self.target
                or pid != self.target.pid or session != self.session
                or sequence <= self.highest_sequence):
            return None
        self.highest_sequence = sequence
        if not text.strip():
            # A stable blank is useful only to disambiguate an identical line
            # appearing again.  It is not a prerequisite for accepting a
            # different stable OCR body as the next Presentation.
            self.ocr_blank = True
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
                                 verified_boundary=had_blank,
                                 received_ns=received_ns)

    def _eligible_external_inputs_locked(self, received_ns: int) -> tuple[int, ...]:
        if received_ns <= 0:
            return tuple(self._external_input_times)
        received_ms = received_ns // 1_000_000
        return tuple(value for value in self._external_input_times if value <= received_ms)

    def _consume_presentation_timing_locked(
            self, received_ns: int, candidates: tuple[int, ...]) -> PresentationTiming:
        """Consume exactly the input facts used to admit this Presentation."""
        if received_ns <= 0:
            self._external_input_times.clear()
        elif candidates:
            received_ms = received_ns // 1_000_000
            self._external_input_times = [
                value for value in self._external_input_times if value > received_ms]
        causal = self._pending_auto_input_ms
        self._pending_auto_input_ms = None
        return PresentationTiming(
            received_ns=received_ns, causal_lower_bound_ms=causal,
            input_candidates_ms=candidates)

    def _accept_line(self, *, sequence: int, raw: str, body: str, speaker: str, origin: str,
                     verified_boundary: bool = False, received_ns: int = 0) -> Token | None:
        received_ns = max(0, int(received_ns))
        old = self.utterance
        staged = self.pending_profile
        repeat_profile = staged or self.profile
        reject_sent_identical = False
        with self._input_facts_lock:
            candidates = self._eligible_external_inputs_locked(received_ns)
            external_boundary = bool(candidates)
            if old and not self.sent and old.raw == raw and not verified_boundary and not external_boundary:
                if origin == 'ocr' or repeat_profile.repeat_rule != 'each_flush':
                    return None
            if old and self.sent and not verified_boundary and raw == old.raw and not external_boundary:
                reject_sent_identical = (
                    origin == 'ocr' or repeat_profile.repeat_rule != 'each_flush')
            timing = (None if reject_sent_identical else
                      self._consume_presentation_timing_locked(received_ns, candidates))
        if staged is not None:
            self.profile = staged
            self.pending_profile = None
        if reject_sent_identical:
            if origin != 'ocr':
                self.pause('ambiguous_identical_line')
            return None
        assert timing is not None
        self.line_no += 1
        self.utterance = interpret(
            raw, self.profile, session=self.session, line=self.line_no, source=origin,
            sequence=sequence, g2p=self.g2p, speaker_override=speaker, body_override=body)
        self.completed = False
        self.presentation_reason = None
        self.text_elapsed = self.tts_finished = False
        self.native_progress = UtteranceProgress()
        self.fallback_due = False
        self.sent = False
        self.hold_reason = None
        self._presentation_timing = timing
        return Token(self.session, self.line_no)

    def record_external_input(self, session: int, at_ms: int) -> bool:
        """Retain one observed game-input time without inventing a boundary.

        An input observed after the current body makes that token stale for
        further AutoAdv input.  The fact is retained until a later admitted
        body consumes it as timing evidence for that new Presentation.
        """
        at_ms = int(at_ms)
        if session != self.session or self.profile is None or at_ms <= 0:
            return False
        with self._input_facts_lock:
            self._external_input_times.append(at_ms)
            self._external_input_times.sort()
        return True

    def presentation_timing(self, token: Token) -> PresentationTiming | None:
        if token != self.token:
            return None
        return self._presentation_timing

    def _external_input_pending(self) -> bool:
        with self._input_facts_lock:
            return bool(self._external_input_times)

    def presentation_event(
        self, token: Token, event: str, *, success: bool = True,
    ) -> bool:
        """Record one presentation fact, then reconcile completion once.

        Device/runtime callbacks only update facts here.  The completion rule is
        centralized in ``_reconcile_completion`` so asynchronous event order
        cannot create a second progression policy.
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
        elif event == 'text_elapsed':
            if u.advance_mode != AdvanceMode.TEXT:
                return False
            self.text_elapsed = True
        elif event == 'fallback_due':
            if (u.advance_mode != AdvanceMode.AUDIO or u.use_tts or
                    not u.uncertain_text_fallback):
                return False
            self.fallback_due = True
        elif event == 'force_due':
            return self._presentation_done('force_timeout')
        else:
            raise ValueError(f'unknown presentation event: {event}')
        return self._reconcile_completion()

    def apply_utterance_progress(
        self, token: Token, progress: UtteranceProgress,
    ) -> bool:
        """Consume one unified audio judgment without reinterpreting evidence."""
        if token != self.token or self.sent or self.completed or not self.utterance:
            return False
        u = self.utterance
        if u.advance_mode != AdvanceMode.AUDIO or u.use_tts:
            return False
        self.native_progress = progress
        return self._reconcile_completion()

    def _reconcile_completion(self) -> bool:
        """Derive Presentation completion from the facts owned by Controller."""
        u = self.utterance
        if u is None or self.sent or self.completed:
            return False
        if u.use_tts:
            if not self.tts_finished:
                return False
            if u.advance_mode == AdvanceMode.AUDIO:
                return self._presentation_done('tts')
            if self.text_elapsed:
                return self._presentation_done('tts_and_text')
            return False
        if u.advance_mode == AdvanceMode.TEXT:
            return self._presentation_done('text') if self.text_elapsed else False
        if self.native_progress.ended:
            return self._presentation_done('utterance_progress')
        if (self.fallback_due and u.uncertain_text_fallback
                and not self.native_progress.active):
            return self._presentation_done('uncertain_text_fallback')
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
        """Actionable runtime hold, independent of the user's AUTO choice."""
        return self.hold_reason

    def permit(self, allowed: bool) -> None:
        """Change only the user's automatic-advance authorization."""
        self.authorized = bool(allowed)

    def pause(self, reason: str) -> None:
        """Record the first runtime fact that blocks this Utterance."""
        if self.hold_reason is None:
            self.hold_reason = reason

    def try_advance(self) -> bool:
        if not (self.authorized and self.completed and self.utterance and self.target and not self.sent):
            return False
        # External input does not prove a dialogue boundary, but once it has
        # occurred after this body was admitted the current token can no longer
        # safely emit another game operation.  A later admitted body consumes
        # that input as timing evidence and restores normal evaluation.
        if self._external_input_pending():
            return False
        ready = None
        if self.hold_reason == 'target_unavailable':
            ready = self.target_ready(self.target)
            if not ready:
                return False
            self.hold_reason = None
        elif self.hold_reason is not None:
            return False
        if ready is None:
            ready = self.target_ready(self.target)
        if not ready:
            self.pause('target_unavailable')
            return False
        # Target checks may cross a scheduling boundary. The final external-input
        # check and operation reservation are one input-fact transaction: an
        # observer that records first prevents this token from reserving input;
        # an already reserved token remains one-way even if later I/O fails.
        with self._input_facts_lock:
            if self._external_input_times:
                return False
            self.sent = True
            self.completed = False
        try:
            sent_at_ms = self.input_send(self.target, self.profile.input_key)  # type: ignore[union-attr]
        except Exception:
            self.pause('input_failure_no_retry')
            return False
        if sent_at_ms is not None:
            sent_at_ms = int(sent_at_ms)
            if sent_at_ms > 0:
                with self._input_facts_lock:
                    self._pending_auto_input_ms = sent_at_ms
        return True
