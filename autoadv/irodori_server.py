"""Irodori installed-folder integration; startup never installs or updates anything."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from urllib import request, error, parse
from typing import Callable
import json
import os
import subprocess
import socket
import time

_AUDIO_SUFFIXES = ('.wav','.flac','.mp3','.m4a','.ogg','.opus','.aac','.webm','.pt','.pth','.speaker.safetensors')


def locate_python(root: Path) -> Path:
    for relative in ('.venv/Scripts/python.exe','.venv/bin/python','venv/Scripts/python.exe','venv/bin/python'):
        executable = root / relative
        if executable.is_file():return executable
    raise ValueError('existing Irodori Python environment not found (.venv or venv)')


def validate_folder(folder: str | Path) -> Path:
    root = Path(folder).expanduser().resolve()
    if not root.is_dir() or not (root/'irodori_openai_tts').is_dir() and not (root/'pyproject.toml').is_file():
        raise ValueError('not an installed Irodori server root')
    locate_python(root)
    return root


def _server_env(root:Path) -> dict[str,str]:
    env=os.environ.copy()
    # Read only declared paths in installed .env; do not execute shell content.
    dot_env=root/'.env'
    if dot_env.is_file():
        for line in dot_env.read_text(encoding='utf-8-sig').splitlines():
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line:continue
            key,value=line.split('=',1)
            key=key.strip()
            if key in ('IRODORI_VOICES_DIR','IRODORI_VOICE_ALIASES_FILE'):
                env[key]=value.strip().strip('"\'')
    return env


def local_voice_ids(root:Path) -> tuple[str,...]:
    env=_server_env(root)
    voice_dir=Path(env.get('IRODORI_VOICES_DIR') or root/'voices')
    if not voice_dir.is_absolute():voice_dir=root/voice_dir
    aliases=Path(env.get('IRODORI_VOICE_ALIASES_FILE') or voice_dir/'voices.json')
    if not aliases.is_absolute():aliases=root/aliases
    names=set()
    if voice_dir.is_dir():
        for path in voice_dir.iterdir():
            if path.is_file():
                suffix=next((ext for ext in _AUDIO_SUFFIXES if path.name.lower().endswith(ext)),None)
                if suffix:names.add(path.name[:-len(suffix)])
    if aliases.is_file():
        mapping=json.loads(aliases.read_text(encoding='utf-8-sig'))
        if not isinstance(mapping,dict):raise ValueError('voice alias JSON must be an object')
        names.update(k for k in mapping if isinstance(k,str) and k)
    return tuple(sorted(names))


def _parse_api_voices(body:object) -> tuple[str,...]:
    values=body.get('voices',body.get('data',[])) if isinstance(body,dict) else body
    if not isinstance(values,list):raise ValueError('Irodori voice response must contain a list')
    ids=[]
    for row in values:
        value=row if isinstance(row,str) else row.get('id') if isinstance(row,dict) else None
        if not isinstance(value,str) or not value:raise ValueError('invalid Voice ID response')
        ids.append(value)
    return tuple(sorted(set(ids)))


@dataclass(frozen=True)
class ServerState:
    status: str
    origin: str   # owned, external, none
    voices: tuple[str,...]
    local_candidates: tuple[str,...]
    difference: tuple[str,...]
    message: str = ''


class IrodoriServer:
    def __init__(self,root:Path | None=None,*,url:str='http://127.0.0.1:8088',
                 opener:Callable|None=None, process_factory:Callable|None=None,
                 clock:Callable[[],float]=time.monotonic):
        self.root=root
        parsed=parse.urlsplit(url)
        if parsed.scheme!='http' or parsed.hostname not in ('localhost','127.0.0.1','::1') or parsed.path.strip('/'):
            raise ValueError('automatic launch requires a local HTTP endpoint')
        self.url=url.rstrip('/')
        self.opener=opener or request.urlopen
        self.process_factory=process_factory or subprocess.Popen
        self.clock=clock
        self.owned_process=None
        self.state=ServerState('unconfigured','none',(),(),())

    def _api_voices(self) -> tuple[str,...]:
        # A generic HTTP service on this port must never be accepted as Irodori.
        health=request.Request(self.url+'/health',headers={'Accept':'application/json'})
        with self.opener(health,timeout=3) as response:
            fingerprint=json.loads(response.read())
        if (not isinstance(fingerprint,dict) or fingerprint.get('status')!='ok'
                or not isinstance(fingerprint.get('model'),dict)
                or not isinstance(fingerprint.get('runtime'),dict)
                or not isinstance(fingerprint.get('voices'),dict)):
            raise ValueError('The occupied HTTP endpoint is not identified as Irodori')
        url=self.url+'/v1/audio/voices'
        req=request.Request(url,headers={'Accept':'application/json'})
        with self.opener(req,timeout=3) as response:
            return _parse_api_voices(json.loads(response.read()))

    def refresh(self) -> ServerState:
        candidates=local_voice_ids(self.root) if self.root else ()
        try:
            api=self._api_voices()
        except error.HTTPError as exc:
            self.state=ServerState('other_service','none',(),candidates,(),f'Voice API HTTP {exc.code}; refusing to start another server')
            return self.state
        except (error.URLError,TimeoutError,ConnectionError,OSError) as exc:
            self.state=ServerState('offline','none',(),candidates,(),type(exc).__name__)
            return self.state
        except (ValueError,json.JSONDecodeError,KeyError) as exc:
            self.state=ServerState('other_service','none',(),candidates,(),str(exc))
            return self.state
        origin='owned' if self.owned_process is not None and self.owned_process.poll() is None else 'external'
        difference=tuple(sorted(set(candidates).symmetric_difference(api)))
        self.state=ServerState('connected',origin,api,candidates,difference,
                               'Selected server identity/folder cannot be verified' if origin=='external' else '')
        return self.state

    def connect_or_start(self, *, timeout_sec:float=15) -> ServerState:
        current=self.refresh()
        if current.status!='offline' or not self.root:return current
        parsed=parse.urlsplit(self.url)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(.5)
            if sock.connect_ex((parsed.hostname or '127.0.0.1',parsed.port or 8088)) == 0:
                self.state=ServerState('other_service','none',(),current.local_candidates,(),
                                       'Port is occupied without a verified Irodori Voice API')
                return self.state
        root=validate_folder(self.root)
        if self.owned_process is None or self.owned_process.poll() is not None:
            python=locate_python(root)
            self.owned_process=self.process_factory([str(python),'-m','irodori_openai_tts','--host','127.0.0.1',
                                                    '--port',str(parse.urlsplit(self.url).port or 8088)],
                                                   cwd=str(root),env=_server_env(root),
                                                   stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                                                   stderr=subprocess.DEVNULL)
        end=self.clock()+timeout_sec
        while self.clock()<end:
            current=self.refresh()
            if current.status!='offline':return current
            if self.owned_process.poll() is not None:break
            time.sleep(.15)
        return self.refresh()

    def close(self) -> None:
        process=self.owned_process
        self.owned_process=None
        if process is not None and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
