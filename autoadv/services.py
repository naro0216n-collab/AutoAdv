"""Single Application-facing service for profiles, preview and server control."""
from __future__ import annotations
from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import os
import shutil
import tempfile
import uuid
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

    def monitor_path(self,game:str|None=None) -> Path|None:
        location=self.preferences.live_profiles.get(game or self.preferences.selected_game)
        return Path(location) if location else None

    def monitor_document(self,game:str|None=None) -> tuple[dict,dict]:
        from .live import LiveConfig
        from .screen import ScreenProfile
        path=self.monitor_path(game)
        if path is None:return {},{}
        # The same loaders validate both GUI and the actual runtime profile.
        config=LiveConfig.load(path)
        screen=ScreenProfile.load(config.screen_profile)
        data=json.loads(path.read_text(encoding='utf-8'))
        observed=json.loads(config.screen_profile.read_text(encoding='utf-8'))
        return data,observed

    def save_monitor(self,game:str,document:dict,screen_document:dict) -> Path:
        """Stage a complete, validated game snapshot then switch one preferences pointer.

        The old profile remains usable until the preferences write succeeds;
        runtime and CLI both load the same standard LiveConfig/ScreenProfile JSON.
        """
        if not game.strip():raise ValueError('game name is required')
        from .live import LiveConfig
        from .screen import ScreenProfile
        profiles=self.settings_file.parent/'profiles'
        profiles.mkdir(parents=True,exist_ok=True)
        staged=Path(tempfile.mkdtemp(prefix='.staging-',dir=profiles))
        destination=profiles/('game-'+hashlib.sha256(game.encode('utf-8')).hexdigest()[:16]
                              +'-'+uuid.uuid4().hex[:12])
        original=self.preferences.live_profiles.get(game)
        original_legacy=self.preferences.live_config
        installed=False
        try:
            screen_data=json.loads(json.dumps(screen_document,ensure_ascii=False))
            screen_path=staged/'screen.json'
            screen_path.write_text(json.dumps(screen_data,ensure_ascii=False,indent=2),encoding='utf-8')
            screen=ScreenProfile.load(screen_path)
            for role,marker in screen.markers.items():
                if not marker.image.is_file():raise ValueError(f'{role}: テンプレート画像が見つかりません: {marker.image}')
            live_data=json.loads(json.dumps(document,ensure_ascii=False))
            live_data['screen_profile']=str(destination/'screen.json')
            live_path=staged/'monitor.json'
            live_path.write_text(json.dumps(live_data,ensure_ascii=False,indent=2),encoding='utf-8')
            config=LiveConfig.load(live_path)
            if any(not path.is_file() for path in
                   (config.loopback_helper,config.asr_model,config.asr_tokens)):
                raise ValueError('loopback helper / ASRモデル / tokens のファイルを確認してください')
            os.replace(staged,destination)
            installed=True
            saved=destination/'monitor.json'
            self.preferences.live_profiles[game]=str(saved)
            # Once imported, the previous global live_config is not retained as
            # a competing runtime selection route.
            self.preferences.live_config=''
            self.preferences.save(self.settings_file)
            return saved
        except Exception:
            if original is None:self.preferences.live_profiles.pop(game,None)
            else:self.preferences.live_profiles[game]=original
            self.preferences.live_config=original_legacy
            if installed:shutil.rmtree(destination,ignore_errors=True)
            raise
        finally:
            if staged.exists():shutil.rmtree(staged,ignore_errors=True)

    def close(self):
        self.autoplay_allowed=False
        self.server.close()
