"""One atomically persisted settings catalog; runtime ownership stays transient."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import tempfile
import uuid
import copy
from .model import (AdvanceMode, Game, HookSelection, Profile, StructureRule, ReplacementRule, OCRSettings,
                    DEFAULT_STRUCTURE_RULES, SPEAKER_ONLY_RULE, COMMON_FIELDS)


@dataclass
class CommonSettings:
    advance_mode: AdvanceMode = AdvanceMode.TEXT
    uncertain_text_fallback: bool = False
    default_tts: bool = False
    default_voice: str = ''
    reading_ms: int = 700
    per_character_wait_ms: int = 200
    native_helper: str = ''
    asr_model: str = ''
    asr_tokens: str = ''
    input_key: int = 13
    next_line_timeout_ms: int = 8000

    def validate(self):
        template = Profile('_common', '_common')
        for key in COMMON_FIELDS:
            setattr(template, key, getattr(self, key))
        template.validate()


@dataclass
class Catalog:
    irodori_root: str = ''
    irodori_url: str = 'http://127.0.0.1:8088'
    host_dll: str = ''
    games: dict[str, Game] = field(default_factory=dict)
    luna_imports: dict[str, str] = field(default_factory=dict)
    common: CommonSettings = field(default_factory=CommonSettings)

    def effective_profile(self, profile: Profile) -> Profile:
        self.common.validate()
        effective = copy.deepcopy(profile)
        for key in profile.inherit_common:
            setattr(effective, key, getattr(self.common, key))
        effective.validate()
        return effective

    def create_game(self, name: str, executable: str = '') -> Game:
        if not name.strip():
            raise ValueError('game name required')
        game_id = uuid.uuid4().hex
        pid = uuid.uuid4().hex
        game = Game(game_id, name.strip(), executable,
                    {pid: Profile(pid, 'Default', inherit_common=list(COMMON_FIELDS))}, pid)
        self.games[game_id] = game
        return game

    def add_profile(self, game_id: str, name: str) -> Profile:
        if not name.strip():
            raise ValueError('profile name required')
        game = self.games[game_id]
        profile_id = uuid.uuid4().hex
        profile = Profile(profile_id, name.strip(), inherit_common=list(COMMON_FIELDS))
        game.profiles[profile_id] = profile
        return profile

    def duplicate_profile(self, game_id: str, profile_id: str, name: str) -> Profile:
        game = self.games[game_id]
        if not name.strip():
            raise ValueError('profile name required')
        duplicate = copy.deepcopy(game.profiles[profile_id])
        duplicate.id, duplicate.name = uuid.uuid4().hex, name.strip()
        game.profiles[duplicate.id] = duplicate
        return duplicate

    def delete_profile(self, game_id: str, profile_id: str) -> None:
        game = self.games[game_id]
        if len(game.profiles) == 1:
            raise ValueError('cannot delete the only profile')
        del game.profiles[profile_id]
        if game.default_profile == profile_id:
            game.default_profile = next(iter(game.profiles))

    def rename_profile(self, game_id: str, profile_id: str, name: str) -> None:
        if not name.strip():
            raise ValueError('profile name required')
        self.games[game_id].profiles[profile_id].name = name.strip()


def user_settings_path() -> Path:
    return Path(os.getenv('APPDATA') or Path.home() / '.config') / 'AutoAdv' / 'settings-v4.json'


def _profile_payload(p: Profile) -> dict:
    return dict(
        id=p.id, name=p.name, hooks=[vars(h) for h in p.hooks],
        body_source=p.body_source, speaker_source=p.speaker_source,
        source_rules={identity: [vars(rule) for rule in rules]
                      for identity, rules in p.source_rules.items()},
        inserted_hooks=p.inserted_hooks, insert_pc_hooks=p.insert_pc_hooks,
        advance_mode=p.advance_mode.value, uncertain_text_fallback=p.uncertain_text_fallback,
        default_tts=p.default_tts, default_voice=p.default_voice,
        speaker_tts=p.speaker_tts, speaker_voices=p.speaker_voices,
        replacements=[vars(x) for x in p.replacements], name_readings=p.name_readings,
        source=p.source, ocr=vars(p.ocr), native_helper=p.native_helper,
        asr_model=p.asr_model, asr_tokens=p.asr_tokens,
        next_line_timeout_ms=p.next_line_timeout_ms, inherit_common=p.inherit_common,
        reading_ms=p.reading_ms, per_character_wait_ms=p.per_character_wait_ms,
        hook_flush_ms=p.hook_flush_ms, hook_codepage=p.hook_codepage,
        hook_max_buffer=p.hook_max_buffer, hook_max_history=p.hook_max_history,
        repeat_rule=p.repeat_rule, input_key=p.input_key)

def _read_replacements(raw: list | dict) -> list[ReplacementRule]:
    # A single one-way data migration from the initial v3 literal dictionary.
    # After saving, only the ordered rule format is written and evaluated.
    if isinstance(raw, dict):
        return [ReplacementRule(str(before), str(after)) for before, after in raw.items()]
    if not isinstance(raw, list):
        raise ValueError('unsupported replacement data; settings preserved')
    return [ReplacementRule(**rule) for rule in raw]


def _read_profile(raw: dict) -> Profile:
    raw_hooks = list(raw.get('hooks', []))
    hooks = [HookSelection(h['code'], int(h['context']), int(h['context2'])) for h in raw_hooks]
    identities = [hook.identity for hook in hooks]

    # Older settings-v4 revisions stored provider-priority lists.  The product
    # contract has one explicit source per field, so normalize that historical
    # representation once at the persistence boundary and never carry fallback
    # ordering into runtime state.
    if ('body_source' in raw or 'speaker_source' in raw or 'body_sources' in raw or
            'speaker_sources' in raw or 'source_rules' in raw):
        body_source = str(raw.get('body_source', '') or '')
        speaker_source = str(raw.get('speaker_source', '') or '')
        if not body_source:
            body_source = next(iter(raw.get('body_sources', []) or []), '')
        if not speaker_source:
            speaker_source = next(iter(raw.get('speaker_sources', []) or []), '')
        source_rules = {identity: [StructureRule(**rule) for rule in rules]
                        for identity, rules in raw.get('source_rules', {}).items()}
    else:
        legacy_roles = {hook.identity: str(data.get('role', 'text'))
                        for hook, data in zip(hooks, raw_hooks)}
        body_source = next((identity for identity in identities
                            if legacy_roles.get(identity) != 'speaker'), '')
        speaker_source = next((identity for identity in identities
                               if legacy_roles.get(identity) == 'speaker'), '')
        old_rules = ([StructureRule(**rule) for rule in raw['structure_rules']]
                     if 'structure_rules' in raw else list(DEFAULT_STRUCTURE_RULES))
        source_rules = {}
        if body_source:
            source_rules[body_source] = list(old_rules)
        if speaker_source:
            source_rules[speaker_source] = [SPEAKER_ONLY_RULE]
    p = Profile(
        id=raw['id'], name=raw['name'], hooks=hooks,
        body_source=body_source, speaker_source=speaker_source, source_rules=source_rules,
        inserted_hooks=list(raw.get('inserted_hooks', [])),
        insert_pc_hooks=raw.get('insert_pc_hooks', False),
        advance_mode=AdvanceMode(raw.get('advance_mode', 'text')),
        uncertain_text_fallback=raw.get('uncertain_text_fallback', False),
        default_tts=raw.get('default_tts', False), default_voice=raw.get('default_voice', ''),
        speaker_tts=dict(raw.get('speaker_tts', {})), speaker_voices=raw.get('speaker_voices', {}),
        replacements=_read_replacements(raw.get('replacements', [])),
        name_readings=raw.get('name_readings', {}), source=raw.get('source', 'hook'),
        ocr=OCRSettings(**{**raw.get('ocr', {}),
                         'region': tuple(raw.get('ocr', {}).get('region', (0, 0, 640, 180)))}),
        native_helper=raw.get('native_helper', ''), asr_model=raw.get('asr_model', ''),
        asr_tokens=raw.get('asr_tokens', ''),
        next_line_timeout_ms=raw.get('next_line_timeout_ms', 8000),
        inherit_common=list(raw.get('inherit_common', [])), reading_ms=raw.get('reading_ms', 700),
        per_character_wait_ms=raw.get('per_character_wait_ms', 200),
        hook_flush_ms=int(raw.get('hook_flush_ms', 500)), hook_codepage=int(raw.get('hook_codepage', 932)),
        hook_max_buffer=int(raw.get('hook_max_buffer', 3000)),
        hook_max_history=int(raw.get('hook_max_history', 1000000)),
        repeat_rule=raw.get('repeat_rule', 'distinct_only'), input_key=int(raw.get('input_key', 13)))
    p.validate()
    return p

def save(catalog: Catalog, path: Path | None = None) -> None:
    path = path or user_settings_path()
    raw = {'schema_version': 4, 'irodori_root': catalog.irodori_root,
           'irodori_url': catalog.irodori_url, 'host_dll': catalog.host_dll, 'luna_imports': catalog.luna_imports,
           'common': {**vars(catalog.common), 'advance_mode': catalog.common.advance_mode.value},
           'games': {k: dict(id=g.id, name=g.name, executable=g.executable,
                             audio_executable=g.audio_executable, input_executable=g.input_executable,
                             default_profile=g.default_profile,
                             profiles={pid: _profile_payload(p) for pid, p in g.profiles.items()})
                     for k, g in catalog.games.items()}}
    catalog.common.validate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix='.autoadv-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(raw, f, indent=2, ensure_ascii=False)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def load(path: Path | None = None) -> Catalog:
    path = path or user_settings_path()
    if not path.exists():
        previous = path.with_name('settings-v3.json')
        if path.name == 'settings-v4.json' and previous.exists():
            path = previous  # read-only migration source until user approval
        else:
            return Catalog()
    raw = json.loads(path.read_text(encoding='utf-8'))
    if raw.get('schema_version') not in (3, 4):
        raise ValueError('unsupported settings schema; original settings preserved')
    review = []
    if raw['schema_version'] == 3:
        # No inference from old audio-routing policy to the new advance mode.
        # Explicit old TTS use can be preserved, but uncertain fields need review.
        review.append('旧4区分の進行モードには等価な移行先がありません。新規設定は文字のみ判断、文字数フォールバックOFFです。')
        common = raw.get('common', {})
        common['default_tts'] = common.get('default_mode') == 'tts'
        common.pop('default_mode', None)
        common.pop('native_wait_ms', None)
        if common.get('reading_ms', 700) <= 0:
            review.append('共通設定のreading_ms=0を正の最低時間に変更しました。')
            common['reading_ms'] = 700
        for game in raw.get('games', {}).values():
            for profile in game.get('profiles', {}).values():
                former = profile.pop('default_mode', 'reading')
                profile['default_tts'] = former == 'tts'
                profile['speaker_tts'] = {k: v == 'tts' for k, v in profile.pop('speaker_modes', {}).items()}
                profile.pop('native_wait_ms', None)
                inherits_old_mode = 'default_mode' in profile.get('inherit_common', [])
                profile['inherit_common'] = [k for k in profile.get('inherit_common', [])
                                              if k not in ('default_mode', 'native_wait_ms')]
                if inherits_old_mode:
                    profile['inherit_common'].append('default_tts')
                    review.append('旧音声既定値の継承がありました。TTS使用方針を確認してください。')
                if profile.get('reading_ms', 700) <= 0:
                    review.append(f"{profile.get('name')}: reading_ms=0を700msへ変更しました。")
                    profile['reading_ms'] = 700
        raw['common'] = common
    base_common = raw.get('common', {})
    catalog = Catalog(raw.get('irodori_root', ''), raw.get('irodori_url', 'http://127.0.0.1:8088'),
                      raw.get('host_dll', ''), luna_imports=raw.get('luna_imports', {}),
                      common=CommonSettings(**{**base_common,
                          'advance_mode': AdvanceMode(base_common.get('advance_mode', 'text'))}))
    catalog.common.validate()
    for gid, row in raw.get('games', {}).items():
        profiles = {pid: _read_profile(p) for pid, p in row['profiles'].items()}
        if gid != row['id'] or row['default_profile'] not in profiles:
            raise ValueError('inconsistent stored game identity')
        catalog.games[gid] = Game(gid, row['name'], row.get('executable', ''),
                                  profiles, row['default_profile'], row.get('audio_executable', ''),
                                  row.get('input_executable', ''))
    # Never overwrite the v3 source without an explicit GUI migration decision.
    catalog.migration_review = review
    catalog.migration_source = str(path) if review else ''
    return catalog
