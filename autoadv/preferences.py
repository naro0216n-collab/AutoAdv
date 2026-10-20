"""Application settings used identically by the GUI and live Application service."""
from __future__ import annotations
from dataclasses import dataclass,field
from pathlib import Path
import json
import os
import tempfile
from .text import GameTextConfig
from .irodori_server import validate_folder


def settings_path() -> Path:
    home=Path(os.getenv('APPDATA') or Path.home()/'.config')
    return home/'AutoAdv'/'settings.json'


@dataclass
class Preferences:
    irodori_root: str = ''
    irodori_url: str = 'http://127.0.0.1:8088'
    luna_url: str = 'ws://127.0.0.1:2333/api/ws/text/origin'
    selected_game: str = ''
    games: dict[str,dict] = field(default_factory=dict)
    live_config: str = ''

    @classmethod
    def load(cls,path:Path | None=None) -> 'Preferences':
        path=path or settings_path()
        if not path.is_file():return cls()
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or set(data)-set(cls.__dataclass_fields__):
            raise ValueError('invalid AutoAdv preferences')
        prefs=cls(**data)
        for name,value in prefs.games.items():
            if not isinstance(name,str) or not isinstance(value,dict):raise ValueError('invalid game entry')
            GameTextConfig.from_dict(value)
        if prefs.irodori_root:
            # A missing install after a move must not suppress the GUI start; surface it on connection.
            Path(prefs.irodori_root)
        return prefs

    def game_config(self) -> GameTextConfig:
        return GameTextConfig.from_dict(self.games.get(self.selected_game,{}))

    def save(self,path:Path | None=None) -> None:
        path=path or settings_path()
        for entry in self.games.values():GameTextConfig.from_dict(entry)
        path.parent.mkdir(parents=True,exist_ok=True)
        from dataclasses import asdict
        data=json.dumps(asdict(self),ensure_ascii=False,indent=2)
        descriptor,tmp=tempfile.mkstemp(dir=path.parent,prefix='.settings-',suffix='.json')
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)

    def set_irodori_root(self,folder:str)->None:
        self.irodori_root=str(validate_folder(folder))
