"""Task-oriented application state presented to the GUI.

The GUI should describe the work the player is doing, not mirror HookHost,
Controller, or persistence internals.  Capture editing is deliberately a draft:
several sources can be combined and previewed before one coherent recipe replaces
the active profile.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable
from collections.abc import Sequence

from .hook_protocol import HookEvent, ThreadSource
from .model import HookSelection, Profile, interpret
from .storage import Catalog


@dataclass(frozen=True)
class CaptureCandidateView:
    identity: str
    address: int
    latest: str
    history: tuple[str, ...]
    role: str | None
    ambiguous: bool
    source_name: str


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

    def observe(self, source: ThreadSource, text: str = '', *, sequence: int = 0,
                ambiguous: bool = False) -> None:
        key = (source.identity, source.address)
        record = self._observed.get(key)
        if record is None:
            record = _ObservedSource(source, deque(maxlen=8))
            self._observed[key] = record
        else:
            record.source = source
        record.ambiguous = record.ambiguous or ambiguous
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

    def clear_observations(self) -> None:
        self._observed.clear()
        self._groups.clear()

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
                source_name=record.source.hook_name or record.source.hook_code or '取得元',
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

    Runtime devices remain separate services; the UI never needs to reconstruct
    a Profile from widget types or edit HookSelection/speaker_links directly.
    """

    def __init__(self, catalog: Catalog, persist: Callable[[], None]):
        self.catalog = catalog
        self._persist = persist
        self.game_id = ''
        self.profile_id = ''
        self.capture: CaptureWorkspace | None = None

    def select_profile(self, game_id: str, profile_id: str) -> Profile | None:
        self.game_id, self.profile_id = game_id, profile_id
        game = self.catalog.games.get(game_id)
        profile = game.profiles.get(profile_id) if game else None
        self.capture = CaptureWorkspace(profile, self.catalog.effective_profile) if profile else None
        return profile

    def current_profile(self) -> Profile | None:
        game = self.catalog.games.get(self.game_id)
        return game.profiles.get(self.profile_id) if game else None

    def observe_capture(self, source: ThreadSource, text: str = '', *, sequence: int = 0,
                        ambiguous: bool = False) -> None:
        if self.capture:
            self.capture.observe(source, text, sequence=sequence, ambiguous=ambiguous)

    def observe_capture_group(self, events: Sequence[HookEvent]) -> None:
        if self.capture:
            self.capture.observe_group(events)

    def remove_capture_source(self, identity: str, address: int) -> None:
        if self.capture:
            self.capture.remove(identity, address)

    def clear_capture_observations(self) -> None:
        if self.capture:
            self.capture.clear_observations()

    def set_capture_role(self, identity: str, address: int, role: str | None) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        self.capture.set_role(identity, address, role)

    def move_dialogue_source(self, identity: str, offset: int) -> None:
        if self.capture:
            self.capture.move_dialogue_source(identity, offset)

    def pair_capture_sources(self, text_identity: str, speaker_identity: str | None) -> None:
        if not self.capture:
            raise ValueError('作品を選択してください')
        self.capture.pair(text_identity, speaker_identity)

    def capture_view(self) -> CaptureWorkspaceView:
        if not self.capture:
            return CaptureWorkspaceView((), (), False, False, None)
        return self.capture.view()

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

