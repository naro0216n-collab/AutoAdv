"""One atomically persisted settings catalog; runtime ownership stays transient."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import tempfile
import uuid
from .model import AudioMode, Game, HookSelection, Profile


@dataclass
class Catalog:
    irodori_root: str = ''
    irodori_url: str = 'http://127.0.0.1:8088'
    host_dll: str = ''
    games: dict[str, Game] = field(default_factory=dict)

    def create_game(self, name: str, executable: str = '') -> Game:
        if not name.strip():
            raise ValueError('game name required')
        game_id = uuid.uuid4().hex
        pid = uuid.uuid4().hex
        game = Game(game_id, name.strip(), executable,
                    {pid: Profile(pid, 'Default')}, pid)
        self.games[game_id] = game
        return game

    def add_profile(self, game_id: str, name: str) -> Profile:
        if not name.strip():
            raise ValueError('profile name required')
        game = self.games[game_id]
        profile_id = uuid.uuid4().hex
        profile = Profile(profile_id, name.strip())
        game.profiles[profile_id] = profile
        return profile


def user_settings_path() -> Path:
    return Path(os.getenv('APPDATA') or Path.home() / '.config') / 'AutoAdv' / 'settings-v3.json'


def _profile_payload(p: Profile) -> dict:
    return dict(id=p.id, name=p.name, hooks=[vars(h) for h in p.hooks],
                default_mode=p.default_mode.value, default_voice=p.default_voice,
                speaker_modes={k: AudioMode(v).value for k, v in p.speaker_modes.items()},
                speaker_voices=p.speaker_voices, replacements=p.replacements,
                reading_ms=p.reading_ms, settle_ms=p.settle_ms,
                repeat_rule=p.repeat_rule, input_key=p.input_key)


def _read_profile(raw: dict) -> Profile:
    p = Profile(id=raw['id'], name=raw['name'],
                hooks=[HookSelection(**h) for h in raw.get('hooks', [])],
                default_mode=AudioMode(raw.get('default_mode', 'reading')),
                default_voice=raw.get('default_voice', ''),
                speaker_modes={k: AudioMode(v) for k, v in raw.get('speaker_modes', {}).items()},
                speaker_voices=raw.get('speaker_voices', {}), replacements=raw.get('replacements', {}),
                reading_ms=int(raw.get('reading_ms', 1200)), settle_ms=int(raw.get('settle_ms', 180)),
                repeat_rule=raw.get('repeat_rule', 'distinct_only'), input_key=int(raw.get('input_key', 13)))
    if not p.id or not p.name or p.reading_ms < 0 or p.settle_ms < 0:
        raise ValueError('invalid stored profile')
    return p


def save(catalog: Catalog, path: Path | None = None) -> None:
    path = path or user_settings_path()
    raw = {'schema_version': 3, 'irodori_root': catalog.irodori_root,
           'irodori_url': catalog.irodori_url, 'host_dll': catalog.host_dll,
           'games': {k: dict(id=g.id, name=g.name, executable=g.executable,
                             default_profile=g.default_profile,
                             profiles={pid: _profile_payload(p) for pid, p in g.profiles.items()})
                     for k, g in catalog.games.items()}}
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
    raw = json.loads(path.read_text(encoding='utf-8'))
    if raw.get('schema_version') != 3:
        raise ValueError('unsupported settings schema; original settings preserved')
    catalog = Catalog(raw.get('irodori_root', ''), raw.get('irodori_url', 'http://127.0.0.1:8088'),
                      raw.get('host_dll', ''))
    for gid, row in raw.get('games', {}).items():
        profiles = {pid: _read_profile(p) for pid, p in row['profiles'].items()}
        if gid != row['id'] or row['default_profile'] not in profiles:
            raise ValueError('inconsistent stored game identity')
        catalog.games[gid] = Game(gid, row['name'], row.get('executable', ''),
                                  profiles, row['default_profile'])
    return catalog
