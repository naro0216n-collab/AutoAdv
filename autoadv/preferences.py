"""The one persistent AutoAdv configuration: common environment and game catalog.

Legacy preferences and Luna settings are read only through an explicit,
source-preserving, one-time import. Runtime PIDs/HWND and authorization are never
written here. No parallel settings stores are used by the Application.
"""
from __future__ import annotations
from dataclasses import dataclass,field
from pathlib import Path
import copy
import hashlib
import json
import os
import tempfile
from .profiles import GameCatalog,import_luna
from .text import GameTextConfig
from .irodori_server import validate_folder


def settings_path() -> Path:
    home=Path(os.getenv('APPDATA') or Path.home()/'.config')
    return home/'AutoAdv'/'settings.json'


class LegacyPreferencesDetected(ValueError):
    """Migration requires a deliberate user action, never an implicit write."""


@dataclass
class Preferences:
    irodori_root: str = ''
    irodori_url: str = 'http://127.0.0.1:8088'
    luna_host_dll: str = ''
    globals: dict[str,object] = field(default_factory=dict)
    catalog: GameCatalog = field(default_factory=GameCatalog)

    SCHEMA=2

    @classmethod
    def load(cls,path:Path | None=None) -> 'Preferences':
        path=path or settings_path()
        if not path.is_file():return cls()
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict):raise ValueError('invalid preferences JSON')
        if data.get('schema_version')!=cls.SCHEMA:
            if ('games' in data or 'live_profiles' in data or 'luna_url' in data
                or 'selected_game' in data or 'live_config' in data):
                raise LegacyPreferencesDetected('旧AutoAdv設定の読取り専用移行を選択してください')
            raise ValueError('unsupported preferences version')
        if set(data)!={'schema_version','irodori_root','irodori_url','luna_host_dll','globals','catalog'}:
            raise ValueError('invalid preferences fields')
        if (not all(isinstance(data[k],str) for k in ('irodori_root','irodori_url','luna_host_dll'))
            or not isinstance(data['globals'],dict)):
            raise ValueError('invalid common settings')
        catalog=GameCatalog.from_dict(data['catalog'])
        return cls(data['irodori_root'],data['irodori_url'],data['luna_host_dll'],data['globals'],catalog)

    def document(self) -> dict:
        if not isinstance(self.globals,dict):raise ValueError('global settings must be a mapping')
        return {'schema_version':self.SCHEMA,'irodori_root':self.irodori_root,
                'irodori_url':self.irodori_url,'luna_host_dll':self.luna_host_dll,
                'globals':copy.deepcopy(self.globals),'catalog':self.catalog.to_dict()}

    def save(self,path:Path | None=None) -> None:
        path=path or settings_path()
        serialized=json.dumps(self.document(),ensure_ascii=False,indent=2)
        path.parent.mkdir(parents=True,exist_ok=True)
        descriptor,tmp=tempfile.mkstemp(dir=path.parent,prefix='.settings-',suffix='.json')
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)

    def game_config(self) -> GameTextConfig:
        selected=self.catalog.active()
        return GameTextConfig.from_dict(selected[1].text if selected else {})

    def set_irodori_root(self,folder:str)->None:
        self.irodori_root=str(validate_folder(folder))

    @classmethod
    def import_legacy(cls, path:Path) -> 'Preferences':
        """User-confirmed, atomic replacement; immutable checksum-named backup."""
        original=path.read_bytes()
        raw=json.loads(original)
        if not isinstance(raw,dict) or 'schema_version' in raw:
            raise ValueError('not a supported legacy AutoAdv settings file')
        allowed={'irodori_root','irodori_url','luna_url','selected_game','games','live_profiles','live_config'}
        if set(raw)-allowed:raise ValueError('unrecognized legacy setting keys')
        games=raw.get('games',{})
        monitors=raw.get('live_profiles',{})
        if not isinstance(games,dict) or not isinstance(monitors,dict):
            raise ValueError('invalid legacy game maps')
        prefs=cls(irodori_root=raw.get('irodori_root',''),
                  irodori_url=raw.get('irodori_url','http://127.0.0.1:8088'))
        mapping={}
        for name in dict.fromkeys([*games,*monitors]):
            if not isinstance(name,str) or not name.strip():raise ValueError('invalid old game name')
            game=prefs.catalog.create_game(name)
            mapping[name]=game.game_id
            profile=game.profiles[game.default_profile_id]
            profile.text=GameTextConfig.from_dict(games.get(name,{})).to_dict()
            if name in monitors:
                from .live import LiveConfig
                source=Path(monitors[name])
                conf=LiveConfig.load(source)
                doc=json.loads(source.read_text(encoding='utf-8'))
                # Runtime process identity is re-established for each session.
                doc.pop('pid',None);doc.pop('hwnd',None);doc.pop('screen_profile',None)
                for key in ('loopback_helper','asr_model','asr_tokens','log_path'):
                    doc[key]=str(getattr(conf,key))
                # Old dialogue/readiness markers never proved input safety.
                # Keep their original data in the checksum backup, not in the
                # new profile as a purported positive advancement provider.
                profile.monitor=doc
        selected=raw.get('selected_game','')
        if selected and selected in mapping:
            prefs.catalog.choose(mapping[selected])
        prefs.document()  # verify fully BEFORE replacing the source
        digest=hashlib.sha256(original).hexdigest()[:16]
        backup=path.with_name(path.name+'.legacy-'+digest+'.bak')
        if backup.exists():
            if backup.read_bytes()!=original:raise ValueError('legacy backup collision')
        else:
            with backup.open('xb') as stream:
                stream.write(original)
                stream.flush();os.fsync(stream.fileno())
        prefs.save(path)
        return prefs

    def import_luna(self,path:Path,settings_file:Path|None=None)->dict[str,str]:
        cloned=copy.deepcopy(self.catalog)
        mapping=import_luna(cloned,path)
        old=self.catalog
        try:
            self.catalog=cloned
            self.save(settings_file)
        except BaseException:
            self.catalog=old
            raise
        return mapping
