"""Read-only import of AutoAdv's former v2 settings into the current catalog.

The converter maps only field contracts with equivalent meaning. Anything
uncertain remains in the source and is listed for the user's review. All
changes are applied to a separate catalog after explicit GUI confirmation.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from copy import deepcopy
import json
from .model import (Game, HookSelection, OCRSettings, Profile,
                    ReplacementRule, StructureRule, DEFAULT_STRUCTURE_RULES)
from .storage import Catalog


@dataclass(frozen=True)
class LegacyGame:
    game: Game
    unmapped: tuple[str, ...]


def inspect_legacy(path: Path) -> tuple[LegacyGame, ...]:
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or raw.get('schema_version') != 2:
        raise ValueError('Expected previous AutoAdv settings schema v2; source unchanged')
    old_catalog = raw.get('catalog')
    if not isinstance(old_catalog, dict) or not isinstance(old_catalog.get('games'), dict):
        raise ValueError('Missing previous AutoAdv game catalog')
    games = []
    for gid, data in old_catalog['games'].items():
        if not isinstance(data, dict) or gid != data.get('game_id'):
            raise ValueError('Invalid previous game identity')
        old_profiles = data.get('profiles')
        if not isinstance(old_profiles, dict) or not old_profiles:
            raise ValueError('Previous game is missing profiles')
        unmapped = []
        paths = data.get('paths', [])
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError('Invalid previous executable paths')
        if len(paths) > 1:
            unmapped.append('additional executable paths')
        if data.get('emulator_game_id'):
            unmapped.append('emulator_game_id (no equivalent game identity yet)')
        profiles = {}
        for pid, row in old_profiles.items():
            if not isinstance(row, dict) or pid != row.get('profile_id'):
                raise ValueError('Invalid previous profile identity')
            profile = Profile(pid, str(row.get('name') or 'Imported profile'))
            text = row.get('text') or {}
            if not isinstance(text, dict):
                raise ValueError('Invalid previous text configuration')
            legacy_structure_rules = ([StructureRule(r['pattern'], r.get('condition', ''),
                                        r.get('enabled', True)) for r in text['structure_rules']]
                                      if 'structure_rules' in text else list(DEFAULT_STRUCTURE_RULES))
            profile.replacements = [ReplacementRule(**item) for item in text.get('replacements', [])]
            profile.name_readings = dict(text.get('name_readings', {}))
            if text.get('default_voice'):
                profile.default_voice = text['default_voice']
            for speaker, route in text.get('speaker_routes', {}).items():
                policy = route.get('policy')
                if policy in ('SELECTED', 'DEFAULT'):
                    profile.speaker_tts[speaker] = True
                elif policy in ('NATIVE', 'SILENT'):
                    profile.speaker_tts[speaker] = False
                else:
                    unmapped.append(f'{pid}: speaker {speaker} route {policy} requires review')
                if route.get('voice_id'):
                    profile.speaker_voices[speaker] = route['voice_id']
            default_policy = text.get('unknown_route')
            if default_policy in ('SELECTED', 'DEFAULT'):
                profile.default_tts = True
                profile.default_voice = text.get('unknown_voice') or profile.default_voice
            elif default_policy not in (None, 'NATIVE', 'SILENT'):
                unmapped.append(f'{pid}: unknown_route {default_policy} requires review')
            unmapped.append(f'{pid}: old audio policy cannot determine the new 2-mode advance setting')
            acquisition = row.get('acquisition') or {}
            if not isinstance(acquisition, dict):
                raise ValueError('Invalid previous acquisition configuration')
            profile.source = acquisition.get('kind', 'hook')
            if profile.source == 'ocr':
                region = acquisition.get('region')
                if region is None:
                    raise ValueError('OCR profile is missing its capture region')
                profile.ocr = OCRSettings(acquisition.get('tesseract_executable', 'tesseract'),
                                          acquisition.get('language', 'jpn'), tuple(region))
            if acquisition.get('stable_ms') is not None:
                unmapped.append(f'{pid}: acquisition.stable_ms (different OCR stability model)')
            hooks = row.get('selected_hooks', [])
            if not isinstance(hooks, list):
                raise ValueError('Invalid previous selected HOOK list')
            profile.hooks = [HookSelection(h['code'], h['context'], h['context2']) for h in hooks]
            profile.body_source = profile.hooks[0].identity if profile.hooks else ''
            profile.source_rules = {hook.identity: list(legacy_structure_rules) for hook in profile.hooks}
            profile.inserted_hooks = list(row.get('inserted_hooks', []))
            monitor = row.get('monitor') or {}
            if not isinstance(monitor, dict):
                raise ValueError('Invalid previous monitor settings')
            for old_field, new_field in (('loopback_helper', 'native_helper'),
                                         ('asr_model', 'asr_model'), ('asr_tokens', 'asr_tokens')):
                if monitor.get(old_field):
                    setattr(profile, new_field, monitor[old_field])
            advance = monitor.get('advance_input') or {}
            if advance.get('mode', 'keyboard') == 'keyboard' and 'virtual_key' in advance:
                profile.input_key = advance['virtual_key']
            elif advance:
                unmapped.append(f'{pid}: monitor.advance_input non-keyboard mode')
            if row.get('quality'):
                unmapped.append(f'{pid}: quality (upstream filters require review)')
            if row.get('overrides'):
                unmapped.append(f'{pid}: overrides (non-equivalent runtime settings)')
            if monitor:
                leftovers = set(monitor) - {'loopback_helper', 'asr_model', 'asr_tokens', 'advance_input'}
                if leftovers:
                    unmapped.append(f'{pid}: monitor fields {", ".join(sorted(leftovers))}')
            unknown_profile_fields = set(row) - {
                'profile_id', 'name', 'text', 'acquisition', 'selected_hooks',
                'inserted_hooks', 'quality', 'monitor', 'overrides'}
            if unknown_profile_fields:
                unmapped.append(f'{pid}: other fields {", ".join(sorted(unknown_profile_fields))}')
            profile.validate()
            profiles[pid] = profile
        default = data.get('default_profile_id')
        if default not in profiles:
            raise ValueError('Invalid previous default profile')
        game = Game(gid, data.get('display_name', ''), paths[0] if paths else '',
                    profiles, default)
        if not game.name.strip():
            raise ValueError('Invalid previous game name')
        games.append(LegacyGame(game, tuple(unmapped)))
    return tuple(games)


def add_legacy_games(catalog: Catalog, rows: tuple[LegacyGame, ...]) -> Catalog:
    result = deepcopy(catalog)
    for row in rows:
        if row.game.id in result.games:
            raise ValueError('Previous AutoAdv game already imported: ' + row.game.id)
        result.games[row.game.id] = deepcopy(row.game)
    return result
