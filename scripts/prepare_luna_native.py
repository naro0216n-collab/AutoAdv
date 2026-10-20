#!/usr/bin/env python3
"""Prepare a build-only LunaHook tree with a traceable, narrowly scoped native patch.

Never modifies vendor/luna, and verifies the pinned source manifest before
turning on the existing cross-bitness Host injection implementation.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil

HOST_DLL = Path('src/NativeImpl/LunaHook/LunaHost/LunaHostDll.cpp')
HOST_CMAKE = Path('src/NativeImpl/LunaHook/LunaHost/CMakeLists.txt')


def prepare(source: Path, target: Path) -> None:
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('source and build-copy must be separate directories')
    manifest = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['revision'] != '51884b6c88cba8c42e98ce3e43c4f408017082a1':
        raise ValueError('unexpected upstream commit')
    for filename in (str(HOST_DLL), str(HOST_CMAKE)):
        digest = hashlib.sha256((source / filename).read_bytes()).hexdigest()
        if digest != manifest['sha256'][filename]:
            raise ValueError(f'modified upstream input: {filename}')
    if target.exists():
        raise FileExistsError(f'build-copy exists: {target}')
    shutil.copytree(source / 'src', target / 'src')
    dll = target / HOST_DLL
    data = dll.read_text(encoding='utf-8-sig')
    old = '#if 0\nC_LUNA_API void Luna_ConnectAndInjectProcess'
    new = 'C_LUNA_API void Luna_ConnectAndInjectProcess'
    if data.count(old) != 1 or data.count('\n#endif\nC_LUNA_API bool Luna_CheckIfNeedInject') != 1:
        raise ValueError('upstream Luna native injection export changed')
    data = data.replace(old, new).replace('\n#endif\nC_LUNA_API bool Luna_CheckIfNeedInject', '\nC_LUNA_API bool Luna_CheckIfNeedInject')
    dll.write_text(data, encoding='utf-8')
    cmake = target / HOST_CMAKE
    data = cmake.read_text(encoding='utf-8-sig')
    old = 'add_library(host host.cpp textthread.cpp ../include/InfoStrings.cpp)'
    if data.count(old) != 1:
        raise ValueError('upstream Host build definition changed')
    cmake.write_text(data.replace(old, 'add_library(host host.cpp injector.cpp textthread.cpp ../include/InfoStrings.cpp)'), encoding='utf-8')
    (target / 'NATIVE_PATCH.json').write_text(json.dumps({
        'upstream_revision': manifest['revision'],
        'purpose': 'Enable unmodified upstream Host cross-bitness injection routine in build copy',
        'patched_paths': [str(HOST_DLL), str(HOST_CMAKE)]
    }, indent=2) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path('vendor/luna'))
    parser.add_argument('--target', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.source, args.target)


if __name__ == '__main__':
    main()
