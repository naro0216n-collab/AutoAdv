"""Task-oriented application state presented to the GUI.

The GUI should describe the work the player is doing, not mirror HookHost,
Controller, or persistence internals.  Capture editing is deliberately a draft:
several sources can be combined and previewed before one coherent recipe replaces
the active profile.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
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
from .model import HookSelection, Profile, Utterance, interpret
from .native_audio import NativeAudio
from .ocr import OCRWorker, TesseractOCR
from .screen import WindowsClientCapture
from .storage import Catalog
from .voice import VoiceJobs


@dataclass(frozen=True)
class CaptureCandidateView:
    identity: str
    address: int
    latest: str
    history: tuple[str, ...]
    role: str | None
    ambiguous: bool


@dataclass(frozen=True)
class DialoguePreviewView:
    sequence: int
    raw: str
    speaker: str
    text: str
    reading: str
    match_text: str
    use_tts: bool
    voice: str


@dataclass(frozen=True)
class CaptureProblemView:
    message: str
    action: str | None = None


@dataclass(frozen=True)
class CaptureWorkspaceView:
    candidates: tuple[CaptureCandidateView, ...]
    previews: tuple[DialoguePreviewView, ...]
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
    active_text_source: str | None
    presentation_reason: str | None
    pause_reason: str | None


@dataclass(frozen=True)
class VoiceServiceView:
    voices: tuple[str, ...]
    issue: str = ''


@dataclass(frozen=True)
class PlayDialogueView:
    speaker: str
    text: str


@dataclass(frozen=True)
class PlayView:
    advance_authorized: bool
    dialogue: PlayDialogueView | None
    issue: CaptureProblemView | None


_PLAY_ISSUES = {
    'target_not_foreground': ('ゲームが前面にないため、この台詞の自動送りを保留しています。', None),
    'next_line_timeout': ('送り後に次の台詞を取得できません。会話取得を確認してください。', 'open_capture'),
    'ambiguous_identical_line': ('同じ本文が続き、次の台詞か判断できないため自動送りを保留しています。', 'open_capture'),
    'ambiguous_multihook_lines': ('複数の台詞ソースの対応を確定できないため自動送りを保留しています。', 'open_capture'),
    'ambiguous_multihook_speakers': ('複数の話者ソースの対応を確定できないため自動送りを保留しています。', 'open_capture'),
    'unpaired_speaker_output': ('話者ソースに対応する台詞を確定できないため自動送りを保留しています。', 'open_capture'),
    'ocr_unverified_change': ('OCRの変化を次の台詞と確認できないため自動送りを保留しています。', 'open_capture'),
    'ocr_failure': ('OCRで台詞を取得できないため、自動送りを保留しています。', 'open_capture'),
    'native_inconclusive_hold': ('原音の終了を確認できないため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
    'native_audio_failure': ('ゲーム音声を確認できないため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
    'native_audio_unavailable_hold': ('音声判断を利用できないため、この台詞の自動送りを保留しています。', 'open_audio_policy'),
    'tts_playback_failed': ('TTS再生に失敗したため、この台詞の自動送りを保留しています。', 'open_voice'),
    'host_changed': ('ゲームからの台詞取得が途切れたため、自動送りを保留しています。', 'open_capture_recovery'),
    'input_failure_no_retry': ('ゲームへの送り操作に失敗しました。安全のため自動再試行していません。', None),
}


@dataclass
class _ObservedSource:
    source: ThreadSource
    history: deque[tuple[int, str]]
    ambiguous: bool = False


class CaptureWorkspace:
    """Draft one conversation acquisition recipe against real observed output."""

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
                self._draft.speaker_links != self._base.speaker_links or
                self._draft.inserted_hooks != self._base.inserted_hooks or
                self._draft.structure_rules != self._base.structure_rules)

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

    def has_hook_code(self, code: str) -> bool:
        return any(record.source.hook_code == code for record in self._observed.values())

    def candidate(self, identity: str, address: int) -> CaptureCandidateView | None:
        record = self._observed.get((identity, address))
        if record is None:
            return None
        history = tuple(text for _, text in record.history)
        hook = next((item for item in self._draft.hooks if item.identity == identity), None)
        return CaptureCandidateView(identity, address, history[-1] if history else '', history,
                                    hook.role if hook else None, record.ambiguous)

    def _source(self, identity: str, address: int) -> ThreadSource:
        record = self._observed.get((identity, address))
        if record is None or record.ambiguous:
            raise ValueError('取得元を利用できません')
        return record.source

    def role_for(self, identity: str) -> str | None:
        return next((hook.role for hook in self._draft.hooks if hook.identity == identity), None)

    def set_role(self, identity: str, address: int, role: str | None) -> None:
        """Assign a source in the language of the user's task.

        Pairing is automatic while the recipe is unambiguous: one dialogue source
        plus one speaker source requires no separate "pair" control.
        """
        if role not in (None, 'text', 'speaker'):
            raise ValueError('invalid capture role')
        source = self._source(identity, address)
        existing = next((h for h in self._draft.hooks if h.matches(source)), None)
        insertion = self._draft.hooks.index(existing) if existing else len(self._draft.hooks)
        if existing:
            self._draft.hooks.remove(existing)
            self._draft.speaker_links = {
                text: speaker for text, speaker in self._draft.speaker_links.items()
                if text != existing.identity and speaker != existing.identity
            }
        if role is not None:
            self._draft.hooks.insert(
                insertion,
                HookSelection(source.hook_code, source.context, source.context2, role),
            )
        self._resolve_obvious_pairing()

    def move_dialogue_source(self, identity: str, offset: int) -> None:
        hooks = self._draft.hooks
        index = next((i for i, h in enumerate(hooks) if h.identity == identity and h.role == 'text'), -1)
        if index < 0:
            return
        text_positions = [i for i, h in enumerate(hooks) if h.role == 'text']
        order = text_positions.index(index)
        new_order = order + offset
        if not 0 <= new_order < len(text_positions):
            return
        other_index = text_positions[new_order]
        hooks[index], hooks[other_index] = hooks[other_index], hooks[index]

    def pair(self, text_identity: str, speaker_identity: str | None) -> None:
        roles = {h.identity: h.role for h in self._draft.hooks}
        if roles.get(text_identity) != 'text':
            raise ValueError('台詞ソースを選択してください')
        if speaker_identity is None:
            self._draft.speaker_links.pop(text_identity, None)
        elif roles.get(speaker_identity) != 'speaker':
            raise ValueError('話者ソースを選択してください')
        else:
            self._draft.speaker_links[text_identity] = speaker_identity

    def _resolve_obvious_pairing(self) -> None:
        texts = [h for h in self._draft.hooks if h.role == 'text']
        speakers = [h for h in self._draft.hooks if h.role == 'speaker']
        valid_text = {h.identity for h in texts}
        valid_speakers = {h.identity for h in speakers}
        self._draft.speaker_links = {
            text: speaker for text, speaker in self._draft.speaker_links.items()
            if text in valid_text and speaker in valid_speakers
        }
        if len(texts) == 1 and len(speakers) == 1:
            self._draft.speaker_links[texts[0].identity] = speakers[0].identity

    def _record_for_identity(self, identity: str) -> _ObservedSource | None:
        matches = [record for (candidate_identity, _), record in self._observed.items()
                   if candidate_identity == identity]
        return matches[0] if len(matches) == 1 and not matches[0].ambiguous else None

    def _preview_from_group(self, events: tuple[HookEvent, ...], profile: Profile) -> DialoguePreviewView | None:
        roles = {hook.identity: hook.role for hook in self._draft.hooks}
        matching = [event for event in events if event.source and event.source.identity in roles]
        if not matching:
            return None
        texts = [hook for hook in self._draft.hooks if hook.role == 'text']
        chosen = next((hook for hook in texts
                       if any(event.source.identity == hook.identity for event in matching)), None)
        if chosen is None:
            return None
        primary = [event for event in matching if event.source.identity == chosen.identity]
        if len(primary) != 1:
            return None
        text_event = primary[0]
        speaker_identity = self._draft.speaker_links.get(chosen.identity, '')
        if not speaker_identity:
            speakers = [hook.identity for hook in self._draft.hooks if hook.role == 'speaker']
            speaker_identity = speakers[0] if len(speakers) == 1 else ''
        speaker_events = [event for event in matching
                          if speaker_identity and event.source.identity == speaker_identity]
        if len(speaker_events) > 1:
            return None
        side_speaker = speaker_events[0].text.strip() if speaker_events else ''
        utterance = interpret(
            text_event.text, profile, session=0, line=0, source=chosen.identity,
            sequence=text_event.sequence, side_speaker=side_speaker)
        return DialoguePreviewView(
            sequence=text_event.sequence, raw=utterance.raw, speaker=utterance.speaker,
            text=utterance.text, reading=utterance.reading, match_text=utterance.match_text,
            use_tts=utterance.use_tts, voice=utterance.voice)

    def _previews(self) -> tuple[DialoguePreviewView, ...]:
        texts = [hook for hook in self._draft.hooks if hook.role == 'text']
        speakers = [hook for hook in self._draft.hooks if hook.role == 'speaker']
        profile = self._resolve(deepcopy(self._draft))
        previews: list[DialoguePreviewView] = []
        # Multi-HOOK preview must use outputs observed in the same bounded batch;
        # a speaker from a later line is never painted onto older dialogue.
        if speakers:
            for group in self._groups:
                preview = self._preview_from_group(group, profile)
                if preview is not None:
                    previews.append(preview)
        else:
            # A single dialogue source needs no pairing batch and should preview
            # immediately while the user advances the game.
            for hook in texts:
                record = self._record_for_identity(hook.identity)
                if record is None:
                    continue
                for sequence, raw in record.history:
                    utterance = interpret(
                        raw, profile, session=0, line=0, source=hook.identity, sequence=sequence)
                    previews.append(DialoguePreviewView(
                        sequence=sequence, raw=utterance.raw, speaker=utterance.speaker,
                        text=utterance.text, reading=utterance.reading, match_text=utterance.match_text,
                        use_tts=utterance.use_tts, voice=utterance.voice))
        previews.sort(key=lambda item: item.sequence)
        return tuple(previews[-12:])

    def view(self) -> CaptureWorkspaceView:
        hooks = {h.identity: h for h in self._draft.hooks}
        candidates = []
        for (_, address), record in self._observed.items():
            history = tuple(text for _, text in record.history)
            if not history:
                continue
            hook = hooks.get(record.source.identity)
            candidates.append(CaptureCandidateView(
                identity=record.source.identity,
                address=address,
                latest=history[-1],
                history=history,
                role=hook.role if hook else None,
                ambiguous=record.ambiguous,
            ))
        selected = [h for h in self._draft.hooks if h.role == 'text']
        ambiguous_selected = any(
            candidate.ambiguous and candidate.role is not None for candidate in candidates
        )
        problem = None
        if ambiguous_selected:
            problem = CaptureProblemView('選択した取得元を一意に識別できません。別の取得元を選択してください。')
        can_commit = bool(selected) and not ambiguous_selected
        return CaptureWorkspaceView(
            candidates=tuple(candidates),
            previews=self._previews(),
            dirty=self.dirty,
            can_commit=can_commit,
            problem=problem,
        )

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
        capture_fields = {'hooks', 'speaker_links', 'inserted_hooks', 'structure_rules'}
        for field in profile.__dataclass_fields__:
            if field in capture_fields or field in ('id', 'name'):
                continue
            setattr(self._base, field, deepcopy(getattr(profile, field)))
            setattr(self._draft, field, deepcopy(getattr(profile, field)))

    def committed_profile(self) -> Profile:
        if not self.view().can_commit:
            raise ValueError('台詞として使用する取得元を選択してください')
        self._draft.validate()
        return deepcopy(self._draft)

    def mark_committed(self, profile: Profile) -> None:
        self._base = deepcopy(profile)
        self._draft = deepcopy(profile)


class AutoAdvApplication:
    """Application facade for user-level configuration actions.

    The application owns controller and LunaHost lifecycles.  The UI never
    reconstructs a Profile from widget types, edits HookSelection/speaker_links
    directly, or translates Controller reasons into user-facing status text.
    """

    def __init__(self, catalog: Catalog, persist: Callable[[], None], *,
                 input_send: Callable, preflight: Callable):
        self.catalog = catalog
        self._persist = persist
        self.game_id = ''
        self.profile_id = ''
        self.capture: CaptureWorkspace | None = None
        self._runtime_capture_problem: CaptureProblemView | None = None
        self.controller = Controller(input_send, preflight)
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

    def runtime_state(self) -> RuntimeState:
        c = self.controller
        return RuntimeState(
            session=c.session, token=c.token, target=c.target, profile=c.profile,
            utterance=c.utterance, authorized=c.authorized, sent=c.sent,
            completed=c.completed, native_playing=c.native_playing, ocr_blank=c.ocr_blank,
            source_generation=c.source_generation, connection_generation=c.connection_generation,
            active_text_source=c.active_text_source,
            presentation_reason=c.presentation_reason, pause_reason=c.pause_reason)

    def stage_runtime_profile(self, profile: Profile) -> None:
        self.controller.stage_profile(profile)

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

    def runtime_presentation_event(self, token: Token, event: str, *, success: bool = True) -> bool:
        return self.controller.presentation_event(token, event, success=success)

    def try_advance(self) -> bool:
        return self.controller.try_advance()

    def expire_next_line(self, token: Token) -> bool:
        return self.controller.expire_next_line(token)

    def set_auto_advance(self, enabled: bool) -> None:
        self.controller.permit(enabled)

    def close_runtime(self, reason: str = 'stopped') -> None:
        self._runtime_output_group.clear()
        self.controller.close(reason)

    def handle_runtime_hook_output(self, event: HookEvent) -> tuple[Token | None, int | None]:
        """Feed one Hook output or queue the selected multi-source batch.

        The returned delay is only a scheduling request for the UI/event loop;
        ownership of the batch and the grouping policy remain in Application.
        """
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
        """Apply native evidence to Controller and report presentation completion."""
        state = self.runtime_state()
        if token is None or token != state.token or state.utterance is None:
            return False
        utterance = state.utterance
        if utterance.advance_mode.value != 'audio' or utterance.use_tts:
            return False
        if event == 'error':
            if not utterance.uncertain_text_fallback:
                self.controller.pause('native_audio_failure')
            return False
        mapping = {'started': 'native_started', 'quiet': 'native_quiet', 'completed': 'native_finished'}
        presentation = mapping.get(event)
        return bool(presentation and self.controller.presentation_event(token, presentation))

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

    def configure_voice_jobs(self, *, on_result: Callable[[Token, bool, str], None],
                             on_preview: Callable[[bool, str], None],
                             on_timing: Callable[[Token, str, float, float], None] | None = None) -> None:
        """Bind presentation callbacks while retaining playback ownership here."""
        if self._voice_jobs is not None:
            self._voice_jobs.cancel()
        if self._preview_jobs is not None:
            self._preview_jobs.cancel()
        self._voice_jobs = VoiceJobs(on_result, on_timing=on_timing)
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

    @property
    def host_epoch(self) -> int:
        return self._host_epoch

    @property
    def hook_host_running(self) -> bool:
        host = self.hook_host
        return bool(host and host.child is not None and host.child.poll() is None)

    def start_hook_host(self, *, on_event: Callable, on_closed: Callable,
                        on_error: Callable) -> None:
        """Own the isolated Luna Host lifecycle and suppress stale callbacks."""
        self._host_epoch += 1
        epoch = self._host_epoch
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
            lambda event: current(on_event, event),
            lambda reason: current(on_closed, reason),
            lambda message: current(on_error, message),
        )
        host.start()
        self.hook_host = host

    def close_hook_host(self) -> None:
        self._host_epoch += 1
        host, self.hook_host = self.hook_host, None
        self._synced_sources.clear()
        if host is not None:
            host.close()

    def sync_hook_threads(self, process_id: int, *, clear: bool = False) -> tuple[str, ...]:
        """Apply Luna selected-thread synchronization from application-owned recipes.

        The UI can request a recomputation when sources or recipes change, but it
        does not own the selected set or remember which native threads were
        already synchronized.
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
                for hook in profile.hooks if len(profile.hooks) > 1 else ():
                    if hook.identity not in seen:
                        selected.append(hook)
                        seen.add(hook.identity)

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
        self._runtime_capture_problem = None

    def start_irodori(self, on_result: Callable[[VoiceServiceView], None]) -> None:
        """Connect or start Irodori without making connection management a UI task."""
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
        self._irodori_epoch += 1
        server, self._irodori_server = self._irodori_server, None
        if server is not None:
            server.close()

    def select_profile(self, game_id: str, profile_id: str) -> Profile | None:
        self.game_id, self.profile_id = game_id, profile_id
        game = self.catalog.games.get(game_id)
        profile = game.profiles.get(profile_id) if game else None
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

    def clear_capture_observations(self) -> None:
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

    def capture_identity_is_ambiguous(self, identity: str) -> bool:
        return bool(self.capture and self.capture.identity_is_ambiguous(identity))

    def capture_has_identity(self, identity: str) -> bool:
        return bool(self.capture and self.capture.has_identity(identity))

    def capture_saved_sources_ready(self, profile: Profile) -> bool:
        if not self.capture:
            return False
        return (not any(self.capture.identity_is_ambiguous(h.identity) for h in profile.hooks)
                and all(self.capture.has_identity(h.identity) for h in profile.hooks))

    def begin_recovery_hook_trial(self, code: str) -> bool:
        code = code.strip()
        if (not code or code in self._searched_hook_codes or
                (self.capture is not None and self.capture.has_hook_code(code))):
            return False
        self._searched_hook_codes.add(code)
        return True

    def mark_recovery_hook_inserted(self, code: str) -> None:
        code = code.strip()
        if code:
            self._trial_hook_codes.add(code)

    def set_capture_role(self, identity: str, address: int, role: str | None) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        source = self.capture.source(identity, address)
        if source is None:
            raise ValueError('取得元を利用できません')
        self.capture.set_role(identity, address, role)
        if role is not None and source.hook_code in self._trial_hook_codes:
            self.capture.adopt_inserted_hook(source.hook_code)

    def move_dialogue_source(self, identity: str, offset: int) -> None:
        if self.capture:
            self.capture.move_dialogue_source(identity, offset)

    def pair_capture_sources(self, text_identity: str, speaker_identity: str | None) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        self.capture.pair(text_identity, speaker_identity)

    def capture_view(self) -> CaptureWorkspaceView:
        if not self.capture:
            return CaptureWorkspaceView((), (), False, False, self._runtime_capture_problem)
        view = self.capture.view()
        if view.problem is not None or self._runtime_capture_problem is None:
            return view
        return CaptureWorkspaceView(
            candidates=view.candidates, previews=view.previews, dirty=view.dirty,
            can_commit=view.can_commit, problem=self._runtime_capture_problem)

    def report_capture_problem(self, message: str, action: str = 'open_capture_recovery') -> None:
        message = message.strip()
        self._runtime_capture_problem = CaptureProblemView(message, action) if message else None

    def clear_capture_problem(self) -> None:
        self._runtime_capture_problem = None

    def play_view(self) -> PlayView:
        controller = self.controller
        utterance = controller.utterance
        dialogue = (PlayDialogueView(utterance.speaker, utterance.text) if utterance is not None else None)
        authorized = controller.authorized
        issue = None
        if authorized:
            reason = controller.pause_reason
            resolved = _PLAY_ISSUES.get(reason)
            if resolved:
                issue = CaptureProblemView(*resolved)
        return PlayView(authorized, dialogue, issue)

    def adopt_inserted_hook(self, code: str) -> None:
        if self.capture:
            self.capture.adopt_inserted_hook(code)

    def update_capture_recipe(self, mutate: Callable[[Profile], None]) -> None:
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
        return updated

    def update_profile(self, mutate: Callable[[Profile], None]) -> Profile:
        current = self.current_profile()
        if current is None:
            raise ValueError('作品を選択してください')
        updated = deepcopy(current)
        mutate(updated)
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
        return updated

    def set_profile_setting(self, name: str, value) -> Profile:
        """Persist one user-visible setting without reconstructing a form.

        Editing a setting that currently inherits the common default is the
        user's decision to create a game override.  The persistence shape stays
        internal to the application; the GUI never toggles an inheritance field.
        """
        if name not in Profile.__dataclass_fields__ or name in {'id', 'name', 'hooks', 'speaker_links'}:
            raise ValueError('unsupported profile setting')

        def change(profile: Profile) -> None:
            setattr(profile, name, deepcopy(value))
            if name in profile.inherit_common:
                profile.inherit_common.remove(name)

        return self.update_profile(change)

    def reset_profile_setting(self, name: str) -> Profile:
        """Remove a game override and use the current common value."""
        if name not in self.catalog.common.__dataclass_fields__:
            raise ValueError('setting has no common default')

        def change(profile: Profile) -> None:
            if name not in profile.inherit_common:
                profile.inherit_common.append(name)

        return self.update_profile(change)

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
        previous = deepcopy(self.catalog.common)
        setattr(self.catalog.common, name, deepcopy(value))
        try:
            self.catalog.common.validate()
            self._persist()
        except Exception:
            self.catalog.common = previous
            raise

    def set_irodori_environment(self, root: str, url: str) -> None:
        previous = (self.catalog.irodori_root, self.catalog.irodori_url)
        self.catalog.irodori_root = root.strip()
        self.catalog.irodori_url = url.strip()
        try:
            self._persist()
        except Exception:
            self.catalog.irodori_root, self.catalog.irodori_url = previous
            raise

