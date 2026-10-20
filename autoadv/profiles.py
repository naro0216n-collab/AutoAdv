"""Validated, stable game/profile identities and source-preserving Luna import.

Profiles contain configuration only. Runtime PIDs/HWND, observed thread addresses,
Host generations and authorization never enter persistent JSON.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import copy
import json
import ntpath
from pathlib import Path
import uuid
from .hook_protocol import HookKey
from .text import GameTextConfig
from .acquisition import SourceSelection


def identity() -> str:
    return uuid.uuid4().hex


def hook_to_dict(value: HookKey) -> dict:
    return {'code':value.code,'name':value.name,'context':value.context,'context2':value.context2}


def hook_from_dict(value: dict) -> HookKey:
    if not isinstance(value,dict) or set(value) != {'code','name','context','context2'}:
        raise ValueError('invalid persisted HookKey')
    return HookKey(**value)


@dataclass
class GameProfile:
    profile_id: str
    name: str
    text: dict = field(default_factory=dict)
    acquisition: dict = field(default_factory=lambda: SourceSelection().to_dict())
    selected_hooks: list[dict] = field(default_factory=list)
    inserted_hooks: list[str] = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    monitor: dict = field(default_factory=dict)
    screen: dict = field(default_factory=dict)
    overrides: dict = field(default_factory=dict)

    def validate(self) -> None:
        if not self.profile_id or not isinstance(self.name,str) or not self.name.strip():
            raise ValueError('invalid profile identity')
        GameTextConfig.from_dict(self.text)
        SourceSelection.from_dict(self.acquisition)
        hooks = tuple(hook_from_dict(x) for x in self.selected_hooks)
        if len(set(hooks)) != len(hooks):
            raise ValueError('duplicate selected HookKey')
        if (not isinstance(self.inserted_hooks,list)
            or any(not isinstance(x,str) or not x.strip() for x in self.inserted_hooks)
            or any(not isinstance(x,dict) for x in (self.quality,self.monitor,self.screen,self.overrides))):
            raise ValueError('invalid profile settings')
        for forbidden in ('pid','hwnd','host_generation','connection_generation','authorized'):
            if forbidden in self.monitor or forbidden in self.overrides:
                raise ValueError('volatile game session state cannot be saved')

    def source(self) -> SourceSelection:
        return SourceSelection.from_dict(self.acquisition)

    def keys(self) -> tuple[HookKey,...]:
        return tuple(hook_from_dict(x) for x in self.selected_hooks)

    def to_dict(self) -> dict:
        self.validate()
        return copy.deepcopy(vars(self))

    @classmethod
    def from_dict(cls, data: dict) -> 'GameProfile':
        if not isinstance(data,dict) or set(data) - cls.__dataclass_fields__.keys():
            raise ValueError('invalid profile schema')
        profile = cls(**copy.deepcopy(data))
        profile.validate()
        return profile


@dataclass
class GameRecord:
    game_id: str
    display_name: str
    paths: list[str] = field(default_factory=list)
    emulator_game_id: str = ''
    default_profile_id: str = ''
    profiles: dict[str,GameProfile] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.game_id or not self.display_name.strip() or not isinstance(self.paths,list):
            raise ValueError('invalid game record')
        if any(not isinstance(x,str) or not x.strip() for x in self.paths):
            raise ValueError('invalid game executable path')
        if not self.profiles or self.default_profile_id not in self.profiles:
            raise ValueError('game must have a default profile')
        for key,profile in self.profiles.items():
            if key!=profile.profile_id:raise ValueError('profile ID mismatch')
            profile.validate()

    def to_dict(self) -> dict:
        self.validate()
        return {'game_id':self.game_id,'display_name':self.display_name,
                'paths':self.paths[:],'emulator_game_id':self.emulator_game_id,
                'default_profile_id':self.default_profile_id,
                'profiles':{k:v.to_dict() for k,v in self.profiles.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> 'GameRecord':
        if not isinstance(data,dict) or set(data)-cls.__dataclass_fields__.keys():
            raise ValueError('invalid game record schema')
        raw=copy.deepcopy(data)
        raw['profiles']={key:GameProfile.from_dict(value) for key,value in raw.get('profiles',{}).items()}
        record=cls(**raw)
        record.validate()
        return record


@dataclass
class GameCatalog:
    games: dict[str,GameRecord] = field(default_factory=dict)
    active_game_id: str = ''
    active_profile_id: str = ''

    def validate(self):
        for key,game in self.games.items():
            if key!=game.game_id:raise ValueError('game stable ID mismatch')
            game.validate()
        if self.active_game_id and self.active_game_id not in self.games:
            raise ValueError('active game not registered')
        if self.active_profile_id:
            if not self.active_game_id or self.active_profile_id not in self.games[self.active_game_id].profiles:
                raise ValueError('selected profile does not belong to game')

    def to_dict(self) -> dict:
        self.validate()
        return {'games':{k:v.to_dict() for k,v in self.games.items()},
                'active_game_id':self.active_game_id,'active_profile_id':self.active_profile_id}

    @classmethod
    def from_dict(cls, data: dict) -> 'GameCatalog':
        if not isinstance(data,dict) or set(data) != {'games','active_game_id','active_profile_id'}:
            raise ValueError('invalid catalog schema')
        catalog=cls({k:GameRecord.from_dict(v) for k,v in data['games'].items()},
                    data['active_game_id'],data['active_profile_id'])
        catalog.validate()
        return catalog

    def create_game(self, name: str, *, executable: str='', emulator_game_id: str='') -> GameRecord:
        if not isinstance(name,str) or not name.strip():raise ValueError('game name required')
        profile=GameProfile(identity(),'標準')
        record=GameRecord(identity(),name,[executable] if executable else [],
                          emulator_game_id,profile.profile_id,{profile.profile_id:profile})
        record.validate()
        self.games[record.game_id]=record
        self.choose(record.game_id)
        return record

    def choose(self, game_id: str, profile_id: str | None=None) -> None:
        record=self.games[game_id]
        profile_id=profile_id or record.default_profile_id
        if profile_id not in record.profiles:raise ValueError('unknown profile for game')
        self.active_game_id,self.active_profile_id=game_id,profile_id

    def active(self) -> tuple[GameRecord,GameProfile] | None:
        if not self.active_game_id:return None
        record=self.games[self.active_game_id]
        return record,record.profiles[self.active_profile_id or record.default_profile_id]

    def duplicate(self, game_id: str, from_profile_id: str, name: str) -> GameProfile:
        if not name.strip():raise ValueError('profile name required')
        game=self.games[game_id]
        profile=copy.deepcopy(game.profiles[from_profile_id])
        profile.profile_id=identity()
        profile.name=name
        game.profiles[profile.profile_id]=profile
        return profile

    def delete_profile(self, game_id: str, profile_id: str) -> None:
        game=self.games[game_id]
        if profile_id not in game.profiles:raise ValueError('unknown profile')
        if len(game.profiles)==1:raise ValueError('cannot delete last profile')
        del game.profiles[profile_id]
        if game.default_profile_id==profile_id:game.default_profile_id=next(iter(game.profiles))
        if self.active_game_id==game_id and self.active_profile_id==profile_id:
            self.active_profile_id=game.default_profile_id

    def candidates(self, *, process_path: str='', emulator_id: str='') -> list[GameRecord]:
        """Return candidates, NEVER assign one based on foreground alone.

        Emulator ID narrows matches; a common emulator exe does not prove a game.
        """
        normalized=ntpath.normcase(ntpath.normpath(process_path)) if process_path else ''
        result=[]
        for game in self.games.values():
            if emulator_id and game.emulator_game_id and emulator_id!=game.emulator_game_id:
                continue
            path_match=normalized and any(ntpath.normcase(ntpath.normpath(p))==normalized for p in game.paths)
            id_match=emulator_id and game.emulator_game_id==emulator_id
            if id_match or (path_match and not game.emulator_game_id):
                result.append(game)
        return result


def read_luna_531(path: Path) -> list[tuple[str, str, GameProfile]]:
    """Read-only import of the *observed* savegamedata_5.3.1 JSON format.

    Preserve upstream selected order, contexts and original UID. Unknown formats
    fail rather than silently relabel games or HOOKs.
    """
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,list) or len(data)<2 or not isinstance(data[0],list) or not isinstance(data[1],dict):
        raise ValueError('not Luna savegamedata_5.3.1 format')
    result=[]
    for uid in data[0]:
        if not isinstance(uid,str) or uid not in data[1] or not isinstance(data[1][uid],dict):
            raise ValueError('unresolved Luna game UID')
        saved=data[1][uid]
        path_value=saved.get('gamepath','')
        if not isinstance(path_value,str):raise ValueError('invalid Luna game path')
        raw_hooks=saved.get('hook',[])
        if not isinstance(raw_hooks,list):raise ValueError('invalid Luna selected HOOKs')
        selected=[]
        for entry in raw_hooks:
            if (not isinstance(entry,list) or len(entry)!=6 or type(entry[2]) is not int
                or type(entry[3]) is not int or not isinstance(entry[4],str) or not isinstance(entry[5],str)):
                raise ValueError(f'unrecognized Luna saved HOOK for UID {uid}')
            # Luna serialkey = [volatile PID, volatile addr, ctx, ctx2, hookname, hookcode]
            selected.append(hook_to_dict(HookKey(entry[5],entry[4],entry[2],entry[3])))
        added=saved.get('needinserthookcode',[])
        if not isinstance(added,list) or any(not isinstance(x,str) for x in added):
            raise ValueError('invalid Luna manual HOOK codes')
        quality=saved.get('save_text_process_info',{})
        if not isinstance(quality,dict):raise ValueError('invalid Luna quality rules')
        profile=GameProfile(identity(),'Luna取込み',selected_hooks=selected,
                            inserted_hooks=list(added),quality=copy.deepcopy(quality))
        profile.validate()
        result.append((uid,path_value,profile))
    return result


def import_luna(catalog: GameCatalog, path: Path) -> dict[str,str]:
    """Explicit, all-or-nothing import without touching the Luna original."""
    imported=read_luna_531(path)
    scratch=copy.deepcopy(catalog)
    mapping={}
    for uid,executable,profile in imported:
        if uid in mapping:raise ValueError('duplicate Luna UID')
        display=Path(executable).stem or uid
        game=scratch.create_game(display,executable=executable)
        game.profiles={profile.profile_id:profile}
        game.default_profile_id=profile.profile_id
        scratch.choose(game.game_id,profile.profile_id)
        mapping[uid]=game.game_id
    scratch.validate()
    catalog.games=scratch.games
    catalog.active_game_id=scratch.active_game_id
    catalog.active_profile_id=scratch.active_profile_id
    return mapping


@dataclass(frozen=True)
class DiscoveredGame:
    game_id: str
    profile_id: str
    pid: int
    hwnd: int
    process_path: str


def identify_window(catalog: GameCatalog, windows: list[tuple[int,int,str]],
                    paths_by_pid: dict[int,str]) -> tuple[DiscoveredGame | None,str]:
    """Conservative AutoAdv session discovery, independent of foreground state.

    No match when several titles share a binary, several candidate windows are
    present, or a PID is not backed by a verified process executable path.
    The discovery result never authorizes game input or resolves audio roles.
    """
    matches=[]
    for pid,hwnd,title in windows:
        if pid<=0 or hwnd<=0 or not title.strip():continue
        path=paths_by_pid.get(pid,'')
        if not path:continue
        candidates=catalog.candidates(process_path=path)
        if len(candidates)!=1:
            if len(candidates)>1:return None,'ambiguous_game_executable'
            continue
        game=candidates[0]
        matches.append(DiscoveredGame(game.game_id,game.default_profile_id,pid,hwnd,path))
    # Even two windows belonging to the same game are not interchangeable.
    if not matches:return None,'no_registered_window'
    if len(matches)>1:return None,'multiple_possible_windows'
    return matches[0],'unique_registered_window'
