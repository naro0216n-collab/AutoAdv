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
from .hook_host import HookHost
from .luna_runtime import adopted_runtime
from .hook_protocol import HookEvent, ThreadSource
from .irodori_server import IrodoriServer
from .model import (HookSelection, OCRSettings, Profile, ReplacementRule, StructureRule,
                    Utterance, EXTRACTION_PRESETS, extract_source_fields, interpret)
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
    native_playing: bool
    ocr_blank: bool
    source_generation: str
    connection_generation: int
    active_body_source: str | None
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


_PLAY_ISSUES = {
    'target_unavailable': ('自動送り先のゲームウィンドウを確認できないため、この台詞の自動送りを保留しています。', None),
    'next_line_timeout': ('送り後に次の台詞を取得できません。会話取得を確認してください。', 'open_capture'),
    'ambiguous_identical_line': ('同じ本文が続き、次の台詞か判断できないため自動送りを保留しています。', 'open_capture'),
    'ambiguous_multihook_lines': ('同じ本文取得元から複数候補が届き、対応を確定できないため自動送りを保留しています。', 'open_capture'),
    'ambiguous_multihook_speakers': ('同じ話者取得元から複数候補が届き、対応を確定できないため自動送りを保留しています。', 'open_capture'),
    'ocr_unverified_change': ('OCRの変化を次の台詞と確認できないため自動送りを保留しています。', 'open_capture'),
    'ocr_failure': ('OCRで台詞を取得できないため、自動送りを保留しています。', 'open_capture'),
    'native_inconclusive_hold': ('原音の終了を確認できないため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
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


class CaptureWorkspace:
    """Draft one field-based conversation capture recipe against real output."""

    def __init__(self, profile: Profile, resolve: Callable[[Profile], Profile] | None = None):
        self._base = deepcopy(profile)
        self._draft = deepcopy(profile)
        self._resolve = resolve or (lambda value: value)
        self._observed: dict[tuple[str, int], _ObservedSource] = {}
        self._groups: deque[tuple[HookEvent, ...]] = deque(maxlen=16)

    @property
    def profile(self) -> Profile:
        return self._draft

    @property
    def dirty(self) -> bool:
        return (self._draft.hooks != self._base.hooks or
                self._draft.body_sources != self._base.body_sources or
                self._draft.speaker_sources != self._base.speaker_sources or
                self._draft.source_rules != self._base.source_rules or
                self._draft.inserted_hooks != self._base.inserted_hooks)

    def reset(self, profile: Profile) -> None:
        self._base = deepcopy(profile)
        self._draft = deepcopy(profile)
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
        value = text.strip()
        if value and (not record.history or record.history[-1][1] != value):
            record.history.append((sequence, value))

    def observe_group(self, events: Sequence[HookEvent]) -> None:
        outputs = tuple(sorted(
            (event for event in events if event.kind == 'output' and event.source is not None and event.text.strip()),
            key=lambda event: event.sequence))
        if outputs:
            self._groups.append(outputs)

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

    def has_identity(self, identity: str) -> bool:
        return any(candidate_identity == identity for candidate_identity, _ in self._observed)

    def has_output(self, identity: str) -> bool:
        record = self._record_for_identity(identity)
        return bool(record and record.history)

    def output_identities(self) -> tuple[str, ...]:
        identities = []
        for identity in self._draft.body_sources:
            if identity not in identities and self.has_output(identity):
                identities.append(identity)
        return tuple(identities)

    def has_hook_code(self, code: str) -> bool:
        return any(record.source.hook_code == code for record in self._observed.values())

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
            extract_source_fields(raw, self._draft, identity))) for raw in history)
        used_fields = []
        if identity in self._draft.body_sources:
            used_fields.append('body')
        if identity in self._draft.speaker_sources or (not self._draft.speaker_sources and
                                                        identity in self._draft.body_sources):
            used_fields.append('speaker')
        used = tuple(used_fields)
        return CaptureCandidateView(identity, address, history[-1] if history else '', history,
                                    samples, used, self._preset_for(identity), record.ambiguous)

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
        providers = self._draft.body_sources if field == 'body' else self._draft.speaker_sources
        providers.clear()
        if identity is None:
            return
        if address is None:
            record = self._record_for_identity(identity)
            if record is None:
                raise ValueError('取得元を利用できません')
            source = record.source
        else:
            source = self._source(identity, address)
        hook = self._ensure_hook(source)
        providers.append(hook.identity)

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
        configured = self._draft.source_rules.get(identity)
        return tuple(configured) if configured is not None else tuple(EXTRACTION_PRESETS['auto'])

    def _record_for_identity(self, identity: str) -> _ObservedSource | None:
        matches = [record for (candidate_identity, _), record in self._observed.items()
                   if candidate_identity == identity]
        return matches[0] if len(matches) == 1 and not matches[0].ambiguous else None

    def _assembly_from_events(self, events: Sequence[HookEvent]) -> CaptureAssemblyView | None:
        if not self._draft.body_sources:
            return None
        body_identity = self._draft.body_sources[0]
        body_events = [event for event in events if event.source and
                       event.source.identity == body_identity and event.text.strip()]
        body_values = [(event, extract_source_fields(event.text, self._draft, body_identity))
                       for event in body_events]
        body_values = [(event, fields) for event, fields in body_values if fields.body]
        if len(body_values) != 1:
            return None
        body_event, body_fields = body_values[0]
        speaker_source: str | None = None
        speaker = ''
        if self._draft.speaker_sources:
            speaker_source = self._draft.speaker_sources[0]
            speaker_events = [event for event in events if event.source and
                              event.source.identity == speaker_source and event.text.strip()]
            values = [(event, extract_source_fields(event.text, self._draft, speaker_source))
                      for event in speaker_events]
            values = [(event, fields) for event, fields in values if fields.speaker]
            if len(values) > 1:
                return None
            if values:
                speaker = values[0][1].speaker
        else:
            speaker = body_fields.speaker
            speaker_source = body_identity if speaker else None
        return CaptureAssemblyView(body_event.sequence, speaker, body_fields.body,
                                   body_identity, speaker_source)

    def _assembly(self) -> tuple[CaptureAssemblyView, ...]:
        if not self._draft.body_sources:
            return ()
        body_identity = self._draft.body_sources[0]
        speaker_identity = self._draft.speaker_sources[0] if self._draft.speaker_sources else body_identity
        assembled: list[CaptureAssemblyView] = []
        if speaker_identity != body_identity:
            for group in self._groups:
                value = self._assembly_from_events(group)
                if value is not None:
                    assembled.append(value)
        else:
            record = self._record_for_identity(body_identity)
            if record is not None:
                for sequence, raw in record.history:
                    fields = extract_source_fields(raw, self._draft, body_identity)
                    if fields.body:
                        speaker = fields.speaker if (not self._draft.speaker_sources or
                                  body_identity in self._draft.speaker_sources) else ''
                        assembled.append(CaptureAssemblyView(
                            sequence, speaker, fields.body, body_identity,
                            body_identity if speaker else None))
        assembled.sort(key=lambda item: item.sequence)
        return tuple(assembled[-12:])

    def view(self) -> CaptureWorkspaceView:
        candidates = []
        for (_, address), record in self._observed.items():
            if not record.history:
                continue
            candidate = self.candidate(record.source.identity, address)
            if candidate is not None:
                candidates.append(candidate)
        providers = set(self._draft.body_sources) | set(self._draft.speaker_sources)
        ambiguous_selected = any(
            candidate.ambiguous and candidate.identity in providers for candidate in candidates)
        problem = None
        if ambiguous_selected:
            problem = CaptureProblemView('選択した取得元を一意に識別できません。別の取得元を選択してください。')
        can_commit = bool(self._draft.body_sources) and not ambiguous_selected
        return CaptureWorkspaceView(
            candidates=tuple(candidates), assembly=self._assembly(),
            body_source=self._draft.body_sources[0] if self._draft.body_sources else None,
            speaker_source=self._draft.speaker_sources[0] if self._draft.speaker_sources else None,
            dirty=self.dirty, can_commit=can_commit, problem=problem)

    def adopt_inserted_hook(self, code: str) -> None:
        code = code.strip()
        if code and code not in self._draft.inserted_hooks:
            self._draft.inserted_hooks.append(code)

    def mutate_recipe(self, mutate: Callable[[Profile], None]) -> None:
        prior = deepcopy(self._draft)
        try:
            mutate(self._draft)
            self._draft.validate()
        except Exception:
            self._draft = prior
            raise

    def rebase_settings(self, profile: Profile) -> None:
        """Refresh non-capture settings without discarding a capture draft."""
        capture_fields = {'hooks', 'body_sources', 'speaker_sources', 'source_rules', 'inserted_hooks'}
        for field in profile.__dataclass_fields__:
            if field in capture_fields or field in ('id', 'name'):
                continue
            setattr(self._base, field, deepcopy(getattr(profile, field)))
            setattr(self._draft, field, deepcopy(getattr(profile, field)))

    def committed_profile(self) -> Profile:
        if not self.view().can_commit:
            raise ValueError('本文として使用する取得元を選択してください')
        candidate = deepcopy(self._draft)
        used = set(candidate.body_sources) | set(candidate.speaker_sources)
        candidate.hooks = [hook for hook in candidate.hooks if hook.identity in used]
        candidate.source_rules = {identity: rules for identity, rules in candidate.source_rules.items()
                                  if identity in used or identity == 'ocr'}
        # Recovery-search insertions are provisional until a source backed by
        # that hook code is part of the committed assembly.  Keep codes that
        # already existed before this workspace, but never persist a newly
        # trialled code merely because it produced one sample during editing.
        base_codes = set(self._base.inserted_hooks)
        used_codes = {hook.code for hook in candidate.hooks}
        candidate.inserted_hooks = [
            code for code in candidate.inserted_hooks
            if code in base_codes or code in used_codes
        ]
        candidate.validate()
        return candidate

    def mark_committed(self, profile: Profile) -> None:
        self._base = deepcopy(profile)
        self._draft = deepcopy(profile)


