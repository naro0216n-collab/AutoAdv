"""Explicit read-only import of Luna 5.3.1 game data; no original writes."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from copy import deepcopy
import json
from .storage import Catalog
from .model import HookSelection


@dataclass(frozen=True)
class LunaGame:
    uid: str
    path: str
    hooks: tuple[HookSelection, ...]
    inserted: tuple[str, ...]


def inspect_luna(path: Path) -> tuple[LunaGame, ...]:
    data = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(data, list) or len(data) < 2 or not isinstance(data[0], list)
            or not isinstance(data[1], dict)):
        raise ValueError('unsupported Luna savegamedata format')
    games = []
    seen = set()
    for uid in data[0]:
        if not isinstance(uid, str) or uid in seen or not isinstance(data[1].get(uid), dict):
            raise ValueError('invalid or duplicate Luna UID')
        seen.add(uid)
        entry = data[1][uid]
        location = entry.get('gamepath', '')
        raw_hooks = entry.get('hook', [])
        manual = entry.get('needinserthookcode', [])
        if (not isinstance(location, str) or not isinstance(raw_hooks, list)
                or not isinstance(manual, list) or any(not isinstance(v, str) or not v.strip() for v in manual)):
            raise ValueError(f'invalid Luna game {uid}')
        hooks = []
        for row in raw_hooks:
            if (not isinstance(row, list) or len(row) != 6 or type(row[2]) is not int
                    or type(row[3]) is not int or not isinstance(row[5], str)
                    or not isinstance(row[4], str)):
                raise ValueError(f'unknown Luna HOOK format in {uid}')
            if row[5]:
                hook = HookSelection(row[5], row[2], row[3])
                if hook not in hooks:
                    hooks.append(hook)
        games.append(LunaGame(uid, location, tuple(hooks), tuple(manual)))
    return tuple(games)


def add_luna_games(catalog: Catalog, games: tuple[LunaGame, ...]) -> Catalog:
    """Construct a separate validated catalog; caller must confirm then save."""
    result = deepcopy(catalog)
    for row in games:
        if row.uid in result.luna_imports:
            raise ValueError('Luna game already imported: ' + row.uid)
        game = result.create_game(Path(row.path).stem or row.uid, row.path)
        profile = game.profiles[game.default_profile]
        profile.hooks = list(row.hooks)
        profile.inserted_hooks = list(row.inserted)
        result.luna_imports[row.uid] = game.id
    return result
