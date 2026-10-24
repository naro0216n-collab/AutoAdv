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
                    COMMON_FIELDS)




_ROOT_KEYS = frozenset({'schema_version', 'irodori_root', 'irodori_url', 'host_dll', 'luna_imports', 'common', 'games'})
_COMMON_KEYS = frozenset(COMMON_FIELDS)
_GAME_KEYS = frozenset({'id', 'name', 'executable', 'audio_executable', 'input_executable', 'default_profile', 'profiles'})
_PROFILE_KEYS = frozenset({
    'id', 'name', 'hooks', 'body_source', 'speaker_source', 'source_rules', 'unwrap_speaker_enclosed_body',
    'inserted_hooks', 'insert_pc_hooks', 'advance_mode', 'uncertain_text_fallback',
    'default_tts', 'default_voice', 'speaker_tts', 'speaker_voices', 'replacements',
    'name_readings', 'source', 'ocr', 'native_helper', 'alignment_model',
    'alignment_vocab', 'next_line_timeout_ms', 'inherit_common', 'reading_ms',
    'per_reading_unit_wait_ms', 'hook_flush_ms', 'hook_codepage', 'hook_max_buffer',
    'hook_max_history', 'repeat_rule', 'input_key',
})
_HOOK_KEYS = frozenset({'code', 'context', 'context2'})
_STRUCTURE_RULE_KEYS = frozenset({'pattern', 'condition', 'enabled'})
_REPLACEMENT_KEYS = frozenset({'pattern', 'replacement', 'regex', 'target', 'enabled'})
_OCR_KEYS = frozenset({'executable', 'language', 'region', 'interval_ms', 'stable_samples'})


def _require_object(value: object, expected: frozenset[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f'invalid current settings {label}')
    return value

@dataclass
class CommonSettings:
    advance_mode: AdvanceMode = AdvanceMode.TEXT
    uncertain_text_fallback: bool = False
    default_tts: bool = False
    default_voice: str = ''
    reading_ms: int = 700
    per_reading_unit_wait_ms: int = 200
    native_helper: str = ''
    alignment_model: str = ''
    alignment_vocab: str = ''
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
        unwrap_speaker_enclosed_body=p.unwrap_speaker_enclosed_body,
        inserted_hooks=p.inserted_hooks, insert_pc_hooks=p.insert_pc_hooks,
        advance_mode=p.advance_mode.value, uncertain_text_fallback=p.uncertain_text_fallback,
        default_tts=p.default_tts, default_voice=p.default_voice,
        speaker_tts=p.speaker_tts, speaker_voices=p.speaker_voices,
        replacements=[vars(x) for x in p.replacements], name_readings=p.name_readings,
        source=p.source, ocr=vars(p.ocr), native_helper=p.native_helper,
        alignment_model=p.alignment_model, alignment_vocab=p.alignment_vocab,
        next_line_timeout_ms=p.next_line_timeout_ms, inherit_common=p.inherit_common,
        reading_ms=p.reading_ms, per_reading_unit_wait_ms=p.per_reading_unit_wait_ms,
        hook_flush_ms=p.hook_flush_ms, hook_codepage=p.hook_codepage,
        hook_max_buffer=p.hook_max_buffer, hook_max_history=p.hook_max_history,
        repeat_rule=p.repeat_rule, input_key=p.input_key)

def _read_replacements(raw: object) -> list[ReplacementRule]:
    if not isinstance(raw, list):
        raise ValueError('invalid current settings replacements')
    return [ReplacementRule(**_require_object(rule, _REPLACEMENT_KEYS, 'replacement')) for rule in raw]


def _read_profile(raw: object) -> Profile:
    raw = _require_object(raw, _PROFILE_KEYS, 'profile')
    raw_hooks = raw['hooks']
    if not isinstance(raw_hooks, list):
        raise ValueError('invalid current settings hooks')
    hooks = [HookSelection(**_require_object(h, _HOOK_KEYS, 'hook')) for h in raw_hooks]
    source_rules_raw = raw['source_rules']
    if not isinstance(source_rules_raw, dict):
        raise ValueError('invalid current settings source rules')
    source_rules: dict[str, list[StructureRule]] = {}
    for identity, rules in source_rules_raw.items():
        if not isinstance(identity, str) or not isinstance(rules, list):
            raise ValueError('invalid current settings source rules')
        source_rules[identity] = [
            StructureRule(**_require_object(rule, _STRUCTURE_RULE_KEYS, 'structure rule'))
            for rule in rules
        ]
    ocr_raw = _require_object(raw['ocr'], _OCR_KEYS, 'OCR')
    region = ocr_raw['region']
    if not isinstance(region, list) or len(region) != 4:
        raise ValueError('invalid current settings OCR region')
    p = Profile(
        id=raw['id'], name=raw['name'], hooks=hooks,
        body_source=raw['body_source'], speaker_source=raw['speaker_source'],
        source_rules=source_rules, unwrap_speaker_enclosed_body=raw['unwrap_speaker_enclosed_body'],
        inserted_hooks=list(raw['inserted_hooks']),
        insert_pc_hooks=raw['insert_pc_hooks'], advance_mode=AdvanceMode(raw['advance_mode']),
        uncertain_text_fallback=raw['uncertain_text_fallback'], default_tts=raw['default_tts'],
        default_voice=raw['default_voice'], speaker_tts=dict(raw['speaker_tts']),
        speaker_voices=dict(raw['speaker_voices']), replacements=_read_replacements(raw['replacements']),
        name_readings=dict(raw['name_readings']), source=raw['source'],
        ocr=OCRSettings(**{**ocr_raw, 'region': tuple(region)}), native_helper=raw['native_helper'],
        alignment_model=raw['alignment_model'], alignment_vocab=raw['alignment_vocab'],
        next_line_timeout_ms=raw['next_line_timeout_ms'], inherit_common=list(raw['inherit_common']),
        reading_ms=raw['reading_ms'], per_reading_unit_wait_ms=raw['per_reading_unit_wait_ms'],
        hook_flush_ms=raw['hook_flush_ms'], hook_codepage=raw['hook_codepage'],
        hook_max_buffer=raw['hook_max_buffer'], hook_max_history=raw['hook_max_history'],
        repeat_rule=raw['repeat_rule'], input_key=raw['input_key'])
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
        return Catalog()
    raw = _require_object(json.loads(path.read_text(encoding='utf-8')), _ROOT_KEYS, 'root')
    if raw['schema_version'] != 4:
        raise ValueError('unsupported settings schema')
    common_raw = _require_object(raw['common'], _COMMON_KEYS, 'common settings')
    catalog = Catalog(
        raw['irodori_root'], raw['irodori_url'], raw['host_dll'],
        luna_imports=dict(raw['luna_imports']),
        common=CommonSettings(**{**common_raw, 'advance_mode': AdvanceMode(common_raw['advance_mode'])}))
    catalog.common.validate()
    games_raw = raw['games']
    if not isinstance(games_raw, dict):
        raise ValueError('invalid current settings games')
    for gid, value in games_raw.items():
        row = _require_object(value, _GAME_KEYS, 'game')
        profiles_raw = row['profiles']
        if not isinstance(profiles_raw, dict):
            raise ValueError('invalid current settings profiles')
        profiles = {pid: _read_profile(profile) for pid, profile in profiles_raw.items()}
        if gid != row['id'] or row['default_profile'] not in profiles:
            raise ValueError('inconsistent stored game identity')
        catalog.games[gid] = Game(
            gid, row['name'], row['executable'], profiles, row['default_profile'],
            row['audio_executable'], row['input_executable'])
    return catalog
