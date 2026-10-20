"""Single Application service for a stable game/profile catalog and Irodori."""
from __future__ import annotations
from pathlib import Path
import copy
import hashlib
import json
import tempfile
from .preferences import Preferences,settings_path
from .irodori_server import IrodoriServer,ServerState
from .text import TextInterpreter,GameTextConfig,Utterance
from .profiles import GameProfile,GameRecord


class AutoAdvService:
    def __init__(self,*,settings_file:Path | None=None):
        self.settings_file=settings_file or settings_path()
        self.preferences=Preferences.load(self.settings_file)
        self.server=IrodoriServer(Path(self.preferences.irodori_root) if self.preferences.irodori_root else None,
                                  url=self.preferences.irodori_url)
        self.latest_original=''
        self.hook_connected=False
        self.autoplay_allowed=False
        self.stop_reason='Not authorized for automatic input'

    @property
    def catalog(self):return self.preferences.catalog

    def active(self) -> tuple[GameRecord,GameProfile] | None:
        return self.catalog.active()

    def current_game_name(self) -> str:
        current=self.active()
        return current[0].display_name if current else ''

    def current_profile_name(self) -> str:
        current=self.active()
        return current[1].name if current else ''

    def select(self,game_id:str,profile_id:str|None=None)->None:
        self.catalog.choose(game_id,profile_id)
        self.preferences.save(self.settings_file)
        self.autoplay_allowed=False
        self.stop_reason='Game/profile changed, input disallowed'
        self.latest_original=''

    def new_game(self,name:str,executable:str='')->GameRecord:
        before=copy.deepcopy(self.catalog)
        try:
            game=self.catalog.create_game(name,executable=executable)
            self.preferences.save(self.settings_file)
            return game
        except BaseException:
            self.preferences.catalog=before
            raise

    def save_game(self,name:str,config:GameTextConfig)->None:
        if not name.strip():raise ValueError('game name required')
        before=copy.deepcopy(self.catalog)
        try:
            current=self.active()
            if current is None:self.catalog.create_game(name)
            elif current[0].display_name!=name:
                # Editing a display name must not silently reassign another
                # stable game; explicit new_game creates a different title.
                current[0].display_name=name
            self.active()[1].text=config.to_dict()
            self.preferences.save(self.settings_file)
        except BaseException:
            self.preferences.catalog=before
            raise

    def add_game_executable(self,executable:str)->None:
        """Bind a known launch path to one stable game without changing its ID."""
        import ntpath
        selected=self.active()
        if selected is None:raise ValueError('select a game first')
        path=str(Path(executable).expanduser().resolve())
        if not path.lower().endswith('.exe') or not Path(path).is_file():
            raise ValueError('select an existing game executable')
        old=copy.deepcopy(selected[0].paths)
        try:
            canonical=ntpath.normcase(ntpath.normpath(path))
            if all(ntpath.normcase(ntpath.normpath(item))!=canonical for item in old):
                selected[0].paths.append(path)
            self.preferences.save(self.settings_file)
        except BaseException:
            selected[0].paths=old
            raise

    def launch_current_game(self):
        """Explicit user action, never a background startup side effect."""
        import subprocess
        selected=self.active()
        if selected is None or not selected[0].paths:
            raise ValueError('register the game executable first')
        path=Path(selected[0].paths[0])
        if not path.is_file():raise FileNotFoundError('registered game path is no longer available')
        return subprocess.Popen([str(path)],cwd=str(path.parent),shell=False)

    def profile_duplicate(self,name:str)->GameProfile:
        before=copy.deepcopy(self.catalog)
        try:
            game,profile=self.active()
            new=self.catalog.duplicate(game.game_id,profile.profile_id,name)
            self.catalog.choose(game.game_id,new.profile_id)
            self.preferences.save(self.settings_file)
            self.autoplay_allowed=False
            return new
        except BaseException:
            self.preferences.catalog=before
            raise

    def profile_rename(self,name:str)->None:
        if not name.strip():raise ValueError('profile name required')
        current=self.active()
        if current is None:raise ValueError('no game selected')
        old=current[1].name
        try:
            current[1].name=name
            self.preferences.save(self.settings_file)
        except BaseException:
            current[1].name=old
            raise

    def profile_set_default(self)->None:
        current=self.active()
        if current is None:raise ValueError('no game selected')
        game,profile=current
        previous=game.default_profile_id
        try:
            game.default_profile_id=profile.profile_id
            self.preferences.save(self.settings_file)
        except BaseException:
            game.default_profile_id=previous
            raise

    def profile_delete(self)->None:
        before=copy.deepcopy(self.catalog)
        try:
            game,profile=self.active()
            self.catalog.delete_profile(game.game_id,profile.profile_id)
            self.preferences.save(self.settings_file)
            self.autoplay_allowed=False
        except BaseException:
            self.preferences.catalog=before
            raise

    def save_hook_selection(self,keys,inserted=())->None:
        from .profiles import hook_to_dict
        current=self.active()
        if current is None:raise ValueError('no game selected')
        old=copy.deepcopy(current[1])
        try:
            current[1].selected_hooks=[hook_to_dict(x) for x in keys]
            current[1].inserted_hooks=list(inserted)
            self.preferences.save(self.settings_file)
            self.autoplay_allowed=False
        except BaseException:
            current[0].profiles[old.profile_id]=old
            raise

    def configure_acquisition(self,selection)->None:
        """One persisted source choice; no implicit fallback or parallel execution."""
        from .acquisition import SourceSelection
        if not isinstance(selection,SourceSelection):
            raise TypeError('acquisition selection required')
        current=self.active()
        if current is None:raise ValueError('select game/profile first')
        previous=copy.deepcopy(current[1])
        try:
            current[1].acquisition=selection.to_dict()
            self.preferences.save(self.settings_file)
            self.autoplay_allowed=False
            self.stop_reason='Source changed; reverify game and grant before automation'
        except BaseException:
            current[0].profiles[previous.profile_id]=previous
            raise

    def configure_server(self,folder:str)->None:
        self.preferences.set_irodori_root(folder)
        self.preferences.save(self.settings_file)
        self.server.close()
        self.server=IrodoriServer(Path(self.preferences.irodori_root),url=self.preferences.irodori_url)

    def connect_server(self)->ServerState:
        return self.server.connect_or_start()

    def refresh_voices(self)->ServerState:
        return self.server.refresh()

    def current_config(self)->GameTextConfig:
        return self.preferences.game_config()

    def preview(self,raw:str)->Utterance:
        return TextInterpreter(self.current_config()).parse(raw)

    def monitor_document(self)->dict:
        current=self.active()
        return copy.deepcopy(current[1].monitor) if current else {}

    def save_monitor(self,document:dict)->None:
        """Persist audio/input settings; never persist ephemeral PIDs or HWNDs."""
        if not self.active():raise ValueError('game not selected')
        from .live import LiveConfig
        live=copy.deepcopy(document)
        for name in ('pid','hwnd','screen_profile'):
            live.pop(name,None)
        with tempfile.TemporaryDirectory(prefix='autoadv-validate-') as tmp:
            root=Path(tmp)
            probe={**live,'pid':1,'hwnd':1}
            lfile=root/'monitor.json'
            lfile.write_text(json.dumps(probe,ensure_ascii=False),encoding='utf-8')
            config=LiveConfig.load(lfile)
            for file in (config.loopback_helper,config.asr_model,config.asr_tokens):
                if not file.is_file():raise ValueError(f'missing runtime dependency: {file}')
        previous=copy.deepcopy(self.active()[1])
        try:
            self.active()[1].monitor=live
            self.preferences.save(self.settings_file)
        except BaseException:
            self.active()[0].profiles[previous.profile_id]=previous
            raise

    def runtime_snapshot(self,pid:int,hwnd:int):
        """Derive one runtime config without creating a secondary settings authority."""
        data=self.monitor_document()
        if not data:raise ValueError('monitor audio settings not saved')
        if pid<=0 or hwnd<=0:raise ValueError('select a live game window')
        temp_root=self.settings_file.parent/'temp'
        temp_root.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(prefix='live-',dir=temp_root)
        root=Path(temporary.name)
        data.update({'pid':pid,'hwnd':hwnd})
        live_file=root/'monitor.json'
        live_file.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
        from .live import LiveConfig
        try:config=LiveConfig.load(live_file)
        except BaseException:
            temporary.cleanup()
            raise
        return config,temporary

    def close(self):
        self.autoplay_allowed=False
        self.server.close()
