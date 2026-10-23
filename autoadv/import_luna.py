"""Explicit read-only import of Luna 5.3.1 game data; no original writes."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from copy import deepcopy
import json
from .storage import Catalog
from .model import HookSelection, ReplacementRule


@dataclass(frozen=True)
class LunaGame:
    uid: str
    path: str
    hooks: tuple[HookSelection, ...]
    inserted: tuple[str, ...]
    title: str = ''
    repairs: tuple[ReplacementRule, ...] = ()
    hook_options: tuple[tuple[str, int], ...] = ()
    unmapped: tuple[str, ...] = ()


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
        repairs = []
        if not entry.get('tts_follow_default', True) and entry.get('tts_repair', False):
            for row in entry.get('tts_repair_regex', []):
                if not isinstance(row, dict) or not all(k in row for k in ('key', 'value')):
                    raise ValueError(f'invalid Luna TTS replacement in {uid}')
                repairs.append(ReplacementRule(str(row['key']), str(row['value']),
                                               bool(row.get('regex', False)), 'tts'))
        # These are the native Host settings Luna exposes through hooksetting_private.
        # Zero codepage signifies automatic detection in upstream and cannot
        # be silently replaced with an arbitrary codepage here.
        known = {'textthreaddelay': 'hook_flush_ms', 'maxBufferSize': 'hook_max_buffer',
                 'maxHistorySize': 'hook_max_history', 'codepage_value': 'hook_codepage'}
        raw_options = entry.get('hooksetting_private', {})
        if not isinstance(raw_options, dict):
            raise ValueError(f'invalid Luna HOOK settings in {uid}')
        options = []
        unmapped = []
        for original, field in known.items():
            if original in raw_options:
                value = raw_options[original]
                if type(value) is int and (original != 'codepage_value' or value > 0):
                    options.append((field, value))
                else:
                    unmapped.append(f'hooksetting_private.{original}')
        if entry.get('tts_skip') or entry.get('tts_skip_regex'):
            unmapped.append('tts_skip / tts_skip_regex (voice routing requires review)')
        if entry.get('save_text_process_info', {}).get('rank'):
            unmapped.append('save_text_process_info (upstream text filters)')
        if entry.get('tts_follow_default', True) and entry.get('tts_repair_regex'):
            unmapped.append('tts_repair_regex (inherits upstream global policy)')
        if any(k not in known for k in raw_options):
            unmapped.append('hooksetting_private other upstream options')
        title = entry.get('title', '')
        if not isinstance(title, str):
            raise ValueError(f'invalid Luna title in {uid}')
        games.append(LunaGame(uid, location, tuple(hooks), tuple(manual), title,
                              tuple(repairs), tuple(options), tuple(unmapped)))
    return tuple(games)


def add_luna_games(catalog: Catalog, games: tuple[LunaGame, ...]) -> Catalog:
    """Construct a separate validated catalog; caller must confirm then save."""
    result = deepcopy(catalog)
    for row in games:
        if row.uid in result.luna_imports:
            raise ValueError('Luna game already imported: ' + row.uid)
        game = result.create_game(row.title.strip() or Path(row.path).stem or row.uid, row.path)
        profile = game.profiles[game.default_profile]
        profile.hooks = list(row.hooks)
        profile.body_source = profile.hooks[0].identity if profile.hooks else ''
        profile.inserted_hooks = list(row.inserted)
        profile.replacements = list(row.repairs)
        for setting, value in row.hook_options:
            setattr(profile, setting, value)
        profile.validate()
        result.luna_imports[row.uid] = game.id
    return result
