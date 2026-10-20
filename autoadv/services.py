"""Single Application-facing service for profiles, preview and server control."""
from __future__ import annotations
from pathlib import Path
from dataclasses import asdict
from .preferences import Preferences, settings_path
from .irodori_server import IrodoriServer,ServerState
from .text import TextInterpreter,GameTextConfig,Utterance


class AutoAdvService:
    def __init__(self,*,settings_file:Path | None=None):
        self.settings_file=settings_file or settings_path()
        self.preferences=Preferences.load(self.settings_file)
        self.server=IrodoriServer(Path(self.preferences.irodori_root) if self.preferences.irodori_root else None,
                                  url=self.preferences.irodori_url)
        self.latest_original=''
        self.luna_connected=False
        self.autoplay_allowed=False  # never persisted
        self.stop_reason='Not authorized for automatic input'

    def configure_server(self,folder:str)->None:
        self.preferences.set_irodori_root(folder)
        self.preferences.save(self.settings_file)
        self.server.close()
        self.server=IrodoriServer(Path(self.preferences.irodori_root),url=self.preferences.irodori_url)

    def connect_server(self) -> ServerState:
        return self.server.connect_or_start()

    def refresh_voices(self) -> ServerState:
        return self.server.refresh()

    def current_config(self) -> GameTextConfig:
        return self.preferences.game_config()

    def preview(self,raw:str) -> Utterance:
        return TextInterpreter(self.current_config()).parse(raw)

    def save_game(self,name:str,config:GameTextConfig)->None:
        if not name.strip():raise ValueError('game name is required')
        old_selected=self.preferences.selected_game
        old_game=self.preferences.games.get(name)
        self.preferences.games[name]=config.to_dict()
        self.preferences.selected_game=name
        try:self.preferences.save(self.settings_file)
        except Exception:
            self.preferences.selected_game=old_selected
            if old_game is None:self.preferences.games.pop(name,None)
            else:self.preferences.games[name]=old_game
            raise

    def close(self):
        self.autoplay_allowed=False
        self.server.close()