class AutoAdvApplication:
    """Application facade for user-level configuration actions.

    The application owns controller and LunaHost lifecycles.  The UI never
    reconstructs a Profile from widget types, edits capture provider storage
    directly, or translates Controller reasons into user-facing status text.
    """

    def __init__(self, catalog: Catalog, persist: Callable[[], None], *,
                 input_send: Callable | None = None, target_ready: Callable | None = None):
        self.catalog = catalog
        self._persist = persist
        self.game_id = ''
        self.profile_id = ''
        self.capture: CaptureWorkspace | None = None
        self._runtime_capture_problem: CaptureProblemView | None = None
        self._raw_input_send = input_send or self._windows_input_send
        self._input_observer: Callable[[str, Token | None], None] | None = None
        self.controller = Controller(self._send_runtime_input, target_ready or self._windows_target_ready)
        self.hook_host: HookHost | None = None
        self._host_epoch = 0
        self._irodori_server: IrodoriServer | None = None
        self._irodori_epoch = 0
        self._native_audio: NativeAudio | None = None
        self._native_audio_epoch = 0
        self._ocr_worker: OCRWorker | None = None
        self._ocr_epoch = 0
        self._voice_jobs: VoiceJobs | None = None
        self._preview_jobs: VoiceJobs | None = None
        self._searched_hook_codes: set[str] = set()
        self._trial_hook_codes: set[str] = set()
        self._capture_output_group: list[HookEvent] = []
        self._runtime_output_group: list[HookEvent] = []
        self._synced_sources: dict[tuple[int, int, int, int], ThreadSource] = {}
        self._hook_connection = HookConnectionState()
        self._runtime_target = RuntimeTargetState()
        self._inserted_hook_codes: set[str] = set()
        self._pc_hooks_inserted = False
        self._resume_saved_monitor = False
        self._capture_transition_pending = False
        self._schedule_runtime: Callable[[int, Callable[[], None]], None] | None = None
        self._runtime_observer: Callable[[], None] | None = None
        self._runtime_event_observer: Callable[[str, Token | None, dict], None] | None = None
        self._ocr_text_observer: Callable[[int, int, int, str], None] | None = None
        self._ocr_error_observer: Callable[[str], None] | None = None
        self._native_ready_observer: Callable[[int, bool, str], None] | None = None
        self._native_event_observer: Callable[[Token | None, str, str], None] | None = None
        self._native_pcm_observer: Callable[[Token, int], None] | None = None
        self._native_inference_observer: Callable[[Token, dict], None] | None = None
        self._native_voice_end_observer: Callable[[Token, dict], None] | None = None
        self._hook_event_observer: Callable[[HookEvent], None] | None = None
        self._hook_closed_observer: Callable[[str], None] | None = None
        self._hook_error_observer: Callable[[str], None] | None = None

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

    def configure_input_observer(self, observer: Callable[[str, Token | None], None] | None) -> None:
        """Expose diagnostic milestones without giving the GUI input authority."""
        self._input_observer = observer

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

    def configure_runtime_services(self, *,
                                   on_ocr_text: Callable[[int, int, int, str], None] | None = None,
                                   on_ocr_error: Callable[[str], None] | None = None,
                                   on_native_ready: Callable[[int, bool, str], None] | None = None,
                                   on_native_event: Callable[[Token | None, str, str], None] | None = None,
                                   on_native_pcm: Callable[[Token, int], None] | None = None,
                                   on_native_inference: Callable[[Token, dict], None] | None = None,
                                   on_native_voice_end: Callable[[Token, dict], None] | None = None) -> None:
        """Bind observation adapters for runtime services once."""
        self._ocr_text_observer = on_ocr_text
        self._ocr_error_observer = on_ocr_error
        self._native_ready_observer = on_native_ready
        self._native_event_observer = on_native_event
        self._native_pcm_observer = on_native_pcm
        self._native_inference_observer = on_native_inference
        self._native_voice_end_observer = on_native_voice_end

    def configure_hook_service(self, *, on_event: Callable[[HookEvent], None],
                               on_closed: Callable[[str], None],
                               on_error: Callable[[str], None]) -> None:
        """Bind Host observation adapters; Application retains Host lifecycle policy."""
        self._hook_event_observer = on_event
        self._hook_closed_observer = on_closed
        self._hook_error_observer = on_error

    def _notify_runtime(self) -> None:
        if self._runtime_observer is not None:
            self._runtime_observer()

    def _emit_runtime_event(self, event: str, token: Token | None = None, **detail) -> None:
        if self._runtime_event_observer is not None:
            self._runtime_event_observer(event, token, detail)

    def _schedule(self, delay_ms: int, callback: Callable[[], None]) -> None:
        if self._schedule_runtime is None:
            raise RuntimeError('runtime scheduler is not configured')
        self._schedule_runtime(max(0, int(delay_ms)), callback)

    def _send_runtime_input(self, target: Target, virtual_key: int) -> None:
        token = self.controller.token
        if self._input_observer is not None:
            self._input_observer('attempt', token)
        if self._native_audio is not None:
            self._native_audio.boundary()
            if self._input_observer is not None:
                self._input_observer('audio_boundary', token)
        self._raw_input_send(target, virtual_key)
        if self._input_observer is not None:
            self._input_observer('sent', token)

    def runtime_state(self) -> RuntimeState:
        c = self.controller
        return RuntimeState(
            session=c.session, token=c.token, target=c.target, profile=c.profile,
            utterance=c.utterance, authorized=c.authorized, sent=c.sent,
            completed=c.completed, native_playing=c.native_playing, ocr_blank=c.ocr_blank,
            source_generation=c.source_generation, connection_generation=c.connection_generation,
            active_body_source=c.active_body_source,
            presentation_reason=c.presentation_reason, pause_reason=c.pause_reason)

    def stage_runtime_profile(self, profile: Profile) -> None:
        self.controller.stage_profile(profile)

    def runtime_capture_matches(self, profile: Profile) -> bool:
        active = self.runtime_state().profile
        if active is None or active.id != profile.id:
            return False
        if not (active.source == profile.source and active.hooks == profile.hooks and
                active.body_sources == profile.body_sources and
                active.speaker_sources == profile.speaker_sources and
                active.source_rules == profile.source_rules and
                not (profile.source == 'ocr' and active.ocr != profile.ocr)):
            return False
        if active.advance_mode != profile.advance_mode:
            return False
        if active.advance_mode.value == 'audio' or profile.advance_mode.value == 'audio':
            return (active.native_helper == profile.native_helper and
                    active.asr_model == profile.asr_model and
                    active.asr_tokens == profile.asr_tokens)
        return True

    def _request_capture_transition(self) -> None:
        if self.defer_capture_transition():
            self._schedule(0, self.apply_capture_transition)

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
                self.restore_profile_capture(updated)
                self.clear_capture_problem()
            except Exception as exc:
                self.report_capture_problem(
                    '保存した取得設定を現在のゲームへ反映できません。取得の復旧を確認してください。')
                self._emit_runtime_event('capture_environment_apply_failed', None, reason=str(exc))
        state = self.runtime_state()
        if state.profile is not None and state.profile.id == updated.id:
            if self.runtime_capture_matches(effective):
                self.controller.stage_profile(effective)
                self._notify_runtime()
            else:
                self._request_capture_transition()

    def hold_runtime(self, reason: str) -> None:
        self.controller.pause(reason)

    def begin_runtime(self, profile: Profile, target: Target, generation: str, connection: int,
                      available_sources: tuple[str, ...] = ()) -> int:
        return self.controller.begin(profile, target, generation, connection, available_sources)

    def observe_runtime_hook(self, **kwargs) -> Token | None:
        return self.controller.observe(**kwargs)

    def observe_runtime_group(self, events: Sequence[HookEvent]) -> Token | None:
        return self.controller.observe_group(events)

    def observe_runtime_ocr(self, **kwargs) -> Token | None:
        return self.controller.observe_ocr(**kwargs)

    def take_external_transition(self, token: Token) -> int:
        return self.controller.take_external_transition(token)

    def _attempt_advance(self) -> bool:
        sent = self.controller.try_advance()
        state = self.runtime_state()
        if sent and state.token is not None and state.profile is not None:
            token = state.token
            if self._capture_transition_pending:
                self._schedule(0, self.apply_capture_transition)
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

    def expire_next_line(self, token: Token) -> bool:
        return self._expire_next_line(token)

    def runtime_presentation_event(self, token: Token, event: str, *, success: bool = True) -> bool:
        completed = self.controller.presentation_event(token, event, success=success)
        if completed:
            self._emit_runtime_event('presentation_complete', token,
                                     reason=self.controller.presentation_reason)
            self._attempt_advance()
        else:
            self._notify_runtime()
        return completed

    def try_advance(self) -> bool:
        return self._attempt_advance()

    def set_auto_advance(self, enabled: bool) -> None:
        self.controller.permit(enabled)
        if enabled:
            self._attempt_advance()
        else:
            self._notify_runtime()

    def _fallback_due(self, token: Token) -> None:
        state = self.runtime_state()
        utterance = state.utterance
        if (token != state.token or utterance is None or utterance.use_tts or
                utterance.advance_mode.value != 'audio' or state.completed or state.sent):
            return
        if self.native_audio_holds_deadline(token):
            self._schedule(200, lambda t=token: self._fallback_due(t))
            return
        if state.native_playing:
            self.runtime_presentation_event(token, 'native_evidence_stale')
        self.runtime_presentation_event(token, 'fallback_elapsed')

    def start_presentation(self, token: Token) -> int:
        """Start every presentation effect for the current immutable Utterance.

        The event-loop adapter does not decide whether text time, native audio,
        TTS playback or uncertain fallback governs completion.
        """
        state = self.runtime_state()
        utterance = state.utterance
        if utterance is None or token != state.token:
            return 0
        self.cancel_voice()
        self.cancel_native_audio()
        external_ns = self.controller.take_external_transition(token)
        if external_ns:
            self._emit_runtime_event('external_hook_transition', token, received_ns=external_ns)
        if utterance.advance_mode.value == 'text':
            self._schedule(utterance.text_wait_ms,
                           lambda t=token: self.runtime_presentation_event(t, 'text_elapsed'))
        elif not utterance.use_tts:
            if self.native_audio_available:
                replay_from_ms = None
                if external_ns and state.profile is not None:
                    lookback_ms = max(100, state.profile.hook_flush_ms + 100)
                    replay_from_ms = max(0, external_ns // 1_000_000 - lookback_ms)
                self.begin_native_audio(token, utterance.match_text, replay_from_ms=replay_from_ms)
            self._schedule(utterance.text_wait_ms, lambda t=token: self._fallback_due(t))
        if utterance.use_tts:
            if not utterance.voice:
                self.runtime_presentation_event(token, 'tts_finished', success=False)
            else:
                self.start_voice(token, utterance.reading, utterance.voice)
        self._notify_runtime()
        return external_ns

    def close_runtime(self, reason: str = 'stopped') -> None:
        self._runtime_output_group.clear()
        self.controller.close(reason)
        self._notify_runtime()

    def start_monitoring(self) -> RuntimeState:
        """Build the selected profile's one runtime path and its services."""
        profile = self.current_profile()
        if profile is None:
            raise ValueError('作品とプロファイルを選択してください')
        effective = self.catalog.effective_profile(profile)
        target = self.session_target()
        self.stop_monitoring('session_changed')
        if effective.source == 'ocr':
            self.begin_runtime(effective, target, 'ocr', 1)
            self.start_ocr(
                effective,
                on_text=self._ocr_text_observer or (lambda *_: None),
                on_error=self._ocr_error_observer or (lambda *_: None))
        else:
            if any(self.capture_identity_is_ambiguous(h.identity) for h in effective.hooks):
                raise ValueError('選択した取得元を一意に識別できません。別の取得元を選択してください')
            connection = self._hook_connection
            if not connection.connected:
                raise RuntimeError('ゲームへのLunaHook接続が完了していません')
            self.begin_runtime(effective, target, connection.host_generation,
                               connection.connection_generation, self.capture_output_identities())
            self._reconcile_hook_sync()
        self.start_native_audio(
            effective,
            on_ready=self._native_ready_observer or (lambda *_: None),
            on_event=self._native_event_observer or (lambda *_: None),
            on_pcm=self._native_pcm_observer,
            on_inference=self._native_inference_observer,
            on_voice_end=self._native_voice_end_observer)
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
            self.sync_hook_threads(target.hook_pid, clear=True)
        self.close_native_audio()
        self.close_ocr()
        self.cancel_voice()
        self.cancel_voice_preview()
        self.close_runtime(reason)

    def request_saved_monitor_resume(self, profile: Profile | None) -> None:
        self._resume_saved_monitor = bool(
            profile and profile.source == 'hook' and bool(profile.body_sources))

    def cancel_saved_monitor_resume(self) -> None:
        self._resume_saved_monitor = False

    def saved_monitor_resume_ready(self, profile: Profile | None) -> bool:
        if (not self._resume_saved_monitor or profile is None or
                not self._hook_connection.connected or not self.capture_saved_sources_ready(profile)):
            return False
        self._resume_saved_monitor = False
        return True

    def try_resume_saved_monitor(self, profile: Profile | None = None) -> RuntimeState | None:
        profile = profile or self.current_profile()
        if not self.saved_monitor_resume_ready(profile):
            return None
        return self.start_monitoring()

    def activate_selected_target(self) -> str:
        """Apply the selected game target without exposing connection mechanics to UI."""
        profile = self.current_profile()
        if profile is None:
            raise ValueError('作品を登録してください')
        target = self._runtime_target
        if not target.selected:
            raise ValueError('実行中のゲーム・プロセスを選択してください')
        self.request_saved_monitor_resume(profile)
        if profile.source == 'ocr':
            if target.input_ready:
                self.start_monitoring()
                return 'monitoring'
            return 'waiting-input-target'

        pid = target.hook_pid
        connection = self._hook_connection
        if self.hook_host_running and (connection.connected_to(pid) or connection.connecting_pid == pid):
            if connection.connected_to(pid):
                self.restore_profile_capture(profile)
                if self.try_resume_saved_monitor(profile) is not None:
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
            self.report_capture_problem('ゲームから台詞を取得するためのLunaHookを起動できません。')
            raise RuntimeError('LunaHook Hostの起動に失敗しました')
        if self.begin_hook_connection(pid, profile):
            return 'connecting'
        if self.try_resume_saved_monitor(profile) is not None:
            return 'monitoring'
        return 'connected'

    def handle_hook_connection_event(self, event: HookEvent) -> str | None:
        """Apply connection provenance, profile restoration and monitor resume."""
        transition = self.accept_hook_connection_event(event)
        if transition in ('connected', 'changed'):
            if transition == 'changed':
                self.stop_monitoring('host_changed')
                self.clear_capture_observations()
            self.clear_capture_problem()
            profile = self.current_profile()
            if profile and profile.source == 'hook':
                try:
                    self.restore_profile_capture(profile)
                except Exception as exc:
                    self.report_capture_problem('保存した会話取得設定を現在のゲームへ復元できません。')
                    self._emit_runtime_event('capture_environment_apply_failed', None, reason=str(exc))
                    return 'restore_failed'
                self.try_resume_saved_monitor(profile)
        elif transition == 'disconnected':
            self.stop_monitoring('host_changed')
            self.clear_capture_observations()
            self.cancel_saved_monitor_resume()
            self.report_capture_problem('ゲームからの台詞取得が切断されました。')
        return transition

    @property
    def capture_transition_pending(self) -> bool:
        return self._capture_transition_pending

    def defer_capture_transition(self) -> bool:
        """Hold input and remember that capture/runtime services need rebuilding.

        Returns True when no current unsent utterance needs to be preserved and
        the transition can be scheduled immediately by the event-loop adapter.
        """
        self.controller.pause('settings_changed')
        self._capture_transition_pending = True
        state = self.runtime_state()
        return state.utterance is None or state.sent

    def capture_transition_boundary(self, event: HookEvent, profile: Profile | None) -> bool:
        if not self._capture_transition_pending or not profile or profile.source != 'hook':
            return False
        if event.kind != 'output' or event.source is None or not event.text.strip():
            return False
        selected = next((h for h in profile.hooks if h.identity in profile.body_sources and h.matches(event.source)), None)
        if selected is None:
            return False
        state = self.runtime_state()
        return state.utterance is None or state.sent or event.text != state.utterance.raw

    def consume_capture_transition(self) -> bool:
        if not self._capture_transition_pending:
            return False
        self._capture_transition_pending = False
        return True

    def apply_capture_transition(self) -> bool:
        """Rebuild the active acquisition/runtime path at one safe dialogue boundary."""
        if not self.consume_capture_transition():
            return False
        profile = self.current_profile()
        if profile is None:
            self.stop_monitoring('session_changed')
            return False
        if profile.source == 'hook':
            if not profile.body_sources:
                self.stop_monitoring('settings_changed')
                return False
            if not self._hook_connection.connected:
                self.stop_monitoring('host_changed')
                self.request_saved_monitor_resume(profile)
                return False
        self.start_monitoring()
        return True

    def handle_runtime_ocr_output(self, *, session: int, pid: int, sequence: int, text: str) -> Token | None:
        """Own OCR boundary transitions before admitting text to Controller."""
        if self._capture_transition_pending and text.strip():
            state = self.runtime_state()
            if state.utterance is None or state.sent or state.ocr_blank:
                # The replacement OCR worker will observe the still-visible line.
                self.apply_capture_transition()
                return None
        return self.controller.observe_ocr(session=session, pid=pid, sequence=sequence, text=text)

    def handle_runtime_hook_output(self, event: HookEvent) -> tuple[Token | None, int | None]:
        """Feed one Hook output or queue the selected multi-source batch.

        The returned delay is only a scheduling request for the UI/event loop;
        ownership of the batch and the grouping policy remain in Application.
        """
        profile = self.current_profile()
        if self.capture_transition_boundary(event, profile):
            # Rebuild synchronously on this HookEvent so the same first output is
            # admitted to the new runtime instead of being discarded.
            self.apply_capture_transition()
        state = self.runtime_state()
        profile, target = state.profile, state.target
        if (event.kind != 'output' or event.source is None or profile is None or target is None or
                profile.source != 'hook' or event.process_id != (target.hook_pid or target.pid) or
                event.host_generation != state.source_generation or
                event.connection_generation != state.connection_generation):
            return None, None
        selected = profile.hooks
        if len(selected) > 1 and any(h.matches(event.source) for h in selected):
            self._runtime_output_group.append(event)
            if len(self._runtime_output_group) >= 32:
                return self.flush_runtime_hook_outputs(), None
            return None, max(10, min(100, profile.hook_flush_ms))
        return (self.controller.observe(
            generation=event.host_generation, connection=event.connection_generation,
            sequence=event.sequence, source=event.source, text=event.text,
            pid=event.process_id or 0, received_ns=event.received_ns), None)

    def flush_runtime_hook_outputs(self) -> Token | None:
        group, self._runtime_output_group = self._runtime_output_group, []
        return self.controller.observe_group(group) if group and self.controller.profile else None

    @property
    def native_audio_available(self) -> bool:
        return self._native_audio is not None

    @property
    def native_audio_pid(self) -> int:
        return self._native_audio.pid if self._native_audio is not None else 0

    def start_native_audio(self, profile: Profile, *, on_ready: Callable[[int, bool, str], None],
                           on_event: Callable[[Token | None, str, str], None],
                           on_pcm: Callable[[Token, int], None] | None = None,
                           on_inference: Callable[[Token, dict], None] | None = None,
                           on_voice_end: Callable[[Token, dict], None] | None = None) -> bool:
        """Own process audio capture for the active runtime session.

        Loading is asynchronous because ASR model construction can be slow.  A
        later session/close invalidates the worker by epoch, so a late helper can
        never become the active audio source for another game or utterance.
        """
        self.close_native_audio()
        if (profile.advance_mode.value != 'audio' or not profile.native_helper or
                not profile.asr_model or not profile.asr_tokens):
            return False
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

        def publish_event(token: Token | None, event: str, detail: str) -> None:
            if current():
                self.handle_native_event(token, event, detail)
                on_event(token, event, detail)

        def publish_pcm(token: Token, frames: int) -> None:
            if current() and on_pcm is not None:
                on_pcm(token, frames)

        def publish_inference(token: Token, metrics: dict) -> None:
            if current() and on_inference is not None:
                on_inference(token, metrics)

        def publish_voice_end(token: Token, metrics: dict) -> None:
            if current() and on_voice_end is not None:
                on_voice_end(token, metrics)

        def load_native() -> None:
            native = None
            try:
                native = NativeAudio(
                    pid, Path(profile.native_helper), Path(profile.asr_model), Path(profile.asr_tokens),
                    publish_event, on_pcm=publish_pcm if on_pcm is not None else None,
                    on_inference=publish_inference if on_inference is not None else None,
                    on_voice_end=publish_voice_end if on_voice_end is not None else None)
                native.start()
                if not current():
                    native.close()
                    return
                self._native_audio = native
                now = self.runtime_state()
                if (now.token is not None and now.utterance is not None and not now.sent and
                        not now.completed and now.utterance.advance_mode.value == 'audio' and
                        not now.utterance.use_tts):
                    native.begin(now.token, now.utterance.match_text)
                on_ready(session, True, '')
            except Exception as exc:
                if native is not None:
                    native.close()
                if current():
                    state = self.runtime_state()
                    utterance = state.utterance
                    if (utterance is not None and utterance.advance_mode.value == 'audio' and
                            not utterance.use_tts and not utterance.uncertain_text_fallback):
                        self.controller.pause('native_audio_unavailable_hold')
                    on_ready(session, False, str(exc))

        threading.Thread(target=load_native, daemon=True, name='autoadv-load-native').start()
        return True

    def close_native_audio(self) -> None:
        self._native_audio_epoch += 1
        native, self._native_audio = self._native_audio, None
        if native is not None:
            threading.Thread(target=native.close, daemon=True, name='autoadv-close-native').start()

    def cancel_native_audio(self) -> None:
        if self._native_audio is not None:
            self._native_audio.cancel()

    def begin_native_audio(self, token: Token, match_text: str, *, replay_from_ms: int | None = None) -> bool:
        if self._native_audio is None:
            return False
        self._native_audio.begin(token, match_text, replay_from_ms=replay_from_ms)
        return True

    def native_audio_holds_deadline(self, token: Token) -> bool:
        return bool(self._native_audio and self._native_audio.holds_deadline(token))

    def native_audio_boundary(self) -> None:
        if self._native_audio is not None:
            self._native_audio.boundary()

    def native_audio_capture_status(self) -> dict:
        return self._native_audio.capture_status() if self._native_audio is not None else {}

    def handle_native_event(self, token: Token | None, event: str, detail: str = '') -> bool:
        """Apply native evidence and drive completion/advance in Application."""
        state = self.runtime_state()
        if token is None or token != state.token or state.utterance is None:
            return False
        utterance = state.utterance
        if utterance.advance_mode.value != 'audio' or utterance.use_tts:
            return False
        if event == 'error':
            if not utterance.uncertain_text_fallback:
                self.controller.pause('native_audio_failure')
            self._notify_runtime()
            return False
        mapping = {'started': 'native_started', 'quiet': 'native_quiet', 'completed': 'native_finished'}
        presentation = mapping.get(event)
        if presentation is None:
            return False
        return self.runtime_presentation_event(token, presentation)

    @property
    def ocr_running(self) -> bool:
        return self._ocr_worker is not None

    def start_ocr(self, profile: Profile, *, on_text: Callable[[int, int, int, str], None],
                  on_error: Callable[[str], None]) -> bool:
        """Own the optional OCR acquisition worker for the active runtime."""
        self.close_ocr()
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

        def publish_text(sequence: int, text: str) -> None:
            if current():
                on_text(session, pid, sequence, text)

        def publish_error(reason: str) -> None:
            if current():
                self.controller.pause('ocr_failure')
                on_error(reason)

        reader = TesseractOCR(profile.ocr, WindowsClientCapture(state.target.hwnd, pid))
        worker = OCRWorker(reader, publish_text, publish_error)
        self._ocr_worker = worker
        worker.start()
        return True

    def close_ocr(self) -> None:
        self._ocr_epoch += 1
        worker, self._ocr_worker = self._ocr_worker, None
        if worker is not None:
            threading.Thread(target=worker.close, daemon=True, name='autoadv-close-ocr').start()

    def configure_voice_jobs(self, *, on_result: Callable[[Token, bool, str], None] | None,
                             on_preview: Callable[[bool, str], None],
                             on_timing: Callable[[Token, str, float, float], None] | None = None) -> None:
        """Bind observers while Application retains playback completion policy."""
        if self._voice_jobs is not None:
            self._voice_jobs.cancel()
        if self._preview_jobs is not None:
            self._preview_jobs.cancel()

        def runtime_result(token: Token, done: bool, reason: str) -> None:
            self.runtime_presentation_event(token, 'tts_finished', success=done)
            if on_result is not None:
                on_result(token, done, reason)

        self._voice_jobs = VoiceJobs(runtime_result, on_timing=on_timing)
        self._preview_jobs = VoiceJobs(lambda _token, done, reason: on_preview(done, reason))

    def start_voice(self, token: Token, reading: str, voice: str) -> None:
        if self._voice_jobs is None:
            raise RuntimeError('voice jobs are not configured')
        self._voice_jobs.start(token, self.catalog.irodori_url, reading, voice)

    def cancel_voice(self) -> None:
        if self._voice_jobs is not None:
            self._voice_jobs.cancel()

    def start_voice_preview(self, reading: str, voice: str) -> None:
        if self._preview_jobs is None:
            raise RuntimeError('voice preview is not configured')
        self._preview_jobs.start(Token(-1, -1), self.catalog.irodori_url, reading, voice)

    def cancel_voice_preview(self) -> None:
        if self._preview_jobs is not None:
            self._preview_jobs.cancel()

    def hook_connection_state(self) -> HookConnectionState:
        return self._hook_connection

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

    def session_target(self) -> Target:
        return self._runtime_target.target()

    def reset_hook_connection(self) -> None:
        self._hook_connection = HookConnectionState()
        self._inserted_hook_codes.clear()
        self._pc_hooks_inserted = False

    def begin_hook_connection(self, pid: int, profile: Profile) -> bool:
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
            self.hook_command('settings', flush_ms=profile.hook_flush_ms,
                codepage=profile.hook_codepage, max_buffer=profile.hook_max_buffer,
                max_history=profile.hook_max_history)
            self.hook_command('connect', pid=pid)
        except Exception:
            self.reset_hook_connection()
            raise
        return True

    def record_inserted_hook(self, code: str) -> None:
        code = code.strip()
        if code:
            self._inserted_hook_codes.add(code)

    def record_pc_hooks_inserted(self) -> None:
        self._pc_hooks_inserted = True

    def restore_profile_capture(self, profile: Profile) -> None:
        """Apply one profile's Luna settings to the current connection.

        Reusing an already connected game after a profile change must not leave
        the previous profile's flush/codepage or omit saved inserted hooks.
        Idempotence is connection-scoped so repeated UI refreshes do not insert
        the same manual hooks again.
        """
        state = self._hook_connection
        if not self.hook_host_running or not state.connected:
            raise RuntimeError('LunaHook is not connected')
        self.hook_command('settings', flush_ms=profile.hook_flush_ms,
            codepage=profile.hook_codepage, max_buffer=profile.hook_max_buffer,
            max_history=profile.hook_max_history)
        if profile.insert_pc_hooks and not self._pc_hooks_inserted:
            self.hook_command('insert_pc_hooks', pid=state.attached_pid)
            self._pc_hooks_inserted = True
        for code in profile.inserted_hooks:
            if code in self._inserted_hook_codes:
                continue
            self.hook_command('insert_hook', pid=state.attached_pid, code=code)
            self._inserted_hook_codes.add(code)

    def accept_hook_connection_event(self, event: HookEvent) -> str | None:
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
            self.reset_hook_connection()
            return 'disconnected'
        return None

    def cancel_hook_connection_attempt(self, pid: int | None = None) -> None:
        state = self._hook_connection
        if pid is not None and state.connecting_pid != pid:
            return
        if state.connected:
            self._hook_connection = HookConnectionState(
                attached_pid=state.attached_pid, host_generation=state.host_generation,
                connection_generation=state.connection_generation)
        else:
            self.reset_hook_connection()

    def capture_event_matches_connection(self, event: HookEvent) -> bool:
        state = self._hook_connection
        return bool(state.connected and event.process_id == state.attached_pid and
                    event.host_generation == state.host_generation and
                    event.connection_generation == state.connection_generation)

    @property
    def host_epoch(self) -> int:
        return self._host_epoch

    @property
    def hook_host_running(self) -> bool:
        host = self.hook_host
        return bool(host and host.child is not None and host.child.poll() is None)

    def start_hook_host(self) -> None:
        """Own the isolated Luna Host lifecycle and suppress stale callbacks."""
        if self._hook_event_observer is None or self._hook_closed_observer is None or self._hook_error_observer is None:
            raise RuntimeError('Hook service observers are not configured')
        self._host_epoch += 1
        epoch = self._host_epoch
        self.reset_hook_connection()
        if self.hook_host is not None:
            self.hook_host.close()
            self.hook_host = None
        self._synced_sources.clear()

        def current(callback, *args):
            if epoch == self._host_epoch:
                callback(*args)

        runtime = adopted_runtime()
        host = HookHost(
            runtime.host,
            lambda event: current(self._hook_event_observer, event),
            lambda reason: current(self._hook_closed_observer, reason),
            lambda message: current(self._hook_error_observer, message),
        )
        host.start()
        self.hook_host = host

    def close_hook_host(self) -> None:
        self._host_epoch += 1
        self.reset_hook_connection()
        host, self.hook_host = self.hook_host, None
        self._synced_sources.clear()
        if host is not None:
            host.close()

    def _reconcile_hook_sync(self) -> None:
        target = self._runtime_target
        if not target.selected:
            return
        errors = self.sync_hook_threads(target.hook_pid)
        if errors:
            self.report_capture_problem('複数の取得元を同期できません。取得の復旧を確認してください。')
            for reason in errors:
                self._emit_runtime_event('hook_sync_failed', None, reason=reason)

    def sync_hook_threads(self, process_id: int, *, clear: bool = False) -> tuple[str, ...]:
        """Apply Luna selected-thread synchronization from application-owned recipes.

        Application recomputes synchronization whenever observed sources,
        capture recipes or the active runtime change.  The UI never owns the
        selected set or remembers which native threads were synchronized.
        """
        selected = []
        if not clear:
            profiles = []
            runtime = self.runtime_state().profile
            if runtime is not None and runtime.source == 'hook':
                profiles.append(runtime)
            if self.capture is not None and self.capture.profile.source == 'hook':
                profiles.append(self.capture.profile)
            seen: set[str] = set()
            for profile in profiles:
                provider_ids = list(dict.fromkeys([*profile.body_sources, *profile.speaker_sources]))
                if len(provider_ids) <= 1:
                    continue
                by_identity = {hook.identity: hook for hook in profile.hooks}
                for identity in provider_ids:
                    hook = by_identity.get(identity)
                    if hook is not None and identity not in seen:
                        selected.append(hook)
                        seen.add(identity)

        desired: dict[tuple[int, int, int, int], ThreadSource] = {}
        for source in self.capture_sources():
            if source.process_id == process_id and any(h.matches(source) for h in selected):
                key = (source.process_id, source.address, source.context, source.context2)
                desired[key] = source

        errors: list[str] = []
        for key, source in tuple(self._synced_sources.items()):
            if key in desired:
                continue
            try:
                if self.hook_host_running:
                    self.hook_command('sync_thread', source=source.__dict__, enabled=False)
            except (RuntimeError, OSError, ValueError) as exc:
                errors.append(str(exc))
            self._synced_sources.pop(key, None)

        if self.hook_host_running:
            for key, source in desired.items():
                if key in self._synced_sources:
                    continue
                try:
                    self.hook_command('sync_thread', source=source.__dict__, enabled=True)
                    self._synced_sources[key] = source
                except (RuntimeError, OSError, ValueError) as exc:
                    errors.append(str(exc))
        elif clear:
            self._synced_sources.clear()
        return tuple(errors)

    def hook_command(self, action: str, **kwargs) -> None:
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
        self.replace_catalog(catalog)

    def replace_catalog(self, catalog: Catalog) -> None:
        """Replace the persisted catalog after an explicit import.

        A catalog replacement invalidates every selected game/profile and capture
        draft.  Callers must repopulate their presentation from this new single
        source rather than keeping the previous catalog alive in parallel.
        """
        self.catalog = catalog
        self.game_id = ''
        self.profile_id = ''
        self.capture = None
        self._capture_output_group.clear()
        self._runtime_output_group.clear()
        self.clear_runtime_target()
        self._resume_saved_monitor = False
        self._capture_transition_pending = False
        self._runtime_capture_problem = None

    def start_irodori(self, on_result: Callable[[VoiceServiceView], None]) -> None:
        """Connect or start Irodori without making connection management a UI task."""
        # A server generation change invalidates both preview and runtime HTTP
        # work.  Do not let an old synthesis finish against a replaced endpoint.
        self.cancel_voice_preview()
        self.cancel_voice()
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
        self.cancel_voice()
        self._irodori_epoch += 1
        server, self._irodori_server = self._irodori_server, None
        if server is not None:
            server.close()

    def select_profile(self, game_id: str, profile_id: str) -> Profile | None:
        self.game_id, self.profile_id = game_id, profile_id
        game = self.catalog.games.get(game_id)
        profile = game.profiles.get(profile_id) if game else None
        self._capture_output_group.clear()
        self._runtime_output_group.clear()
        self._resume_saved_monitor = False
        self._capture_transition_pending = False
        self.capture = CaptureWorkspace(profile, self.catalog.effective_profile) if profile else None
        self._runtime_capture_problem = None
        self._searched_hook_codes.clear()
        self._trial_hook_codes.clear()
        return profile

    def current_profile(self) -> Profile | None:
        game = self.catalog.games.get(self.game_id)
        return game.profiles.get(self.profile_id) if game else None

    def observe_capture(self, source: ThreadSource, text: str = '', *, sequence: int = 0) -> None:
        if self.capture:
            self.capture.observe(source, text, sequence=sequence)
            self._reconcile_hook_sync()

    def observe_capture_group(self, events: Sequence[HookEvent]) -> None:
        if self.capture:
            self.capture.observe_group(events)

    def queue_capture_output(self, event: HookEvent) -> int | None:
        if not self.capture or event.kind != 'output' or event.source is None:
            return None
        self._capture_output_group.append(event)
        if len(self._capture_output_group) >= 64:
            self.flush_capture_outputs()
            return None
        return 100

    def flush_capture_outputs(self) -> None:
        group, self._capture_output_group = self._capture_output_group, []
        if group and self.capture:
            self.capture.observe_group(group)

    def remove_capture_source(self, identity: str, address: int) -> None:
        if self.capture:
            self.capture.remove(identity, address)
            self._reconcile_hook_sync()

    def clear_capture_observations(self) -> None:
        target = self._runtime_target
        if target.selected:
            self.sync_hook_threads(target.hook_pid, clear=True)
        self._capture_output_group.clear()
        if self.capture:
            self.capture.clear_observations()
        self._searched_hook_codes.clear()
        self._trial_hook_codes.clear()

    def capture_source(self, identity: str, address: int, *, require_unique: bool = True) -> ThreadSource | None:
        return self.capture.source(identity, address, require_unique=require_unique) if self.capture else None

    def capture_sources(self, *, unique_only: bool = True) -> tuple[ThreadSource, ...]:
        return self.capture.sources(unique_only=unique_only) if self.capture else ()

    def capture_candidate(self, identity: str, address: int) -> CaptureCandidateView | None:
        return self.capture.candidate(identity, address) if self.capture else None

    def capture_profile_snapshot(self) -> Profile | None:
        return deepcopy(self.capture.profile) if self.capture else None

    def capture_identity_is_ambiguous(self, identity: str) -> bool:
        return bool(self.capture and self.capture.identity_is_ambiguous(identity))

    def capture_has_identity(self, identity: str) -> bool:
        return bool(self.capture and self.capture.has_identity(identity))

    def capture_has_output(self, identity: str) -> bool:
        return bool(self.capture and self.capture.has_output(identity))

    def capture_saved_sources_ready(self, profile: Profile) -> bool:
        if not self.capture:
            return False
        return any(
            not self.capture.identity_is_ambiguous(identity) and self.capture.has_output(identity)
            for identity in profile.body_sources)

    def capture_output_identities(self) -> tuple[str, ...]:
        return self.capture.output_identities() if self.capture else ()

    def handle_capture_host_event(self, event: HookEvent) -> CaptureHostEventOutcome:
        """Own capture-source state transitions for one current Host event."""
        if event.source is None or not self.capture_event_matches_connection(event):
            return CaptureHostEventOutcome()
        identity = event.source.identity
        if event.kind == 'removed':
            active_removed = self.runtime_state().active_body_source == identity
            self.remove_capture_source(identity, event.source.address)
            resumed = False
            if active_removed:
                self.stop_monitoring('host_changed')
                profile = self.current_profile()
                self.request_saved_monitor_resume(profile)
                resumed = self.try_resume_saved_monitor(profile) is not None
                if resumed:
                    self.clear_capture_problem()
                else:
                    self.report_capture_problem('監視中の台詞取得元が消失しました。取得の復旧を確認してください。')
            return CaptureHostEventOutcome(
                changed=True, active_source_removed=active_removed, monitor_started=resumed)
        if event.kind not in ('created', 'output'):
            return CaptureHostEventOutcome()
        was_ambiguous = self.capture_identity_is_ambiguous(identity)
        self.observe_capture(event.source, event.text if event.kind == 'output' else '', sequence=event.sequence)
        new_ambiguity = self.capture_identity_is_ambiguous(identity) and not was_ambiguous
        if new_ambiguity and self.runtime_state().profile is not None:
            self.stop_monitoring('host_changed')
            self.report_capture_problem('同じ識別子の取得元が複数あり、台詞を一意に選べません。')
        resumed = self.try_resume_saved_monitor(self.current_profile()) is not None
        return CaptureHostEventOutcome(
            changed=True, new_ambiguity=new_ambiguity, monitor_started=resumed)

    def handle_hook_diagnostic(self, event: HookEvent) -> None:
        if event.kind != 'diagnostic':
            return
        if event.detail in ('existing_luna_injection_detected', 'cannot_attach_to_host_process'):
            self.cancel_saved_monitor_resume()
            self.cancel_hook_connection_attempt(event.process_id)
        if event.detail == 'existing_luna_injection_detected':
            self.report_capture_problem(
                '対象には既にLunaHookが注入されています。ゲームを再起動してから選び直してください。')
        elif event.detail == 'cannot_attach_to_host_process':
            self.report_capture_problem(
                'ゲームではないLunaHostプロセスが選択されています。実行中のゲームを選び直してください。')

    def handle_capture_activation_error(self, message: str = '') -> None:
        """Translate a capture-service activation failure into product state."""
        if self._runtime_capture_problem is None:
            self.report_capture_problem('ゲームから会話を取得できません。取得の復旧を確認してください。')
        if message:
            self._emit_runtime_event('capture_activation_failed', None, reason=message)

    def handle_hook_error(self, message: str = '') -> None:
        """Expose a Host failure as a user-actionable capture problem.

        The UI adapter may log the native diagnostic, but it must not decide
        whether the failure is a capture problem or how it affects the product
        view.
        """
        self.handle_capture_activation_error(message)
        if message:
            self._emit_runtime_event('hook_error', None, reason=message)

    def handle_hook_closed(self) -> None:
        self.cancel_saved_monitor_resume()
        self.stop_monitoring('host_changed')
        self.reset_hook_connection()
        self.report_capture_problem('ゲームからの台詞取得が停止しました。')

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
        self.hook_command('find_hooks', pid=pid, boundary_module=boundary_module,
                          parameters={'maxRecords': 64})

    def insert_pc_recovery_hooks(self) -> None:
        pid = self._connected_hook_pid()
        self.hook_command('insert_pc_hooks', pid=pid)
        self.record_pc_hooks_inserted()

    def insert_recovery_hook(self, code: str, *, discovered: bool = False) -> bool:
        code = code.strip()
        if not code:
            return False
        if discovered and not self.begin_recovery_hook_trial(code):
            return False
        try:
            pid = self._connected_hook_pid()
            self.hook_command('insert_hook', pid=pid, code=code)
            self.record_inserted_hook(code)
            self.mark_recovery_hook_inserted(code)
            return True
        except Exception:
            if discovered:
                self.recovery_hook_trial_failed(code)
            raise

    def begin_recovery_hook_trial(self, code: str) -> bool:
        code = code.strip()
        if (not code or code in self._searched_hook_codes or
                (self.capture is not None and self.capture.has_hook_code(code))):
            return False
        self._searched_hook_codes.add(code)
        return True

    def recovery_hook_trial_failed(self, code: str) -> None:
        self._searched_hook_codes.discard(code.strip())

    def mark_recovery_hook_inserted(self, code: str) -> None:
        code = code.strip()
        if code:
            self._trial_hook_codes.add(code)

    def set_capture_field_source(self, field: str, identity: str | None, address: int | None = None) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        source = self.capture.source(identity, address) if identity is not None and address is not None else None
        self.capture.set_field_source(field, identity, address)
        if source is not None and source.hook_code in self._trial_hook_codes:
            self.capture.adopt_inserted_hook(source.hook_code)
        self._reconcile_hook_sync()

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
            return CaptureWorkspaceView((), (), None, None, False, False, self._runtime_capture_problem)
        view = self.capture.view()
        if view.problem is not None or self._runtime_capture_problem is None:
            return view
        return CaptureWorkspaceView(
            candidates=view.candidates, assembly=view.assembly, body_source=view.body_source,
            speaker_source=view.speaker_source, dirty=view.dirty, can_commit=view.can_commit,
            problem=self._runtime_capture_problem)

    def report_capture_problem(self, message: str, action: str = 'open_capture_recovery') -> None:
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
             ' active=' + (runtime.active_body_source or '-') +
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

    def adopt_inserted_hook(self, code: str) -> None:
        if self.capture:
            self.capture.adopt_inserted_hook(code)

    def _update_capture_recipe(self, mutate: Callable[[Profile], None]) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        self.capture.mutate_recipe(mutate)

    def rebase_profile_settings(self, profile: Profile) -> None:
        if self.capture and profile.id == self.profile_id:
            self.capture.rebase_settings(profile)

    def commit_capture(self) -> Profile:
        current = self.current_profile()
        if current is None or self.capture is None:
            raise ValueError('作品を選択してください')
        updated = self.capture.committed_profile()
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
        if (self.runtime_state().profile is None and updated.body_sources
                and self._hook_connection.connected):
            self.request_saved_monitor_resume(updated)
            self.try_resume_saved_monitor(updated)
        return updated

    def _update_profile(self, mutate: Callable[[Profile], None]) -> Profile:
        current = self.current_profile()
        if current is None:
            raise ValueError('作品を選択してください')
        updated = deepcopy(current)
        mutate(updated)
        capture_transaction_fields = ('hooks', 'body_sources', 'speaker_sources', 'source_rules', 'inserted_hooks')
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
        if self.capture:
            self.capture.rebase_settings(updated)
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
                name in {'id', 'name', 'hooks', 'body_sources', 'speaker_sources', 'source_rules', 'inserted_hooks'}):
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
                if self.runtime_capture_matches(updated_effective):
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

