"""Explicitly adopted Luna release assets; never download or patch on startup.

Luna's public Host DLL creates named pipes through Luna_ConnectProcess;
LunaSubProcess performs the architecture-specific hook DLL injection.  These
paths are from one release and must be adopted together.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import struct

RELEASE = 'v12.1.2.12'
COMMIT = '51884b6c88cba8c42e98ce3e43c4f408017082a1'
FILES = (
    'LunaHost64.dll', 'LunaHook64.dll', 'LunaSubProcess64.exe',
    'LunaHook32.dll', 'LunaSubProcess32.exe',
)


def runtime_dir() -> Path:
    return Path(__file__).resolve().parent / 'luna_assets'


@dataclass(frozen=True)
class LunaRuntime:
    directory: Path

    def verify(self) -> None:
        manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('release') != RELEASE or manifest.get('commit') != COMMIT:
            raise ValueError('Luna release provenance does not match the adopted version')
        for name in FILES:
            path = self.directory / name
            if not path.is_file():
                raise FileNotFoundError(f'adopted Luna asset missing: {path}')
            if hashlib.sha256(path.read_bytes()).hexdigest() != manifest['sha256'][name]:
                raise ValueError(f'Luna runtime asset differs from adopted release: {name}')

    @property
    def host(self) -> Path:
        return self.directory / 'LunaHost64.dll'

    def injection(self, pid: int, is_64bit: bool) -> tuple[Path, Path]:
        suffix = '64' if is_64bit else '32'
        return (self.directory / f'LunaSubProcess{suffix}.exe',
                self.directory / f'LunaHook{suffix}.dll')


def adopted_runtime() -> LunaRuntime:
    runtime = LunaRuntime(runtime_dir())
    runtime.verify()
    return runtime


def is_process_64bit(pid: int) -> bool:
    """Report target process bitness with Windows kernel32; do not guess from Host."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.IsWow64Process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'Cannot inspect game process architecture')
    try:
        wow64 = wintypes.BOOL()
        if not kernel.IsWow64Process(handle, ctypes.byref(wow64)):
            raise OSError(ctypes.get_last_error(), 'IsWow64Process failed')
        return struct.calcsize('P') == 8 and not wow64.value
    finally:
        kernel.CloseHandle(handle)


def inject(pid: int, runtime: LunaRuntime) -> None:
    """Invoke Luna's own isolated subprocess. A return code of 1 means success."""
    import subprocess
    executable, hook = runtime.injection(pid, is_process_64bit(pid))
    result = subprocess.run([str(executable), 'dllinject', str(pid), str(hook)],
                            cwd=str(runtime.directory), capture_output=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=20)
    if result.returncode != 1:
        raise RuntimeError(f'Luna DLL injection failed (return code {result.returncode})')
