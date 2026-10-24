"""Task-oriented application state presented to the GUI.

The GUI should describe the work the player is doing, not mirror HookHost,
Controller, or persistence internals.  Capture editing is deliberately a draft:
several sources can be combined and previewed before one coherent recipe replaces
the active profile.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import ctypes
import sys
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable
from collections.abc import Sequence

from .conversation import Controller, Target, Token
from .g2p import G2PBackend, default_g2p
from .utterance_progress import (
    NativeAudioObservation, UtteranceProgressEstimator, UtteranceProgressState,
)
from .hook_host import HookHost
from .luna_runtime import adopted_runtime
from .hook_protocol import HookEvent, ThreadSource
from .irodori_server import IrodoriServer
from .model import (HookSelection, OCRSettings, Profile, ReplacementRule, StructureRule,
                    Utterance, EXTRACTION_PRESETS, assemble_dialogue_fields,
                    configured_speaker_names, extract_fields, interpret,
                    resolve_speaker_enclosed_body)
from .native_audio import NativeAudio
from .ocr import OCRWorker, TesseractOCR
from .screen import WindowsClientCapture
from .storage import Catalog
from .voice import VoiceJobs
from .windows_input import WindowsAdvancementInput


@dataclass(frozen=True)
class CaptureSampleView:
    raw: str
    speaker: str
    body: str


@dataclass(frozen=True)
class CaptureCandidateView:
    identity: str
    address: int
    latest: str
    history: tuple[str, ...]
    samples: tuple[CaptureSampleView, ...]
    used_for: tuple[str, ...]
    preset: str
    ambiguous: bool


@dataclass(frozen=True)
class CaptureAssemblyView:
    sequence: int
    speaker: str
    body: str
    body_source: str
    speaker_source: str | None


@dataclass(frozen=True)
class CaptureProblemView:
    message: str
    action: str | None = None


@dataclass(frozen=True)
class CaptureWorkspaceView:
    candidates: tuple[CaptureCandidateView, ...]
    assembly: tuple[CaptureAssemblyView, ...]
    body_source: str | None
    speaker_source: str | None
    unwrap_speaker_enclosed_body: bool
    dirty: bool
    can_commit: bool
    problem: CaptureProblemView | None


@dataclass(frozen=True)
class RuntimeState:
    session: int
    token: Token | None
    target: Target | None
    profile: Profile | None
    utterance: Utterance | None
    authorized: bool
    sent: bool
    completed: bool
    ocr_blank: bool
    source_generation: str
    connection_generation: int
    presentation_reason: str | None
    pause_reason: str | None


@dataclass(frozen=True)
class HookConnectionState:
    attached_pid: int = 0
    connecting_pid: int = 0
    host_generation: str = ''
    connection_generation: int = 0

    @property
    def connected(self) -> bool:
        return bool(self.attached_pid and self.host_generation and self.connection_generation > 0)

    def connected_to(self, pid: int) -> bool:
        return self.connected and self.attached_pid == pid


@dataclass(frozen=True)
class RuntimeTargetState:
    hook_pid: int = 0
    hook_hwnd: int = 0
    audio_pid: int = 0
    input_pid: int = 0
    input_hwnd: int = 0

    @property
    def selected(self) -> bool:
        return self.hook_pid > 0

    @property
    def input_ready(self) -> bool:
        return bool((self.input_pid or self.hook_pid) > 0 and self.hwnd > 0)

    @property
    def hwnd(self) -> int:
        return self.input_hwnd or self.hook_hwnd

    def target(self) -> Target:
        input_pid = self.input_pid or self.hook_pid
        if self.hook_pid <= 0 or input_pid <= 0 or self.hwnd <= 0:
            raise ValueError('自動送りに使うゲームウィンドウを確認できません')
        return Target(
            input_pid,
            self.hwnd,
            hook_pid=self.hook_pid,
            audio_pid=self.audio_pid or self.hook_pid,
        )


@dataclass(frozen=True)
class VoiceServiceView:
    voices: tuple[str, ...]
    issue: str = ''


@dataclass(frozen=True)
class PlayDialogueView:
    speaker: str
    text: str
    use_tts: bool
    voice: str


@dataclass(frozen=True)
class PlayView:
    advance_authorized: bool
    dialogue: PlayDialogueView | None
    issue: CaptureProblemView | None


@dataclass(frozen=True)
class DiagnosticsView:
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return '\n'.join(self.lines)


@dataclass(frozen=True)
class CaptureHostEventOutcome:
    changed: bool = False
    new_ambiguity: bool = False
    active_source_removed: bool = False
    monitor_started: bool = False


@dataclass(frozen=True)
class HookEventOutcome:
    connection_transition: str | None = None
    capture: CaptureHostEventOutcome = CaptureHostEventOutcome()
    token: Token | None = None
    flush_delay_ms: int | None = None
    note: str = ''


_PLAY_ISSUES = {
    'target_unavailable': ('自動送り先のゲームウィンドウを確認できないため、この台詞の自動送りを保留しています。', None),
    'next_line_timeout': ('送り後に次の台詞を取得できません。会話取得を確認してください。', 'open_capture'),
    'ambiguous_identical_line': ('同じ本文が続き、次の台詞か判断できないため自動送りを保留しています。', 'open_capture'),
    'ambiguous_multihook_lines': ('同じ本文取得元から複数候補が届き、対応を確定できないため自動送りを保留しています。', 'open_capture'),
    'ambiguous_multihook_speakers': ('同じ話者取得元から複数候補が届き、対応を確定できないため自動送りを保留しています。', 'open_capture'),
    'ocr_unverified_change': ('OCRの変化を次の台詞と確認できないため自動送りを保留しています。', 'open_capture'),
    'ocr_failure': ('OCRで台詞を取得できないため、自動送りを保留しています。', 'open_capture'),
    'utterance_progress_unresolved_hold': ('現在の発声が終了したと確認できないため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
    'utterance_progress_active_hold': ('人の発声が続いているため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
    'native_audio_failure': ('ゲーム音声を確認できないため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
    'native_audio_unavailable_hold': ('音声判断を利用できないため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
    'tts_playback_failed': ('TTS再生に失敗したため、この台詞の自動送りを保留しています。', 'open_voice'),
    'host_changed': ('ゲームからの台詞取得が途切れたため、自動送りを保留しています。', 'open_capture_recovery'),
    'input_failure_no_retry': ('ゲームへの送り操作に失敗しました。安全のため自動再試行していません。', None),
}

_PLAY_ISSUES_WITHOUT_AUTO = {
    'ambiguous_identical_line',
    'ambiguous_multihook_lines',
    'ambiguous_multihook_speakers',
    'ocr_unverified_change',
    'ocr_failure',
    'tts_playback_failed',
    'host_changed',
}


@dataclass
class _ObservedSource:
    source: ThreadSource
    history: deque[tuple[int, str]]
    ambiguous: bool = False


@dataclass
class CaptureRecipe:
    hooks: list[HookSelection]
    body_source: str
    speaker_source: str
    source_rules: dict[str, list[StructureRule]]
    unwrap_speaker_enclosed_body: bool
    inserted_hooks: list[str]

    @classmethod
    def from_profile(cls, profile: Profile) -> 'CaptureRecipe':
        return cls(
            hooks=deepcopy(profile.hooks),
            body_source=profile.body_source,
            speaker_source=profile.speaker_source,
            source_rules=deepcopy(profile.source_rules),
            unwrap_speaker_enclosed_body=profile.unwrap_speaker_enclosed_body,
            inserted_hooks=list(profile.inserted_hooks),
        )

    def apply_to(self, profile: Profile) -> Profile:
        result = deepcopy(profile)
        result.hooks = deepcopy(self.hooks)
        result.body_source = self.body_source
        result.speaker_source = self.speaker_source
        result.source_rules = deepcopy(self.source_rules)
        result.unwrap_speaker_enclosed_body = self.unwrap_speaker_enclosed_body
        result.inserted_hooks = list(self.inserted_hooks)
        return result


class CaptureWorkspace:
    """Draft only the interdependent conversation-capture recipe."""

    def __init__(self, profile: Profile):
        self._base = CaptureRecipe.from_profile(profile)
        self._draft = deepcopy(self._base)
        self._observed: dict[tuple[str, int], _ObservedSource] = {}
        self._groups: deque[tuple[HookEvent, ...]] = deque(maxlen=16)

    @property
    def dirty(self) -> bool:
        return self._draft != self._base

    def reset(self, profile: Profile) -> None:
        self._base = CaptureRecipe.from_profile(profile)
        self._draft = deepcopy(self._base)
        self._observed.clear()
        self._groups.clear()

    def _refresh_ambiguity(self, identity: str) -> None:
        matches = [record for (candidate_identity, _), record in self._observed.items()
                   if candidate_identity == identity]
        ambiguous = len(matches) > 1
        for record in matches:
            record.ambiguous = ambiguous

    def observe(self, source: ThreadSource, text: str = '', *, sequence: int = 0) -> None:
        key = (source.identity, source.address)
        record = self._observed.get(key)
        if record is None:
            record = _ObservedSource(source, deque(maxlen=8))
            self._observed[key] = record
        else:
            record.source = source
        self._refresh_ambiguity(source.identity)
        value = text
        if value.strip() and (not record.history or record.history[-1][1] != value):
            record.history.append((sequence, value))

    def observe_group(self, events: Sequence[HookEvent]) -> None:
        outputs = tuple(sorted(
            (event for event in events if event.kind == 'output' and event.source is not None and event.text.strip()),
            key=lambda event: event.sequence))
        if outputs:
            self._groups.append(outputs)

    def clear_groups(self) -> None:
        self._groups.clear()

    def remove(self, identity: str, address: int) -> None:
        self._observed.pop((identity, address), None)
        self._refresh_ambiguity(identity)

    def clear_observations(self) -> None:
        self._observed.clear()
        self._groups.clear()

    def source(self, identity: str, address: int, *, require_unique: bool = True) -> ThreadSource | None:
        record = self._observed.get((identity, address))
        if record is None or (require_unique and record.ambiguous):
            return None
        return record.source

    def sources(self, *, unique_only: bool = True) -> tuple[ThreadSource, ...]:
        return tuple(record.source for record in self._observed.values()
                     if not unique_only or not record.ambiguous)

    def identity_is_ambiguous(self, identity: str) -> bool:
        return any(record.ambiguous for (candidate_identity, _), record in self._observed.items()
                   if candidate_identity == identity)

    def has_source(self, identity: str) -> bool:
        return self._record_for_identity(identity) is not None

    def has_output(self, identity: str) -> bool:
        record = self._record_for_identity(identity)
        return bool(record and record.history)

    def has_hook_code(self, code: str) -> bool:
        return any(record.source.hook_code == code for record in self._observed.values())

    def _rules_for(self, identity: str) -> tuple[StructureRule, ...]:
        configured = self._draft.source_rules.get(identity)
        return tuple(configured) if configured is not None else tuple(EXTRACTION_PRESETS['auto'])

    def _extract(self, raw: str, identity: str):
        return extract_fields(raw, self._rules_for(identity))

    def _preset_for(self, identity: str) -> str:
        configured = tuple(self._draft.source_rules.get(identity, ()))
        if not configured:
            return 'auto'
        for name, rules in EXTRACTION_PRESETS.items():
            if configured == tuple(rules):
                return name
        return 'custom'

    def candidate(self, identity: str, address: int) -> CaptureCandidateView | None:
        record = self._observed.get((identity, address))
        if record is None:
            return None
        history = tuple(text for _, text in record.history)
        samples = tuple(CaptureSampleView(raw, *(lambda fields: (fields.speaker, fields.body))(
            self._extract(raw, identity))) for raw in history)
        used_fields = []
        if identity == self._draft.body_source:
            used_fields.append('body')
        if identity == (self._draft.speaker_source or self._draft.body_source):
            used_fields.append('speaker')
        return CaptureCandidateView(identity, address, history[-1] if history else '', history,
                                    samples, tuple(used_fields), self._preset_for(identity), record.ambiguous)

    def _source(self, identity: str, address: int) -> ThreadSource:
        record = self._observed.get((identity, address))
        if record is None or record.ambiguous:
            raise ValueError('取得元を利用できません')
        return record.source

    def _ensure_hook(self, source: ThreadSource) -> HookSelection:
        existing = next((hook for hook in self._draft.hooks if hook.matches(source)), None)
        if existing is not None:
            return existing
        hook = HookSelection(source.hook_code, source.context, source.context2)
        self._draft.hooks.append(hook)
        return hook

    def set_field_source(self, field: str, identity: str | None, address: int | None = None) -> None:
        if field not in ('body', 'speaker'):
            raise ValueError('invalid dialogue field')
        value = ''
        if identity is not None:
            if address is None:
                record = self._record_for_identity(identity)
                if record is None:
                    raise ValueError('取得元を利用できません')
                source = record.source
            else:
                source = self._source(identity, address)
            value = self._ensure_hook(source).identity
        if field == 'body':
            self._draft.body_source = value
        else:
            self._draft.speaker_source = value

    def set_unwrap_speaker_enclosed_body(self, enabled: bool) -> None:
        self._draft.unwrap_speaker_enclosed_body = bool(enabled)

    def _format_assembled_body(
            self, body: str, speaker: str, configured_speakers: Sequence[str]) -> tuple[str, str]:
        if not self._draft.unwrap_speaker_enclosed_body:
            return speaker, body
        return resolve_speaker_enclosed_body(body, speaker, configured_speakers)

    def set_extraction_preset(self, identity: str, address: int, preset: str) -> None:
        if preset not in EXTRACTION_PRESETS:
            raise ValueError('不明な整形方式です')
        source = self._source(identity, address)
        hook = self._ensure_hook(source)
        if preset == 'auto':
            self._draft.source_rules.pop(hook.identity, None)
        else:
            self._draft.source_rules[hook.identity] = list(EXTRACTION_PRESETS[preset])

    def set_source_rules(self, identity: str, address: int, rules: Sequence[StructureRule]) -> None:
        source = self._source(identity, address)
        hook = self._ensure_hook(source)
        self._draft.source_rules[hook.identity] = [deepcopy(rule) for rule in rules]

    def source_rules(self, identity: str) -> tuple[StructureRule, ...]:
        return self._rules_for(identity)

    def _record_for_identity(self, identity: str) -> _ObservedSource | None:
        matches = [record for (candidate_identity, _), record in self._observed.items()
                   if candidate_identity == identity]
        return matches[0] if len(matches) == 1 and not matches[0].ambiguous else None

    def _assembly_from_events(
            self, events: Sequence[HookEvent], configured_speakers: Sequence[str]) -> CaptureAssemblyView | None:
        body_identity = self._draft.body_source
        if not body_identity:
            return None
        candidates = [event for event in events if event.source and event.text.strip()]
        extracted = [
            (event.source.identity, self._extract(event.text, event.source.identity))
            for event in candidates
        ]
        assembly = assemble_dialogue_fields(
            extracted, body_source=body_identity, speaker_source=self._draft.speaker_source)
        if assembly.problem or assembly.body_index is None:
            return None
        body_event = candidates[assembly.body_index]
        speaker_source = (self._draft.speaker_source or
                          (body_identity if assembly.speaker else None))
        speaker, body = self._format_assembled_body(
            assembly.body, assembly.speaker, configured_speakers)
        return CaptureAssemblyView(body_event.sequence, speaker, body,
                                   body_identity, speaker_source)

    def _assembly(self, configured_speakers: Sequence[str]) -> tuple[CaptureAssemblyView, ...]:
        body_identity = self._draft.body_source
        if not body_identity:
            return ()
        speaker_identity = self._draft.speaker_source or body_identity
        assembled: list[CaptureAssemblyView] = []
        if speaker_identity != body_identity:
            for group in self._groups:
                value = self._assembly_from_events(group, configured_speakers)
                if value is not None:
                    assembled.append(value)
        else:
            record = self._record_for_identity(body_identity)
            if record is not None:
                for sequence, raw in record.history:
                    fields = self._extract(raw, body_identity)
                    if fields.body:
                        speaker, body = self._format_assembled_body(
                            fields.body, fields.speaker, configured_speakers)
                        assembled.append(CaptureAssemblyView(
                            sequence, speaker, body, body_identity,
                            body_identity if fields.speaker else None))
        assembled.sort(key=lambda item: item.sequence)
        return tuple(assembled[-12:])

    def view(self, configured_speakers: Sequence[str] = ()) -> CaptureWorkspaceView:
        candidates = []
        for (_, address), record in self._observed.items():
            if not record.history:
                continue
            candidate = self.candidate(record.source.identity, address)
            if candidate is not None:
                candidates.append(candidate)
        providers = {value for value in (self._draft.body_source, self._draft.speaker_source) if value}
        ambiguous_selected = any(
            candidate.ambiguous and candidate.identity in providers for candidate in candidates)
        problem = None
        if ambiguous_selected:
            problem = CaptureProblemView('選択した取得元を一意に識別できません。別の取得元を選択してください。')
        return CaptureWorkspaceView(
            candidates=tuple(candidates), assembly=self._assembly(configured_speakers),
            body_source=self._draft.body_source or None,
            speaker_source=self._draft.speaker_source or None,
            unwrap_speaker_enclosed_body=self._draft.unwrap_speaker_enclosed_body,
            dirty=self.dirty, can_commit=bool(self._draft.body_source) and not ambiguous_selected,
            problem=problem)

    def adopt_inserted_hook(self, code: str) -> None:
        code = code.strip()
        if code and code not in self._draft.inserted_hooks:
            self._draft.inserted_hooks.append(code)

    def selected_provider_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            identity for identity in (self._draft.body_source, self._draft.speaker_source) if identity))

    def selected_hooks(self) -> tuple[HookSelection, ...]:
        selected = set(self.selected_provider_ids())
        return tuple(hook for hook in self._draft.hooks if hook.identity in selected)

    def committed_profile(self, profile: Profile) -> Profile:
        if not self.view().can_commit:
            raise ValueError('本文として使用する取得元を選択してください')
        candidate = self._draft.apply_to(profile)
        used = set(self.selected_provider_ids())
        candidate.hooks = [hook for hook in candidate.hooks if hook.identity in used]
        candidate.source_rules = {identity: rules for identity, rules in candidate.source_rules.items()
                                  if identity in used or identity == 'ocr'}
        base_codes = set(self._base.inserted_hooks)
        used_codes = {hook.code for hook in candidate.hooks}
        candidate.inserted_hooks = [
            code for code in candidate.inserted_hooks
            if code in base_codes or code in used_codes
        ]
        candidate.validate()
        return candidate

    def mark_committed(self, profile: Profile) -> None:
        self._base = CaptureRecipe.from_profile(profile)
        self._draft = deepcopy(self._base)


class AutoAdvApplication:
    """Application facade for user-level configuration actions.

    The application owns controller and LunaHost lifecycles.  The UI never
    reconstructs a Profile from widget types, edits capture provider storage
    directly, or translates Controller reasons into user-facing status text.
    """

    def __init__(self, catalog: Catalog, persist: Callable[[], None], *,
                 input_send: Callable | None = None, target_ready: Callable | None = None,
                 g2p: G2PBackend | None = None):
        self.catalog = catalog
        self._persist = persist
        self.game_id = ''
        self.profile_id = ''
        self.capture: CaptureWorkspace | None = None
        self._runtime_capture_problem: CaptureProblemView | None = None
        self._raw_input_send = input_send or self._windows_input_send
        self.g2p = g2p if g2p is not None else default_g2p()
        self.controller = Controller(self._send_runtime_input, target_ready or self._windows_target_ready, self.g2p)
        self.hook_host: HookHost | None = None
        self._host_epoch = 0
        self._irodori_server: IrodoriServer | None = None
        self._irodori_epoch = 0
        self._native_audio: NativeAudio | None = None
        self._native_audio_epoch = 0
        self._progress_token: Token | None = None
        self._progress_estimator = UtteranceProgressEstimator()
        self._ocr_worker: OCRWorker | None = None
        self._ocr_epoch = 0
        self._voice_jobs: VoiceJobs | None = None
        self._preview_jobs: VoiceJobs | None = None
        self._searched_hook_codes: set[str] = set()
        self._trial_hook_codes: set[str] = set()
        self._hook_output_group: list[HookEvent] = []
        self._hook_flush_epoch = 0
        self._hook_flush_pending = False
        self._synced_sources: dict[tuple[int, int, int, int], ThreadSource] = {}
        self._hook_connection = HookConnectionState()
        self._runtime_target = RuntimeTargetState()
        self._inserted_hook_codes: set[str] = set()
        self._pc_hooks_inserted = False
        self._capture_transition_pending = False
        self._schedule_runtime: Callable[[int, Callable[[], None]], None] | None = None
        self._runtime_observer: Callable[[], None] | None = None
        self._runtime_event_observer: Callable[[str, Token | None, dict], None] | None = None
        self._telemetry_observer: Callable[[str, Token | None, dict], None] | None = None

    @staticmethod
    def _windows_target_ready(target: Target) -> bool:
        """Confirm the selected HWND still belongs to the configured input process.

        Foreground focus is deliberately irrelevant: runtime input is posted
        directly to this HWND and must not steal focus from the user.
        """
        if sys.platform != 'win32':
            return False
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = (ctypes.c_void_p,)
        user32.IsWindow.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint32
        hwnd = ctypes.c_void_p(target.hwnd)
        if not user32.IsWindow(hwnd):
            return False
        pid = ctypes.c_uint32()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return False
        return pid.value == target.pid

    @staticmethod
    def _windows_input_send(target: Target, virtual_key: int) -> None:
        WindowsAdvancementInput(target.hwnd, virtual_key=virtual_key).advance()

    def configure_runtime_loop(self, *,
                               schedule: Callable[[int, Callable[[], None]], None],
                               on_change: Callable[[], None] | None = None,
                               on_event: Callable[[str, Token | None, dict], None] | None = None) -> None:
        """Bind the event-loop adapter without giving it conversation policy.

        Application owns which deadlines, playback and advancement operations
        occur.  Qt only schedules callbacks and repaints semantic views.
        """
        self._schedule_runtime = schedule
        self._runtime_observer = on_change
        self._runtime_event_observer = on_event

    def configure_telemetry(self, observer: Callable[[str, Token | None, dict], None] | None) -> None:
        """Attach one optional diagnostics sink; it never participates in policy."""
        self._telemetry_observer = observer

    def _notify_runtime(self) -> None:
        if self._runtime_observer is not None:
            self._runtime_observer()

    def _emit_runtime_event(self, event: str, token: Token | None = None, **detail) -> None:
        if self._runtime_event_observer is not None:
            self._runtime_event_observer(event, token, detail)
        self._emit_telemetry(event, token, **detail)

    def _emit_telemetry(self, event: str, token: Token | None = None, **detail) -> None:
        if self._telemetry_observer is not None:
            self._telemetry_observer(event, token, detail)

    def _accept_runtime_token(self, token: Token | None) -> None:
        if token is None or token != self.controller.token:
            return
        # Publish the admitted immutable Utterance before any presentation side
        # effect can emit PCM/alignment/TTS/completion telemetry. Besides making the
        # lifecycle causal, this gives validation a conservative reading-time
        # origin instead of timestamping the line after its timer has started.
        self._emit_runtime_event('utterance', token)
        self._start_presentation(token)

    def _schedule(self, delay_ms: int, callback: Callable[[], None]) -> None:
        if self._schedule_runtime is None:
            raise RuntimeError('runtime scheduler is not configured')
        self._schedule_runtime(max(0, int(delay_ms)), callback)

    def _dispatch_runtime(self, callback: Callable[[], None]) -> None:
        """Marshal service callbacks through the configured event loop when present."""
        if self._schedule_runtime is None:
            callback()
        else:
            self._schedule_runtime(0, callback)

    def _send_runtime_input(self, target: Target, virtual_key: int) -> None:
        token = self.controller.token
        self._emit_runtime_event('advance_attempt', token)
        self._raw_input_send(target, virtual_key)
        # Only a successfully issued AutoAdv input is a strong Presentation
        # Epoch boundary.  A failed input attempt must leave the current epoch
        # intact rather than inventing a transition the game never received.
        if self._native_audio is not None:
            self._native_audio.boundary()
            self._emit_runtime_event('audio_observation_boundary', token)
        self._emit_runtime_event(
            'advance_input', token, presentation_reason=self.controller.presentation_reason)

    def runtime_state(self) -> RuntimeState:
        c = self.controller
        return RuntimeState(
            session=c.session, token=c.token, target=c.target, profile=c.profile,
            utterance=c.utterance, authorized=c.authorized, sent=c.sent,
            completed=c.completed, ocr_blank=c.ocr_blank,
            source_generation=c.source_generation, connection_generation=c.connection_generation,
            presentation_reason=c.presentation_reason, pause_reason=c.pause_reason)

    def _runtime_capture_matches(self, profile: Profile) -> bool:
        active = self.runtime_state().profile
        if active is None or active.id != profile.id:
            return False
        if not (active.source == profile.source and active.hooks == profile.hooks and
                active.body_source == profile.body_source and
                active.speaker_source == profile.speaker_source and
                active.source_rules == profile.source_rules and
                active.unwrap_speaker_enclosed_body == profile.unwrap_speaker_enclosed_body and
                not (profile.source == 'ocr' and active.ocr != profile.ocr)):
            return False
        if active.advance_mode != profile.advance_mode:
            return False
        if active.advance_mode.value == 'audio' or profile.advance_mode.value == 'audio':
            return (active.native_helper == profile.native_helper and
                    active.alignment_model == profile.alignment_model and
                    active.alignment_vocab == profile.alignment_vocab)
        return True

    def _request_capture_transition(self) -> None:
        if self._defer_capture_transition():
            self._schedule(0, self._apply_capture_transition)

    def _apply_persisted_profile_change(self, previous: Profile, updated: Profile) -> None:
        """Apply one saved profile change to Host/runtime from one owner."""
        effective = self.catalog.effective_profile(updated)
        hook_environment_changed = (
            previous.hook_flush_ms, previous.hook_codepage,
            previous.hook_max_buffer, previous.hook_max_history, previous.insert_pc_hooks,
            tuple(previous.inserted_hooks)) != (
            updated.hook_flush_ms, updated.hook_codepage,
            updated.hook_max_buffer, updated.hook_max_history, updated.insert_pc_hooks,
            tuple(updated.inserted_hooks))
        if (updated.source == 'hook' and hook_environment_changed and
                self._hook_connection.connected and self.hook_host_running):
            try:
                self._restore_profile_capture(updated)
                self.clear_capture_problem()
            except Exception as exc:
                self._report_capture_problem(
                    '保存した取得設定を現在のゲームへ反映できません。取得の復旧を確認してください。')
                self._emit_runtime_event('capture_environment_apply_failed', None, reason=str(exc))
        state = self.runtime_state()
        if state.profile is not None and state.profile.id == updated.id:
            if self._runtime_capture_matches(effective):
                self.controller.stage_profile(effective)
                self._notify_runtime()
            else:
                self._request_capture_transition()

    def _begin_runtime(self, profile: Profile, target: Target, generation: str, connection: int) -> int:
        return self.controller.begin(profile, target, generation, connection)

    def _attempt_advance(self) -> bool:
        sent = self.controller.try_advance()
        state = self.runtime_state()
        if sent and state.token is not None and state.profile is not None:
            token = state.token
            if self._capture_transition_pending:
                self._schedule(0, self._apply_capture_transition)
            self._schedule(state.profile.next_line_timeout_ms,
                           lambda t=token: self._expire_next_line(t))
        self._notify_runtime()
        return sent

    def _expire_next_line(self, token: Token) -> bool:
        expired = self.controller.expire_next_line(token)
        if expired:
            self._emit_runtime_event('next_line_timeout', token)
        self._notify_runtime()
        return expired

    def _finish_presentation_transition(self, token: Token, completed: bool) -> bool:
        if completed:
            if self._native_audio is not None:
                self._native_audio.finish_line(token)
            self._emit_runtime_event('presentation_complete', token,
                                     reason=self.controller.presentation_reason)
            self._attempt_advance()
        else:
            self._notify_runtime()
        return completed

    def _runtime_presentation_event(self, token: Token, event: str, *, success: bool = True) -> bool:
        completed = self.controller.presentation_event(token, event, success=success)
        return self._finish_presentation_transition(token, completed)

    def set_auto_advance(self, enabled: bool) -> None:
        self.controller.permit(enabled)
        if enabled:
            self._attempt_advance()
        else:
            self._notify_runtime()

    def _audio_deadline_due(self, token: Token) -> None:
        """Apply the one text-derived fallback boundary to current voice state."""
        state = self.runtime_state()
        utterance = state.utterance
        if (token != state.token or utterance is None or utterance.use_tts or
                utterance.advance_mode.value != 'audio' or state.completed or state.sent):
            return
        self._emit_runtime_event('audio_deadline_elapsed', token,
                                 text_wait_ms=utterance.text_wait_ms)
        completed = self.controller.presentation_event(token, 'audio_deadline_elapsed')
        self._finish_presentation_transition(token, completed)
        if (not completed and self.runtime_state().pause_reason in {
                'utterance_progress_unresolved_hold', 'utterance_progress_active_hold'}):
            self._emit_native_capture_summary(token)

    def _start_presentation(self, token: Token) -> int:
        """Start every presentation effect for the current immutable Utterance.

        The event-loop adapter does not decide whether text time, native audio,
        TTS playback or uncertain fallback governs completion.
        """
        state = self.runtime_state()
        utterance = state.utterance
        if utterance is None or token != state.token:
            return 0
        self._cancel_voice()
        self._cancel_native_audio()
        self._progress_token = token
        self._progress_estimator = UtteranceProgressEstimator()
        external_ns = self.controller.take_external_transition(token)
        if external_ns:
            if self._native_audio is not None:
                self._native_audio.external_transition()
            self._emit_runtime_event('external_hook_transition', token, received_ns=external_ns,
                                     boundary_certainty='external_unknown')
        if utterance.advance_mode.value == 'text':
            self._schedule(utterance.text_wait_ms,
                           lambda t=token: self._runtime_presentation_event(t, 'text_elapsed'))
        elif not utterance.use_tts:
            if self.native_audio_available:
                self._begin_native_audio(token, utterance.speech.alignment_text,
                                         utterance.speech.alignment_candidates)
            self._schedule(utterance.text_wait_ms, lambda t=token: self._audio_deadline_due(t))
        if utterance.use_tts:
            if not utterance.voice:
                self._runtime_presentation_event(token, 'tts_finished', success=False)
            else:
                self._start_voice(token, utterance.speech.tts_text, utterance.voice)
        self._notify_runtime()
        return external_ns

    def _close_runtime(self, reason: str = 'stopped') -> None:
        self._hook_output_group.clear()
        self._progress_token = None
        self._progress_estimator = UtteranceProgressEstimator()
        self.controller.close(reason)
        self._notify_runtime()

    def _start_monitoring(self) -> RuntimeState:
        """Build the selected profile's one runtime path and its services."""
        profile = self.current_profile()
        if profile is None:
            raise ValueError('作品とプロファイルを選択してください')
        effective = self.catalog.effective_profile(profile)
        target = self._session_target()
        self.stop_monitoring('session_changed')
        if effective.source == 'ocr':
            self._begin_runtime(effective, target, 'ocr', 1)
            self._start_ocr(effective)
        else:
            selected_sources = tuple(identity for identity in
                                     (effective.body_source, effective.speaker_source) if identity)
            if any(self._capture_identity_is_ambiguous(identity) for identity in selected_sources):
                raise ValueError('選択した取得元を一意に識別できません。別の取得元を選択してください')
            connection = self._hook_connection
            if not connection.connected:
                raise RuntimeError('ゲームへのLunaHook接続が完了していません')
            self._begin_runtime(effective, target, connection.host_generation,
                               connection.connection_generation)
            self._reconcile_hook_sync()
        self._start_native_audio(effective)
        state = self.runtime_state()
        if state.target is not None and state.profile is not None:
            self._emit_runtime_event(
                'monitor_started', None, session=state.session, source=state.profile.source,
                game_id=self.game_id, profile_id=self.profile_id,
                hook_pid=state.target.hook_pid or state.target.pid,
                audio_pid=state.target.audio_pid or state.target.pid, input_pid=state.target.pid,
                generation=state.source_generation, connection=state.connection_generation)
        self._notify_runtime()
        return state

    def stop_monitoring(self, reason: str = 'stopped') -> None:
        prior = self.runtime_state()
        if prior.profile is not None:
            self._emit_runtime_event('monitor_ended', None, session=prior.session, reason=reason)
        target = self._runtime_target
        if target.selected:
            self._sync_hook_threads(target.hook_pid, clear=True)
        self._close_native_audio()
        self._close_ocr()
        self._cancel_voice()
        self.cancel_voice_preview()
        self._close_runtime(reason)

    def _reconcile_monitoring(self, profile: Profile | None = None) -> RuntimeState | None:
        """Converge runtime monitoring from current persisted and connection state.

        No resume latch is kept.  A saved capture becomes monitorable exactly
        when its target, connection and selected source are all available.
        """
        profile = profile or self.current_profile()
        if profile is None:
            return None
        effective = self.catalog.effective_profile(profile)
        state = self.runtime_state()
        # A persisted capture-affecting change stays staged until the current
        # dialogue boundary. Capture-source observation must not bypass that
        # boundary by reconciling the new profile early.
        if self._capture_transition_pending and state.profile is not None:
            return state
        if state.profile is not None and state.profile.id == effective.id and self._runtime_capture_matches(effective):
            return state
        target = self._runtime_target
        if profile.source == 'ocr':
            return self._start_monitoring() if target.input_ready else None
        if (not profile.body_source or not target.selected or
                not self._hook_connection.connected_to(target.hook_pid) or
                not self._capture_saved_sources_ready(profile)):
            return None
        if self._capture_identity_is_ambiguous(profile.body_source):
            return None
        if profile.speaker_source and self._capture_identity_is_ambiguous(profile.speaker_source):
            return None
        return self._start_monitoring()

    def activate_selected_target(self) -> str:
        """Apply the selected game target without exposing connection mechanics to UI."""
        profile = self.current_profile()
        if profile is None:
            raise ValueError('作品を登録してください')
        target = self._runtime_target
        if not target.selected:
            raise ValueError('実行中のゲーム・プロセスを選択してください')
        if profile.source == 'ocr':
            if target.input_ready:
                self._start_monitoring()
                return 'monitoring'
            return 'waiting-input-target'

        pid = target.hook_pid
        connection = self._hook_connection
        if self.hook_host_running and (connection.connected_to(pid) or connection.connecting_pid == pid):
            if connection.connected_to(pid):
                self._restore_profile_capture(profile)
                if self._reconcile_monitoring(profile) is not None:
                    return 'monitoring'
                return 'connected'
            return 'connecting'

        if (not self.hook_host_running or
                (connection.attached_pid and connection.attached_pid != pid) or
                (connection.connecting_pid and connection.connecting_pid != pid)):
            self.stop_monitoring('session_changed')
            self.clear_capture_observations()
            self.start_hook_host()
        if not self.hook_host_running:
            self._report_capture_problem('ゲームから台詞を取得するためのLunaHookを起動できません。')
            raise RuntimeError('LunaHook Hostの起動に失敗しました')
        if self._begin_hook_connection(pid, profile):
            return 'connecting'
        if self._reconcile_monitoring(profile) is not None:
            return 'monitoring'
        return 'connected'

    def _handle_hook_connection_event(self, event: HookEvent) -> str | None:
        """Apply connection provenance, profile restoration and monitor resume."""
        transition = self._accept_hook_connection_event(event)
        if transition in ('connected', 'changed'):
            if transition == 'changed':
                self.stop_monitoring('host_changed')
                self.clear_capture_observations()
            self.clear_capture_problem()
            profile = self.current_profile()
            if profile and profile.source == 'hook':
                try:
                    self._restore_profile_capture(profile)
                except Exception as exc:
                    self._report_capture_problem('保存した会話取得設定を現在のゲームへ復元できません。')
                    self._emit_runtime_event('capture_environment_apply_failed', None, reason=str(exc))
                    return 'restore_failed'
                self._reconcile_hook_sync()
                self._reconcile_monitoring(profile)
        elif transition == 'disconnected':
            self.stop_monitoring('host_changed')
            self.clear_capture_observations()
            self._report_capture_problem('ゲームからの台詞取得が切断されました。')
        return transition

    def _defer_capture_transition(self) -> bool:
        """Remember a runtime rebuild without changing the current Utterance.

        The accepted Utterance is immutable. A setting that requires rebuilding
        capture/audio services becomes active only after that line has been sent
        or an independently observed next dialogue boundary arrives.
        """
        self._capture_transition_pending = True
        state = self.runtime_state()
        return state.utterance is None or state.sent

    def _capture_transition_boundary(self, event: HookEvent) -> bool:
        if not self._capture_transition_pending:
            return False
        state = self.runtime_state()
        active = state.profile
        if not active or active.source != 'hook':
            return False
        if event.kind != 'output' or event.source is None or not event.text.strip():
            return False
        selected = next((h for h in active.hooks
                         if h.identity == active.body_source and h.matches(event.source)), None)
        if selected is None:
            return False
        return state.utterance is None or state.sent or event.text != state.utterance.raw

    def _consume_capture_transition(self) -> bool:
        if not self._capture_transition_pending:
            return False
        self._capture_transition_pending = False
        return True

    def _apply_capture_transition(self) -> bool:
        """Rebuild the active acquisition/runtime path at one safe dialogue boundary."""
        if not self._consume_capture_transition():
            return False
        profile = self.current_profile()
        if profile is None:
            self.stop_monitoring('session_changed')
            return False
        if profile.source == 'hook':
            if not profile.body_source:
                self.stop_monitoring('settings_changed')
                return False
            if not self._hook_connection.connected:
                self.stop_monitoring('host_changed')
                return False
        self._start_monitoring()
        return True

    def _handle_runtime_ocr_output(self, *, session: int, pid: int, sequence: int, text: str) -> Token | None:
        """Own OCR boundary transitions before admitting text to Controller."""
        if self._capture_transition_pending and text.strip():
            state = self.runtime_state()
            if state.utterance is None or state.sent or state.ocr_blank:
                # The replacement OCR worker will observe the still-visible line.
                self._apply_capture_transition()
                return None
        return self.controller.observe_ocr(session=session, pid=pid, sequence=sequence, text=text)

    def _runtime_uses_hook_group(self) -> bool:
        profile = self.controller.profile
        return bool(profile and profile.source == 'hook' and profile.body_source and
                    profile.speaker_source and profile.speaker_source != profile.body_source)

    def _hook_group_sync_ready(self, provider_ids: Sequence[str], process_id: int) -> bool:
        required = set(provider_ids)
        if len(required) <= 1:
            return True
        synchronized = {
            source.identity for source in self._synced_sources.values()
            if source.process_id == process_id and source.identity in required
        }
        return synchronized == required

    def _capture_uses_hook_group(self, event: HookEvent) -> bool:
        if self.capture is None or event.source is None or event.process_id is None:
            return False
        providers = self.capture.selected_provider_ids()
        return bool(
            len(providers) > 1 and event.source.identity in providers and
            self._hook_group_sync_ready(providers, event.process_id))

    def _invalidate_hook_group_evidence(self) -> None:
        """Discard evidence whenever provider/synchronization ownership changes."""
        self._hook_output_group.clear()
        self._hook_flush_epoch += 1
        self._hook_flush_pending = False
        if self.capture is not None:
            self.capture.clear_groups()

    def _handle_hook_output(self, event: HookEvent) -> tuple[Token | None, int | None]:
        """Admit one Host output and share one bounded multi-source group."""
        if self._capture_transition_boundary(event):
            self._apply_capture_transition()
        state = self.runtime_state()
        runtime_profile, target = state.profile, state.target
        runtime_matches = bool(
            event.kind == 'output' and event.source is not None and runtime_profile is not None and
            target is not None and runtime_profile.source == 'hook' and
            event.process_id == (target.hook_pid or target.pid) and
            event.host_generation == state.source_generation and
            event.connection_generation == state.connection_generation and
            any(h.matches(event.source) for h in runtime_profile.hooks))
        capture_matches = bool(
            event.kind == 'output' and event.source is not None and
            self._capture_event_matches_connection(event) and
            not self._capture_identity_is_ambiguous(event.source.identity))
        if not runtime_matches and not capture_matches:
            return None, None

        token = None
        if runtime_matches:
            # Provenance belongs to Application: at this point the output has
            # passed the current runtime PID/generation/connection and selected
            # HookSelection checks.  Emit that fact before Controller can turn
            # the output (or its synchronized group) into an Utterance.
            self._emit_telemetry(
                'selected_hook_output', None,
                session=state.session,
                generation=event.host_generation,
                connection=event.connection_generation,
                hook_sequence=event.sequence,
                source=event.source.identity,
                received_ns=event.received_ns,
                pid=event.process_id,
                address=event.source.address,
                context=event.source.context,
                context2=event.source.context2,
                raw=event.text,
            )
        runtime_requires_group = runtime_matches and self._runtime_uses_hook_group()
        runtime_grouped = bool(
            runtime_requires_group and runtime_profile is not None and target is not None and
            self._hook_group_sync_ready(
                tuple(dict.fromkeys((runtime_profile.body_source, runtime_profile.speaker_source))),
                target.hook_pid or target.pid))
        capture_grouped = capture_matches and self._capture_uses_hook_group(event)
        if runtime_matches and not runtime_requires_group:
            token = self.controller.observe(
                generation=event.host_generation, connection=event.connection_generation,
                sequence=event.sequence, source=event.source, text=event.text,
                pid=event.process_id or 0, received_ns=event.received_ns)
        if not runtime_grouped and not capture_grouped:
            return token, None

        self._hook_output_group.append(event)
        if len(self._hook_output_group) >= 64:
            grouped_token = self._flush_hook_outputs()
            return grouped_token or token, None
        delay = 100
        if runtime_grouped and runtime_profile is not None:
            delay = max(10, min(100, runtime_profile.hook_flush_ms))
        return token, delay

    def _flush_hook_outputs(self) -> Token | None:
        group, self._hook_output_group = self._hook_output_group, []
        if not group:
            return None
        if self.capture and any(self._capture_uses_hook_group(event) for event in group):
            self.capture.observe_group(group)
        state = self.runtime_state()
        if (self._runtime_uses_hook_group() and state.profile is not None and state.target is not None and
                self._hook_group_sync_ready(
                    tuple(dict.fromkeys((state.profile.body_source, state.profile.speaker_source))),
                    state.target.hook_pid or state.target.pid)):
            return self.controller.observe_group(group)
        return None

    @property
    def native_audio_available(self) -> bool:
        return self._native_audio is not None

    def _consume_native_observation(self, token: Token, observation: NativeAudioObservation) -> bool:
        """Apply one current-generation acoustic observation to product state."""
        state = self.runtime_state()
        if token != state.token or state.completed or state.sent:
            return False
        if self._progress_token != token:
            self._progress_token = token
            self._progress_estimator = UtteranceProgressEstimator()
        previous = self._progress_estimator.progress
        progress = self._progress_estimator.observe(observation)

        lexical = observation.lexical
        vocal = observation.vocal
        recurrence = observation.recurrence
        self._emit_telemetry(
            'native_audio_observation', token,
            kind=observation.kind,
            target_units=(lexical.target_units if lexical is not None else 0),
            aligned_target_units=(lexical.aligned_target_units if lexical is not None else 0),
            target_associated=(lexical.target_associated if lexical is not None else False),
            complete_alignment=(lexical.complete_alignment if lexical is not None else False),
            committed_target_end=(lexical.committed_target_end if lexical is not None else False),
            lexical_target_end_ms=(lexical.target_end_ms if lexical is not None else None),
            capture_start_ms=observation.capture_start_ms,
            capture_end_ms=observation.capture_end_ms,
            boundary_at_ms=observation.boundary_at_ms,
            boundary_certainty=observation.boundary_certainty.value,
            history_truncated=observation.history_truncated,
            vocal_mean_probability=(vocal.mean_probability if vocal is not None else 0.0),
            vocal_active_fraction=(vocal.active_fraction if vocal is not None else 0.0),
            vocal_first_active_ms=(vocal.first_active_ms if vocal is not None else None),
            vocal_last_active_ms=(vocal.last_active_ms if vocal is not None else None),
            recurrence_strength=(recurrence.strength if recurrence is not None else 0.0),
            recurrence_period_ms=(recurrence.period_ms if recurrence is not None else None),
            recurrence_stability=(recurrence.period_stability if recurrence is not None else 0.0),
            cross_epoch_persistence=(recurrence.cross_epoch_persistence if recurrence is not None else 0.0),
            recent_recurrence_strength=(recurrence.recent_strength if recurrence is not None else 0.0),
            pcm_last_frame_ms=observation.pcm_last_frame_ms,
            heartbeat_at_ms=observation.heartbeat_at_ms,
        )

        if progress != previous:
            self._emit_runtime_event(
                'utterance_progress', token,
                state=progress.state.value,
                ended_at_ms=progress.ended_at_ms,
                lexical_target_associated=progress.lexical_target_associated,
                lexical_target_ended=progress.lexical_target_ended,
                finite_vocal_observed=progress.finite_vocal_observed,
                persistent_vocal_observed=progress.persistent_vocal_observed,
                boundary_certainty=progress.boundary_certainty.value)
        if progress.state == UtteranceProgressState.ENDED and progress.ended_at_ms is not None:
            self._emit_telemetry(
                'utterance_progress_end_estimate', token,
                estimated_at_ns=progress.ended_at_ms * 1_000_000,
                evidence='utterance_progress')

        # Controller needs every state transition so the fallback deadline can
        # distinguish uncertainty from a known finite vocal event in progress.
        completed = self.controller.apply_utterance_progress(token, progress)
        if completed:
            self._finish_presentation_transition(token, True)
        return completed

    def _start_native_audio(self, profile: Profile) -> bool:
        """Own process audio capture and publish only semantic/telemetry events."""
        self._close_native_audio()
        if profile.advance_mode.value != 'audio':
            return False
        from .native_runtime import managed_paths
        managed = managed_paths()
        native_helper = profile.native_helper or managed.helper_dll
        alignment_model = profile.alignment_model or managed.alignment_model
        alignment_vocab = profile.alignment_vocab or managed.alignment_vocab
        state = self.runtime_state()
        if state.target is None:
            return False
        self._native_audio_epoch += 1
        epoch = self._native_audio_epoch
        session = state.session
        pid = state.target.audio_pid or state.target.pid

        def current() -> bool:
            now = self.runtime_state()
            return (epoch == self._native_audio_epoch and now.session == session and
                    now.target is not None and (now.target.audio_pid or now.target.pid) == pid)

        def consume_error(token: Token | None, detail: str) -> None:
            if not current():
                return
            self._emit_runtime_event('native_audio_error', token, reason=detail)
            self._handle_native_error(token, detail)

        def publish_error(token: Token | None, detail: str) -> None:
            if current():
                self._dispatch_runtime(lambda: consume_error(token, detail))

        def consume_observation(token: Token, observation: NativeAudioObservation) -> None:
            if current():
                self._consume_native_observation(token, observation)

        def publish_observation(token: Token, observation: NativeAudioObservation) -> None:
            if current():
                self._dispatch_runtime(lambda: consume_observation(token, observation))

        def publish_pcm(token: Token, frames: int) -> None:
            if current():
                self._emit_telemetry('native_pcm', token, frames=frames, audio_pid=pid)

        def publish_inference(token: Token, metrics: dict) -> None:
            if current():
                self._emit_telemetry('alignment_inference', token, **metrics)

        def activate(native: NativeAudio) -> None:
            if not current():
                native.close()
                return
            self._native_audio = native
            now = self.runtime_state()
            if (now.token is not None and now.utterance is not None and not now.sent and
                    not now.completed and now.utterance.advance_mode.value == 'audio' and
                    not now.utterance.use_tts):
                self._begin_native_audio(now.token, now.utterance.speech.alignment_text, now.utterance.speech.alignment_candidates)
            self._emit_runtime_event('native_ready', None, session=session, ready=True,
                                     reason='', audio_pid=pid)

        def fail(reason: str) -> None:
            if not current():
                return
            now = self.runtime_state()
            if now.token is not None:
                self._runtime_presentation_event(now.token, 'native_unavailable')
            self._emit_runtime_event('native_ready', None, session=session, ready=False,
                                     reason=reason, audio_pid=pid)
            self._notify_runtime()

        def load_native() -> None:
            native = None
            try:
                native = NativeAudio(
                    pid, Path(native_helper), Path(alignment_model), Path(alignment_vocab),
                    publish_error,
                    on_pcm=publish_pcm,
                    on_inference=publish_inference,
                    on_observation=publish_observation)
                native.start()
                self._dispatch_runtime(lambda: activate(native))
            except Exception as exc:
                if native is not None:
                    native.close()
                self._dispatch_runtime(lambda reason=str(exc): fail(reason))

        threading.Thread(target=load_native, daemon=True, name='autoadv-load-native').start()
        return True

    def _close_native_audio(self) -> None:
        self._native_audio_epoch += 1
        native, self._native_audio = self._native_audio, None
        if native is not None:
            threading.Thread(target=native.close, daemon=True, name='autoadv-close-native').start()

    def _cancel_native_audio(self) -> None:
        if self._native_audio is not None:
            self._native_audio.cancel()

    def _begin_native_audio(self, token: Token, alignment_text: str, candidates) -> bool:
        if self._native_audio is None:
            return False
        try:
            self._native_audio.begin(token, alignment_text, candidates=candidates)
        except Exception as exc:
            detail = 'alignment target failure: ' + str(exc)
            self._emit_runtime_event('native_audio_error', token, reason=detail)
            self._handle_native_error(token, detail)
            return False
        return True

    def _emit_native_capture_summary(self, token: Token) -> None:
        if self._native_audio is None:
            return
        self._emit_runtime_event(
            'native_capture_summary', token, audio_pid=self._native_audio.pid,
            **self._native_audio.capture_status())

    def _handle_native_error(self, token: Token | None, detail: str = '') -> bool:
        """Route an acquisition/recognition failure through Controller policy."""
        state = self.runtime_state()
        if token is None or token != state.token or state.utterance is None:
            return False
        utterance = state.utterance
        if utterance.advance_mode.value != 'audio' or utterance.use_tts:
            return False
        completed = self._runtime_presentation_event(token, 'native_failed')
        if not utterance.uncertain_text_fallback:
            self._emit_native_capture_summary(token)
        return completed

    def _start_ocr(self, profile: Profile) -> bool:
        """Own OCR acquisition, Controller admission and presentation dispatch."""
        self._close_ocr()
        if profile.source != 'ocr':
            return False
        state = self.runtime_state()
        if state.target is None:
            raise RuntimeError('runtime target required before OCR starts')
        self._ocr_epoch += 1
        epoch = self._ocr_epoch
        session = state.session
        pid = state.target.pid

        def current() -> bool:
            now = self.runtime_state()
            return (epoch == self._ocr_epoch and now.session == session and
                    now.target is not None and now.target.pid == pid)

        def consume_text(sequence: int, text: str) -> None:
            if not current():
                return
            token = self._handle_runtime_ocr_output(
                session=session, pid=pid, sequence=sequence, text=text)
            self._accept_runtime_token(token)

        def publish_text(sequence: int, text: str) -> None:
            if current():
                self._dispatch_runtime(lambda: consume_text(sequence, text))

        def consume_error(reason: str) -> None:
            if not current():
                return
            self.controller.pause('ocr_failure')
            self._emit_runtime_event('ocr_error', None, reason=reason)
            self._notify_runtime()

        def publish_error(reason: str) -> None:
            if current():
                self._dispatch_runtime(lambda: consume_error(reason))

        reader = TesseractOCR(profile.ocr, WindowsClientCapture(state.target.hwnd, pid))
        worker = OCRWorker(reader, publish_text, publish_error)
        self._ocr_worker = worker
        worker.start()
        return True

    def _close_ocr(self) -> None:
        self._ocr_epoch += 1
        worker, self._ocr_worker = self._ocr_worker, None
        if worker is not None:
            threading.Thread(target=worker.close, daemon=True, name='autoadv-close-ocr').start()

    def configure_voice_jobs(self, *, on_result: Callable[[Token, bool, str], None] | None,
                             on_preview: Callable[[bool, str], None]) -> None:
        """Bind observers while Application retains playback completion policy."""
        if self._voice_jobs is not None:
            self._voice_jobs.cancel()
        if self._preview_jobs is not None:
            self._preview_jobs.cancel()

        def runtime_result(token: Token, done: bool, reason: str) -> None:
            self._runtime_presentation_event(token, 'tts_finished', success=done)
            self._emit_runtime_event('tts_result', token, success=done, reason=reason)
            if on_result is not None:
                on_result(token, done, reason)

        def runtime_timing(token: Token, stage: str, duration_ms: float, audio_ms: float) -> None:
            self._emit_telemetry(
                'tts_timing', token, stage=stage, duration_ms=duration_ms, audio_ms=audio_ms)

        self._voice_jobs = VoiceJobs(runtime_result, on_timing=runtime_timing)
        self._preview_jobs = VoiceJobs(lambda _token, done, reason: on_preview(done, reason))

    def _start_voice(self, token: Token, reading: str, voice: str) -> None:
        if self._voice_jobs is None:
            raise RuntimeError('voice jobs are not configured')
        self._voice_jobs.start(token, self.catalog.irodori_url, reading, voice)

    def _cancel_voice(self) -> None:
        if self._voice_jobs is not None:
            self._voice_jobs.cancel()

    def start_voice_preview(self, reading: str, voice: str) -> None:
        if self._preview_jobs is None:
            raise RuntimeError('voice preview is not configured')
        self._preview_jobs.start(Token(-1, -1), self.catalog.irodori_url, reading, voice)

    def cancel_voice_preview(self) -> None:
        if self._preview_jobs is not None:
            self._preview_jobs.cancel()

    def runtime_target_state(self) -> RuntimeTargetState:
        return self._runtime_target

    def clear_runtime_target(self) -> None:
        self._runtime_target = RuntimeTargetState()

    def select_primary_target(self, pid: int, hwnd: int = 0) -> RuntimeTargetState:
        if pid <= 0 or hwnd < 0:
            raise ValueError('実行中ゲームのPID/HWNDを確認できません')
        self._runtime_target = RuntimeTargetState(
            hook_pid=pid, hook_hwnd=hwnd, audio_pid=pid, input_pid=pid, input_hwnd=hwnd)
        return self._runtime_target

    def select_audio_target(self, pid: int | None) -> RuntimeTargetState:
        current = self._runtime_target
        if not current.selected:
            raise ValueError('先に実行中ゲームを選択してください')
        if pid is not None and pid <= 0:
            raise ValueError('音声PIDを確認できません')
        self._runtime_target = RuntimeTargetState(
            hook_pid=current.hook_pid,
            hook_hwnd=current.hook_hwnd,
            audio_pid=pid or current.hook_pid,
            input_pid=current.input_pid or current.hook_pid,
            input_hwnd=current.input_hwnd or current.hook_hwnd,
        )
        return self._runtime_target

    def select_input_target(self, pid: int | None, hwnd: int | None) -> RuntimeTargetState:
        current = self._runtime_target
        if not current.selected:
            raise ValueError('先に実行中ゲームを選択してください')
        if pid is not None and (pid <= 0 or hwnd is None or hwnd <= 0):
            raise ValueError('入力先ウィンドウを確認できません')
        input_pid = current.hook_pid if pid is None else pid
        input_hwnd = current.hook_hwnd if pid is None else hwnd
        self._runtime_target = RuntimeTargetState(
            hook_pid=current.hook_pid,
            hook_hwnd=current.hook_hwnd,
            audio_pid=current.audio_pid or current.hook_pid,
            input_pid=input_pid,
            input_hwnd=input_hwnd or 0,
        )
        return self._runtime_target

    def _session_target(self) -> Target:
        return self._runtime_target.target()

    def _reset_hook_connection(self) -> None:
        self._hook_connection = HookConnectionState()
        self._inserted_hook_codes.clear()
        self._pc_hooks_inserted = False

    def _begin_hook_connection(self, pid: int, profile: Profile) -> bool:
        """Request one Host connection and own its transient identity."""
        if pid <= 0:
            raise ValueError('hook pid must be positive')
        state = self._hook_connection
        if state.connected_to(pid) or state.connecting_pid == pid:
            return False
        if not self.hook_host_running:
            raise RuntimeError('LunaHook Host is not running')
        self._hook_connection = HookConnectionState(connecting_pid=pid)
        try:
            self._hook_command('settings', flush_ms=profile.hook_flush_ms,
                codepage=profile.hook_codepage, max_buffer=profile.hook_max_buffer,
                max_history=profile.hook_max_history)
            self._hook_command('connect', pid=pid)
        except Exception:
            self._reset_hook_connection()
            raise
        return True

    def _record_inserted_hook(self, code: str) -> None:
        code = code.strip()
        if code:
            self._inserted_hook_codes.add(code)

    def _record_pc_hooks_inserted(self) -> None:
        self._pc_hooks_inserted = True

    def _restore_profile_capture(self, profile: Profile) -> None:
        """Apply one profile's Luna settings to the current connection.

        Reusing an already connected game after a profile change must not leave
        the previous profile's flush/codepage or omit saved inserted hooks.
        Idempotence is connection-scoped so repeated UI refreshes do not insert
        the same manual hooks again.
        """
        state = self._hook_connection
        if not self.hook_host_running or not state.connected:
            raise RuntimeError('LunaHook is not connected')
        self._hook_command('settings', flush_ms=profile.hook_flush_ms,
            codepage=profile.hook_codepage, max_buffer=profile.hook_max_buffer,
            max_history=profile.hook_max_history)
        if profile.insert_pc_hooks and not self._pc_hooks_inserted:
            self._hook_command('insert_pc_hooks', pid=state.attached_pid)
            self._pc_hooks_inserted = True
        for code in profile.inserted_hooks:
            if code in self._inserted_hook_codes:
                continue
            self._hook_command('insert_hook', pid=state.attached_pid, code=code)
            self._inserted_hook_codes.add(code)

    def _accept_hook_connection_event(self, event: HookEvent) -> str | None:
        """Update transient Host connection identity from one provenance event."""
        state = self._hook_connection
        if event.kind == 'connected':
            if event.process_id is None or state.connecting_pid != event.process_id:
                return None
            changed = (state.attached_pid != event.process_id or
                       state.host_generation != event.host_generation or
                       state.connection_generation != event.connection_generation)
            self._hook_connection = HookConnectionState(
                attached_pid=event.process_id, connecting_pid=0,
                host_generation=event.host_generation,
                connection_generation=event.connection_generation)
            return 'changed' if changed else 'connected'
        if event.kind == 'disconnected' and event.process_id == state.attached_pid:
            self._reset_hook_connection()
            return 'disconnected'
        return None

    def _cancel_hook_connection_attempt(self, pid: int | None = None) -> None:
        state = self._hook_connection
        if pid is not None and state.connecting_pid != pid:
            return
        if state.connected:
            self._hook_connection = HookConnectionState(
                attached_pid=state.attached_pid, host_generation=state.host_generation,
                connection_generation=state.connection_generation)
        else:
            self._reset_hook_connection()

    def _capture_event_matches_connection(self, event: HookEvent) -> bool:
        state = self._hook_connection
        return bool(state.connected and event.process_id == state.attached_pid and
                    event.host_generation == state.host_generation and
                    event.connection_generation == state.connection_generation)

    @property
    def hook_host_running(self) -> bool:
        host = self.hook_host
        return bool(host and host.child is not None and host.child.poll() is None)

    def _schedule_hook_flush(self, delay_ms: int) -> None:
        if self._schedule_runtime is None or self._hook_flush_pending:
            return
        self._hook_flush_pending = True
        epoch = self._hook_flush_epoch

        def finish() -> None:
            if epoch != self._hook_flush_epoch:
                return
            self._hook_flush_pending = False
            token = self._flush_hook_outputs()
            self._accept_runtime_token(token)
            self._emit_runtime_event('hook_group_flushed', token)

        self._schedule(delay_ms, finish)

    def _consume_host_event(self, event: HookEvent) -> None:
        self.handle_hook_event(event)

    def _consume_host_closed(self, reason: str) -> None:
        self._handle_hook_closed()
        self._emit_runtime_event('hook_closed', None, reason=reason)

    def _consume_host_error(self, message: str) -> None:
        self._handle_hook_error(message)

    def start_hook_host(self) -> None:
        """Own the isolated Luna Host lifecycle and all Host state transitions."""
        self._host_epoch += 1
        epoch = self._host_epoch
        self._reset_hook_connection()
        if self.hook_host is not None:
            self.hook_host.close()
            self.hook_host = None
        self._synced_sources.clear()
        self._invalidate_hook_group_evidence()

        def current(callback, *args):
            if epoch == self._host_epoch:
                self._dispatch_runtime(lambda: callback(*args))

        runtime = adopted_runtime()
        host = HookHost(
            runtime.host,
            lambda event: current(self._consume_host_event, event),
            lambda reason: current(self._consume_host_closed, reason),
            lambda message: current(self._consume_host_error, message),
        )
        host.start()
        self.hook_host = host

    def close_hook_host(self) -> None:
        self._host_epoch += 1
        self._reset_hook_connection()
        host, self.hook_host = self.hook_host, None
        self._synced_sources.clear()
        self._invalidate_hook_group_evidence()
        if host is not None:
            host.close()

    def _reconcile_hook_sync(self) -> None:
        target = self._runtime_target
        if not target.selected:
            return
        errors = self._sync_hook_threads(target.hook_pid)
        if errors:
            self._report_capture_problem('複数の取得元を同期できません。取得の復旧を確認してください。')
            for reason in errors:
                self._emit_runtime_event('hook_sync_failed', None, reason=reason)

    def _sync_hook_threads(self, process_id: int, *, clear: bool = False) -> tuple[str, ...]:
        """Apply Luna selected-thread synchronization from application-owned recipes.

        Application recomputes synchronization whenever observed sources,
        capture recipes or the active runtime change.  The UI never owns the
        selected set or remembers which native threads were synchronized.
        """
        before = set(self._synced_sources)
        selected: list[HookSelection] = []
        if not clear:
            seen: set[str] = set()
            runtime = self.runtime_state().profile
            if runtime is not None and runtime.source == 'hook':
                provider_ids = tuple(dict.fromkeys(
                    identity for identity in (runtime.body_source, runtime.speaker_source) if identity))
                if len(provider_ids) > 1:
                    by_identity = {hook.identity: hook for hook in runtime.hooks}
                    for identity in provider_ids:
                        hook = by_identity.get(identity)
                        if hook is not None and identity not in seen:
                            selected.append(hook)
                            seen.add(identity)
            if self.capture is not None and len(self.capture.selected_provider_ids()) > 1:
                for hook in self.capture.selected_hooks():
                    if hook.identity not in seen:
                        selected.append(hook)
                        seen.add(hook.identity)

        desired: dict[tuple[int, int, int, int], ThreadSource] = {}
        for source in self._capture_sources():
            if source.process_id == process_id and any(h.matches(source) for h in selected):
                key = (source.process_id, source.address, source.context, source.context2)
                desired[key] = source

        errors: list[str] = []
        for key, source in tuple(self._synced_sources.items()):
            if key in desired:
                continue
            try:
                if self.hook_host_running:
                    self._hook_command('sync_thread', source=source.__dict__, enabled=False)
            except (RuntimeError, OSError, ValueError) as exc:
                errors.append(str(exc))
            self._synced_sources.pop(key, None)

        if self.hook_host_running:
            for key, source in desired.items():
                if key in self._synced_sources:
                    continue
                try:
                    self._hook_command('sync_thread', source=source.__dict__, enabled=True)
                    self._synced_sources[key] = source
                except (RuntimeError, OSError, ValueError) as exc:
                    errors.append(str(exc))
        elif clear:
            self._synced_sources.clear()
        if set(self._synced_sources) != before:
            # A group is valid only inside one concrete Luna synchronization
            # set. Never let evidence collected under an earlier sync set cross
            # into the newly synchronized epoch.
            self._invalidate_hook_group_evidence()
        return tuple(errors)

    def _hook_command(self, action: str, **kwargs) -> None:
        if not self.hook_host_running or self.hook_host is None:
            raise RuntimeError('LunaHook Host is not running')
        self.hook_host.command(action, **kwargs)

    def create_game(self, name: str, executable: str = ''):
        game = self.catalog.create_game(name, executable)
        try:
            self._persist()
        except Exception:
            self.catalog.games.pop(game.id, None)
            raise
        return game

    def add_profile(self, name: str) -> Profile:
        if self.game_id not in self.catalog.games:
            raise ValueError('作品を選択してください')
        profile = self.catalog.add_profile(self.game_id, name)
        try:
            self._persist()
        except Exception:
            self.catalog.games[self.game_id].profiles.pop(profile.id, None)
            raise
        return profile

    def duplicate_current_profile(self, name: str) -> Profile:
        current = self.current_profile()
        if current is None:
            raise ValueError('プロファイルを選択してください')
        duplicate = self.catalog.duplicate_profile(self.game_id, current.id, name)
        try:
            self._persist()
        except Exception:
            self.catalog.games[self.game_id].profiles.pop(duplicate.id, None)
            raise
        return duplicate

    def rename_current_profile(self, name: str) -> Profile:
        current = self.current_profile()
        if current is None:
            raise ValueError('プロファイルを選択してください')
        previous = current.name
        self.catalog.rename_profile(self.game_id, current.id, name)
        try:
            self._persist()
        except Exception:
            current.name = previous
            raise
        return current

    def delete_current_profile(self) -> str:
        current = self.current_profile()
        game = self.catalog.games.get(self.game_id)
        if current is None or game is None:
            raise ValueError('プロファイルを選択してください')
        previous = deepcopy(game)
        self.catalog.delete_profile(self.game_id, current.id)
        try:
            self._persist()
        except Exception:
            self.catalog.games[self.game_id] = previous
            raise
        next_id = game.default_profile
        self.select_profile(self.game_id, next_id)
        return next_id

    def set_default_profile(self) -> None:
        current = self.current_profile()
        game = self.catalog.games.get(self.game_id)
        if current is None or game is None:
            raise ValueError('プロファイルを選択してください')
        previous = game.default_profile
        game.default_profile = current.id
        try:
            self._persist()
        except Exception:
            game.default_profile = previous
            raise

    def install_catalog(self, catalog: Catalog) -> None:
        """Persist one imported catalog atomically, then make it the sole source."""
        previous = self.catalog
        self.catalog = catalog
        try:
            self._persist()
        except Exception:
            self.catalog = previous
            raise
        self._replace_catalog(catalog)

    def _replace_catalog(self, catalog: Catalog) -> None:
        """Replace the persisted catalog after an explicit import.

        A catalog replacement invalidates every selected game/profile and capture
        draft.  Callers must repopulate their presentation from this new single
        source rather than keeping the previous catalog alive in parallel.
        """
        self.catalog = catalog
        self.game_id = ''
        self.profile_id = ''
        self.capture = None
        self._hook_output_group.clear()
        self.clear_runtime_target()
        self._capture_transition_pending = False
        self._runtime_capture_problem = None

    def start_irodori(self, on_result: Callable[[VoiceServiceView], None]) -> None:
        """Connect or start Irodori without making connection management a UI task."""
        # A server generation change invalidates both preview and runtime HTTP
        # work.  Do not let an old synthesis finish against a replaced endpoint.
        self.cancel_voice_preview()
        self._cancel_voice()
        self._irodori_epoch += 1
        epoch = self._irodori_epoch
        if self._irodori_server is not None:
            self._irodori_server.close()
        root = Path(self.catalog.irodori_root) if self.catalog.irodori_root else None
        server = IrodoriServer(root, url=self.catalog.irodori_url)
        self._irodori_server = server

        def publish(view: VoiceServiceView) -> None:
            if epoch == self._irodori_epoch:
                on_result(view)

        def work() -> None:
            try:
                state = server.connect_or_start()
                if state.status == 'connected':
                    publish(VoiceServiceView(tuple(state.voices)))
                else:
                    message = state.message or 'Irodoriを利用できません。'
                    publish(VoiceServiceView((), message))
            except Exception as exc:
                publish(VoiceServiceView((), f'Irodoriへ接続できません: {exc}'))

        threading.Thread(target=work, daemon=True, name='autoadv-irodori').start()

    def close_irodori(self) -> None:
        self.cancel_voice_preview()
        self._cancel_voice()
        self._irodori_epoch += 1
        server, self._irodori_server = self._irodori_server, None
        if server is not None:
            server.close()

    def select_profile(self, game_id: str, profile_id: str) -> Profile | None:
        self.game_id, self.profile_id = game_id, profile_id
        game = self.catalog.games.get(game_id)
        profile = game.profiles.get(profile_id) if game else None
        self._hook_output_group.clear()
        self._hook_flush_epoch += 1
        self._hook_flush_pending = False
        self._capture_transition_pending = False
        self.capture = CaptureWorkspace(profile) if profile else None
        self._runtime_capture_problem = None
        self._searched_hook_codes.clear()
        self._trial_hook_codes.clear()
        return profile

    def current_profile(self) -> Profile | None:
        game = self.catalog.games.get(self.game_id)
        return game.profiles.get(self.profile_id) if game else None

    def _observe_capture(self, source: ThreadSource, text: str = '', *, sequence: int = 0) -> None:
        if self.capture:
            was_ambiguous = self.capture.identity_is_ambiguous(source.identity)
            self.capture.observe(source, text, sequence=sequence)
            if (source.identity in self.capture.selected_provider_ids() and
                    was_ambiguous != self.capture.identity_is_ambiguous(source.identity)):
                self._invalidate_hook_group_evidence()
            self._reconcile_hook_sync()


    def _remove_capture_source(self, identity: str, address: int) -> None:
        if self.capture:
            selected = identity in self.capture.selected_provider_ids()
            self.capture.remove(identity, address)
            if selected:
                self._invalidate_hook_group_evidence()
            self._reconcile_hook_sync()

    def clear_capture_observations(self) -> None:
        target = self._runtime_target
        if target.selected:
            self._sync_hook_threads(target.hook_pid, clear=True)
        if self.capture:
            self.capture.clear_observations()
        self._invalidate_hook_group_evidence()
        self._searched_hook_codes.clear()
        self._trial_hook_codes.clear()

    def capture_source(self, identity: str, address: int, *, require_unique: bool = True) -> ThreadSource | None:
        return self.capture.source(identity, address, require_unique=require_unique) if self.capture else None

    def _capture_sources(self, *, unique_only: bool = True) -> tuple[ThreadSource, ...]:
        return self.capture.sources(unique_only=unique_only) if self.capture else ()

    def capture_candidate(self, identity: str, address: int) -> CaptureCandidateView | None:
        return self.capture.candidate(identity, address) if self.capture else None


    def _capture_identity_is_ambiguous(self, identity: str) -> bool:
        return bool(self.capture and self.capture.identity_is_ambiguous(identity))

    def _capture_saved_sources_ready(self, profile: Profile) -> bool:
        if not self.capture or not profile.body_source:
            return False
        if (self.capture.identity_is_ambiguous(profile.body_source) or
                not self.capture.has_output(profile.body_source)):
            return False
        return (not profile.speaker_source or
                (not self.capture.identity_is_ambiguous(profile.speaker_source) and
                 self.capture.has_source(profile.speaker_source)))

    def _handle_capture_host_event(self, event: HookEvent) -> CaptureHostEventOutcome:
        """Own capture-source state transitions for one current Host event."""
        if event.source is None or not self._capture_event_matches_connection(event):
            return CaptureHostEventOutcome()
        identity = event.source.identity
        runtime_profile = self.runtime_state().profile
        active_sources = ({value for value in
                           (runtime_profile.body_source, runtime_profile.speaker_source) if value}
                          if runtime_profile else set())
        if event.kind == 'removed':
            active_removed = identity in active_sources
            self._remove_capture_source(identity, event.source.address)
            resumed = False
            if active_removed:
                self.stop_monitoring('host_changed')
                profile = self.current_profile()
                resumed = self._reconcile_monitoring(profile) is not None
                if resumed:
                    self.clear_capture_problem()
                else:
                    self._report_capture_problem('監視中の台詞取得元が消失しました。取得の復旧を確認してください。')
            return CaptureHostEventOutcome(
                changed=True, active_source_removed=active_removed, monitor_started=resumed)
        if event.kind not in ('created', 'output'):
            return CaptureHostEventOutcome()
        was_ambiguous = self._capture_identity_is_ambiguous(identity)
        self._observe_capture(event.source, event.text if event.kind == 'output' else '', sequence=event.sequence)
        new_ambiguity = self._capture_identity_is_ambiguous(identity) and not was_ambiguous
        active_ambiguity = new_ambiguity and identity in active_sources
        if active_ambiguity:
            self.stop_monitoring('host_changed')
            self._report_capture_problem('使用中の取得元を一意に識別できません。取得の復旧を確認してください。')
        resumed = self._reconcile_monitoring(self.current_profile()) is not None
        return CaptureHostEventOutcome(
            changed=True, new_ambiguity=active_ambiguity, monitor_started=resumed)

    def _apply_hook_event(self, event: HookEvent) -> HookEventOutcome:
        """Apply one Host event and return its semantic outcome internally."""
        note = ''
        if event.kind == 'hook_found' and self._capture_event_matches_connection(event):
            try:
                self.insert_recovery_hook(event.detail, discovered=True)
            except (RuntimeError, OSError, ValueError) as exc:
                note = '探索したHOOKを試用できません: ' + str(exc)

        transition = self._handle_hook_connection_event(event)
        capture_outcome = self._handle_capture_host_event(event)
        token = None
        delay = None
        if event.kind == 'output' and event.source is not None:
            token, delay = self._handle_hook_output(event)
        elif event.kind == 'inserted':
            target = self._runtime_target
            if target.selected and event.process_id == target.hook_pid:
                code = event.source.hook_code if event.source else ''
                note = f'LunaによるHOOK挿入: {event.detail} {code}'.strip()
        elif event.kind == 'diagnostic':
            self._handle_hook_diagnostic(event)
            note = 'LunaHost: ' + event.detail
        return HookEventOutcome(transition, capture_outcome, token, delay, note)

    def handle_hook_event(self, event: HookEvent) -> None:
        """Admit one Host event through the product's single external Hook contract.

        Result delivery is semantic notification only.  Callers inspect
        application state rather than using a second return-value observation
        surface that exists only for tests.
        """
        outcome = self._apply_hook_event(event)
        self._emit_runtime_event('hook_event', None, hook_event=event, outcome=outcome)
        if self._schedule_runtime is not None:
            self._accept_runtime_token(outcome.token)
            if outcome.flush_delay_ms is not None:
                self._schedule_hook_flush(outcome.flush_delay_ms)

    def _handle_hook_diagnostic(self, event: HookEvent) -> None:
        if event.kind != 'diagnostic':
            return
        if event.detail in ('existing_luna_injection_detected', 'cannot_attach_to_host_process'):
            self._cancel_hook_connection_attempt(event.process_id)
        if event.detail == 'existing_luna_injection_detected':
            self._report_capture_problem(
                '対象には既にLunaHookが注入されています。ゲームを再起動してから選び直してください。')
        elif event.detail == 'cannot_attach_to_host_process':
            self._report_capture_problem(
                'ゲームではないLunaHostプロセスが選択されています。実行中のゲームを選び直してください。')

    def handle_capture_activation_error(self, message: str = '') -> None:
        """Translate a capture-service activation failure into product state."""
        if self._runtime_capture_problem is None:
            self._report_capture_problem('ゲームから会話を取得できません。取得の復旧を確認してください。')
        if message:
            self._emit_runtime_event('capture_activation_failed', None, reason=message)

    def _handle_hook_error(self, message: str = '') -> None:
        """Expose a Host failure as a user-actionable capture problem.

        The UI adapter may log the native diagnostic, but it must not decide
        whether the failure is a capture problem or how it affects the product
        view.
        """
        self.handle_capture_activation_error(message)
        if message:
            self._emit_runtime_event('hook_error', None, reason=message)

    def _handle_hook_closed(self) -> None:
        self.stop_monitoring('host_changed')
        self._reset_hook_connection()
        self._report_capture_problem('ゲームからの台詞取得が停止しました。')

    def target_resolution_needed(self) -> bool:
        connection = self._hook_connection
        return not self._runtime_target.selected and not connection.attached_pid and not connection.connecting_pid

    def _connected_hook_pid(self) -> int:
        target = self._runtime_target
        connection = self._hook_connection
        if (not self.hook_host_running or not target.selected or
                not connection.connected_to(target.hook_pid)):
            raise RuntimeError('ゲームへのLunaHook接続が完了していません')
        return target.hook_pid

    def start_recovery_hook_search(self, boundary_module: str = '') -> None:
        pid = self._connected_hook_pid()
        self._hook_command('find_hooks', pid=pid, boundary_module=boundary_module,
                          parameters={'maxRecords': 64}, addresses='')

    def insert_pc_recovery_hooks(self) -> None:
        pid = self._connected_hook_pid()
        self._hook_command('insert_pc_hooks', pid=pid)
        self._record_pc_hooks_inserted()

    def insert_recovery_hook(self, code: str, *, discovered: bool = False) -> bool:
        code = code.strip()
        if not code:
            return False
        if discovered and not self._begin_recovery_hook_trial(code):
            return False
        try:
            pid = self._connected_hook_pid()
            self._hook_command('insert_hook', pid=pid, code=code)
            self._record_inserted_hook(code)
            self._mark_recovery_hook_inserted(code)
            return True
        except Exception:
            if discovered:
                self._recovery_hook_trial_failed(code)
            raise

    def _begin_recovery_hook_trial(self, code: str) -> bool:
        code = code.strip()
        if (not code or code in self._searched_hook_codes or
                (self.capture is not None and self.capture.has_hook_code(code))):
            return False
        self._searched_hook_codes.add(code)
        return True

    def _recovery_hook_trial_failed(self, code: str) -> None:
        self._searched_hook_codes.discard(code.strip())

    def _mark_recovery_hook_inserted(self, code: str) -> None:
        code = code.strip()
        if code:
            self._trial_hook_codes.add(code)

    def set_capture_field_source(self, field: str, identity: str | None, address: int | None = None) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        providers_before = self.capture.selected_provider_ids()
        source = self.capture.source(identity, address) if identity is not None and address is not None else None
        self.capture.set_field_source(field, identity, address)
        if self.capture.selected_provider_ids() != providers_before:
            self._invalidate_hook_group_evidence()
        if source is not None and source.hook_code in self._trial_hook_codes:
            self.capture.adopt_inserted_hook(source.hook_code)
        self._reconcile_hook_sync()

    def set_capture_unwrap_speaker_enclosed_body(self, enabled: bool) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        self.capture.set_unwrap_speaker_enclosed_body(enabled)

    def set_capture_extraction_preset(self, identity: str, address: int, preset: str) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        self.capture.set_extraction_preset(identity, address, preset)
        self._reconcile_hook_sync()

    def save_capture_source_rule(self, identity: str, address: int, index: int | None,
                                 rule: StructureRule) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        rules = list(self.capture.source_rules(identity))
        if index is not None and 0 <= index < len(rules):
            rules[index] = deepcopy(rule)
        else:
            rules.insert(0, deepcopy(rule))
        self.capture.set_source_rules(identity, address, rules)

    def move_capture_source_rule(self, identity: str, address: int, index: int, movement: int) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        rules = list(self.capture.source_rules(identity))
        if not 0 <= index < len(rules):
            raise ValueError('変更する抽出規則を選択してください')
        destination = index + movement
        if not 0 <= destination < len(rules):
            return
        rules[index], rules[destination] = rules[destination], rules[index]
        self.capture.set_source_rules(identity, address, rules)

    def delete_capture_source_rule(self, identity: str, address: int, index: int) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        rules = list(self.capture.source_rules(identity))
        if not 0 <= index < len(rules):
            raise ValueError('削除する抽出規則を選択してください')
        rules.pop(index)
        self.capture.set_source_rules(identity, address, rules)

    def capture_source_rules(self, identity: str) -> tuple[StructureRule, ...]:
        if not self.capture:
            return ()
        return self.capture.source_rules(identity)

    def capture_view(self) -> CaptureWorkspaceView:
        if not self.capture:
            return CaptureWorkspaceView((), (), None, None, False, False, False, self._runtime_capture_problem)
        profile = self.current_profile()
        view = self.capture.view(configured_speaker_names(profile) if profile else ())
        if view.problem is not None or self._runtime_capture_problem is None:
            return view
        return CaptureWorkspaceView(
            candidates=view.candidates, assembly=view.assembly, body_source=view.body_source,
            speaker_source=view.speaker_source,
            unwrap_speaker_enclosed_body=view.unwrap_speaker_enclosed_body,
            dirty=view.dirty, can_commit=view.can_commit,
            problem=self._runtime_capture_problem)

    def _report_capture_problem(self, message: str, action: str = 'open_capture_recovery') -> None:
        message = message.strip()
        self._runtime_capture_problem = CaptureProblemView(message, action) if message else None

    def clear_capture_problem(self) -> None:
        self._runtime_capture_problem = None

    def diagnostics_view(self) -> DiagnosticsView:
        connection = self._hook_connection
        target = self._runtime_target
        runtime = self.runtime_state()
        capture = self.capture_view()
        profile = self.current_profile()
        lines = [
            'LunaHost: ' + ('running' if self.hook_host_running else 'stopped'),
            ('Luna connection: pid=' + str(connection.attached_pid) +
             f' generation={connection.host_generation or "-"}/{connection.connection_generation}'
             if connection.connected else
             ('Luna connection: connecting pid=' + str(connection.connecting_pid)
              if connection.connecting_pid else 'Luna connection: disconnected')),
            ('Target: hook=' + str(target.hook_pid or '-') +
             ' audio=' + str(target.audio_pid or '-') +
             ' input=' + str(target.input_pid or '-') +
             ' hwnd=' + (hex(target.hwnd) if target.hwnd else '-')),
            f'Capture: candidates={len(capture.candidates)} assembly={len(capture.assembly)} dirty={capture.dirty}',
            ('Saved sources: ' + str(len(profile.hooks)) if profile else 'Saved sources: -'),
            ('Runtime: session=' + str(runtime.session) +
             ' source=' + (runtime.profile.source if runtime.profile else '-') +
             ' body=' + (runtime.profile.body_source if runtime.profile else '-') +
             ' token=' + (f'{runtime.token.session}:{runtime.token.line}' if runtime.token else '-') +
             ' sent=' + str(runtime.sent) + ' completed=' + str(runtime.completed)),
            ('Runtime reason: presentation=' + (runtime.presentation_reason or '-') +
             ' pause=' + (runtime.pause_reason or '-')),
        ]
        if capture.problem is not None:
            lines.append('Capture problem: ' + capture.problem.message)
        return DiagnosticsView(tuple(lines))

    def play_view(self) -> PlayView:
        controller = self.controller
        utterance = controller.utterance
        dialogue = (PlayDialogueView(
            utterance.speaker, utterance.text, utterance.use_tts, utterance.voice)
            if utterance is not None else None)
        authorized = controller.authorized
        issue = None
        reason = controller.pause_reason
        if authorized or reason in _PLAY_ISSUES_WITHOUT_AUTO:
            resolved = _PLAY_ISSUES.get(reason)
            if resolved:
                issue = CaptureProblemView(*resolved)
        return PlayView(authorized, dialogue, issue)


    def commit_capture(self) -> Profile:
        current = self.current_profile()
        if current is None or self.capture is None:
            raise ValueError('作品を選択してください')
        updated = self.capture.committed_profile(current)
        game = self.catalog.games[self.game_id]
        previous = game.profiles[current.id]
        game.profiles[updated.id] = updated
        try:
            self._persist()
        except Exception:
            game.profiles[current.id] = previous
            raise
        self.capture.mark_committed(updated)
        self._reconcile_hook_sync()
        self._apply_persisted_profile_change(previous, updated)
        if self.runtime_state().profile is None:
            self._reconcile_monitoring(updated)
        return updated

    def _update_profile(self, mutate: Callable[[Profile], None]) -> Profile:
        current = self.current_profile()
        if current is None:
            raise ValueError('作品を選択してください')
        updated = deepcopy(current)
        mutate(updated)
        capture_transaction_fields = ('hooks', 'body_source', 'speaker_source', 'source_rules', 'unwrap_speaker_enclosed_body', 'inserted_hooks')
        changed_capture_fields = [
            field for field in capture_transaction_fields
            if getattr(updated, field) != getattr(current, field)
        ]
        if changed_capture_fields:
            raise ValueError(
                'capture recipe changes require the Capture Workspace transaction: ' +
                ', '.join(changed_capture_fields))
        updated.validate()
        game = self.catalog.games[self.game_id]
        previous = game.profiles[current.id]
        game.profiles[updated.id] = updated
        try:
            self._persist()
        except Exception:
            game.profiles[current.id] = previous
            raise
        self._apply_persisted_profile_change(previous, updated)
        return updated

    def set_capture_environment(self, *, ocr_executable: str, ocr_language: str,
                                ocr_region: tuple[int, int, int, int], ocr_stable_samples: int,
                                insert_pc_hooks: bool, hook_flush_ms: int, hook_codepage: int,
                                hook_max_buffer: int, hook_max_history: int) -> Profile:
        """Persist the acquisition environment as one user-meaningful command."""
        ocr = OCRSettings(ocr_executable.strip(), ocr_language.strip(), tuple(ocr_region),
                          stable_samples=ocr_stable_samples)

        def change(profile: Profile) -> None:
            profile.ocr = ocr
            profile.insert_pc_hooks = bool(insert_pc_hooks)
            profile.hook_flush_ms = int(hook_flush_ms)
            profile.hook_codepage = int(hook_codepage)
            profile.hook_max_buffer = int(hook_max_buffer)
            profile.hook_max_history = int(hook_max_history)

        return self._update_profile(change)

    def add_speaker(self, name: str, tts_enabled: bool, voice: str = '') -> Profile:
        name = name.strip()
        if not name:
            raise ValueError('話者名を入力してください')
        current = self.current_profile()
        if current is None:
            raise ValueError('作品を選択してください')
        if name in set(current.speaker_tts) | set(current.speaker_voices):
            raise ValueError('同名の話者が既にあります')

        def change(profile: Profile) -> None:
            profile.speaker_tts[name] = bool(tts_enabled)
            profile.speaker_voices[name] = voice.strip()

        return self._update_profile(change)

    def rename_speaker(self, old_name: str, new_name: str) -> Profile:
        old_name, new_name = old_name.strip(), new_name.strip()
        if not old_name or not new_name:
            raise ValueError('話者名を入力してください')
        current = self.current_profile()
        if current is None:
            raise ValueError('作品を選択してください')
        existing = set(current.speaker_tts) | set(current.speaker_voices)
        if old_name not in existing:
            raise ValueError('変更する話者がありません')
        if new_name != old_name and new_name in existing:
            raise ValueError('同名の話者が既にあります')

        def change(profile: Profile) -> None:
            if old_name in profile.speaker_tts:
                profile.speaker_tts[new_name] = profile.speaker_tts.pop(old_name)
            if old_name in profile.speaker_voices:
                profile.speaker_voices[new_name] = profile.speaker_voices.pop(old_name)

        return self._update_profile(change)

    def set_speaker_tts(self, name: str, enabled: bool) -> Profile:
        name = name.strip()
        current = self.current_profile()
        if current is None or name not in set(current.speaker_tts) | set(current.speaker_voices):
            raise ValueError('変更する話者がありません')
        return self._update_profile(lambda profile: profile.speaker_tts.__setitem__(name, bool(enabled)))

    def set_speaker_voice(self, name: str, voice: str) -> Profile:
        name = name.strip()
        current = self.current_profile()
        if current is None or name not in set(current.speaker_tts) | set(current.speaker_voices):
            raise ValueError('変更する話者がありません')
        return self._update_profile(lambda profile: profile.speaker_voices.__setitem__(name, voice.strip()))

    def delete_speaker(self, name: str) -> Profile:
        name = name.strip()

        def change(profile: Profile) -> None:
            profile.speaker_tts.pop(name, None)
            profile.speaker_voices.pop(name, None)

        return self._update_profile(change)

    def save_replacement_rule(self, index: int | None, rule: ReplacementRule) -> Profile:
        def change(profile: Profile) -> None:
            if index is not None and 0 <= index < len(profile.replacements):
                profile.replacements[index] = deepcopy(rule)
            else:
                profile.replacements.append(deepcopy(rule))

        return self._update_profile(change)

    def move_replacement_rule(self, index: int, movement: int) -> Profile:
        current = self.current_profile()
        if current is None or not 0 <= index < len(current.replacements):
            raise ValueError('変更する置換規則を選択してください')
        destination = index + movement
        if not 0 <= destination < len(current.replacements):
            return current

        def change(profile: Profile) -> None:
            profile.replacements[index], profile.replacements[destination] = (
                profile.replacements[destination], profile.replacements[index])

        return self._update_profile(change)

    def delete_replacement_rule(self, index: int) -> Profile:
        current = self.current_profile()
        if current is None or not 0 <= index < len(current.replacements):
            raise ValueError('削除する置換規則を選択してください')
        return self._update_profile(lambda profile: profile.replacements.pop(index))

    def add_reading(self, word: str, reading: str) -> Profile:
        word, reading = word.strip(), reading.strip()
        if not word or not reading:
            raise ValueError('表記と読みの両方を入力してください')
        current = self.current_profile()
        if current is None:
            raise ValueError('作品を選択してください')
        if word in current.name_readings:
            raise ValueError('同じ表記が既にあります')
        return self._update_profile(lambda profile: profile.name_readings.__setitem__(word, reading))

    def update_reading(self, old_word: str, word: str, reading: str) -> Profile:
        old_word, word, reading = old_word.strip(), word.strip(), reading.strip()
        if not word or not reading:
            raise ValueError('表記と読みの両方を入力してください')
        current = self.current_profile()
        if current is None or old_word not in current.name_readings:
            raise ValueError('変更する読みがありません')
        if word != old_word and word in current.name_readings:
            raise ValueError('同じ表記が既にあります')

        def change(profile: Profile) -> None:
            if old_word != word:
                profile.name_readings.pop(old_word, None)
            profile.name_readings[word] = reading

        return self._update_profile(change)

    def delete_reading(self, word: str) -> Profile:
        return self._update_profile(lambda profile: profile.name_readings.pop(word, None))

    def set_profile_setting(self, name: str, value) -> Profile:
        """Persist one user-visible setting without reconstructing a form.

        Editing a setting that currently inherits the common default is the
        user's decision to create a game override.  The persistence shape stays
        internal to the application; the GUI never toggles an inheritance field.
        """
        if (name not in Profile.__dataclass_fields__ or
                name in {'id', 'name', 'hooks', 'body_source', 'speaker_source', 'source_rules', 'unwrap_speaker_enclosed_body', 'inserted_hooks'}):
            raise ValueError('unsupported profile setting')

        def change(profile: Profile) -> None:
            setattr(profile, name, deepcopy(value))
            if name in profile.inherit_common:
                profile.inherit_common.remove(name)

        return self._update_profile(change)

    def reset_profile_setting(self, name: str) -> Profile:
        """Remove a game override and use the current common value."""
        if name not in self.catalog.common.__dataclass_fields__:
            raise ValueError('setting has no common default')

        def change(profile: Profile) -> None:
            if name not in profile.inherit_common:
                profile.inherit_common.append(name)

        return self._update_profile(change)

    def effective_setting(self, name: str):
        profile = self.current_profile()
        if profile is None:
            raise ValueError('作品を選択してください')
        effective = self.catalog.effective_profile(profile)
        return deepcopy(getattr(effective, name)), ('common' if name in profile.inherit_common else 'game')

    def update_game_executables(self, executable: str, audio_executable: str,
                                input_executable: str) -> None:
        game = self.catalog.games.get(self.game_id)
        if game is None:
            raise ValueError('作品を選択してください')
        previous = (game.executable, game.audio_executable, game.input_executable)
        game.executable = executable.strip()
        game.audio_executable = audio_executable.strip()
        game.input_executable = input_executable.strip()
        try:
            self._persist()
        except Exception:
            game.executable, game.audio_executable, game.input_executable = previous
            raise

    def set_common_setting(self, name: str, value) -> None:
        if name not in self.catalog.common.__dataclass_fields__:
            raise ValueError('unsupported common setting')
        current_profile = self.current_profile()
        previous_effective = (self.catalog.effective_profile(current_profile)
                              if current_profile and name in current_profile.inherit_common else None)
        previous = deepcopy(self.catalog.common)
        setattr(self.catalog.common, name, deepcopy(value))
        try:
            self.catalog.common.validate()
            self._persist()
        except Exception:
            self.catalog.common = previous
            raise
        if current_profile and previous_effective is not None:
            updated_effective = self.catalog.effective_profile(current_profile)
            # The raw profile did not change, but the effective runtime contract did.
            state = self.runtime_state()
            if state.profile is not None and state.profile.id == current_profile.id:
                if self._runtime_capture_matches(updated_effective):
                    self.controller.stage_profile(updated_effective)
                    self._notify_runtime()
                else:
                    self._request_capture_transition()

    def set_irodori_environment(self, root: str, url: str) -> None:
        previous = (self.catalog.irodori_root, self.catalog.irodori_url)
        self.catalog.irodori_root = root.strip()
        self.catalog.irodori_url = url.strip()
        try:
            self._persist()
        except Exception:
            self.catalog.irodori_root, self.catalog.irodori_url = previous
            raise

