#!/usr/bin/env python3
"""Deliberate, one-time adoption of verified upstream Luna release binaries.

Never called from app startup or regular CI. A ZIP may be supplied offline;
--download explicitly fetches the pinned release archive for manual adoption.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen
import zipfile

RELEASE = 'v12.1.2.12'
COMMIT = '51884b6c88cba8c42e98ce3e43c4f408017082a1'
ZIP_SHA256 = '518eb9374f43487f04d0effa23b9c52384d73efad63acad43870f709d16fe0fa'
URL = 'https://github.com/HIllya51/LunaTranslator/releases/download/v12.1.2.12/LunaTranslator_x64.zip'
FILES = ('LunaHost64.dll', 'LunaHook64.dll', 'LunaSubProcess64.exe', 'LunaHook32.dll', 'LunaSubProcess32.exe')


def adopt(archive: bytes, target: Path) -> dict:
    if hashlib.sha256(archive).hexdigest() != ZIP_SHA256:
        raise ValueError('official Luna release ZIP digest differs from pinned metadata')
    if target.exists():
        raise FileExistsError(f'Luna runtime exists (explicit update required): {target}')
    with zipfile.ZipFile(io.BytesIO(archive)) as source, TemporaryDirectory() as temp:
        root = Path(temp) / 'runtime'
        root.mkdir()
        digests = {}
        entries = source.infolist()
        for name in FILES:
            matches = [entry for entry in entries if not entry.is_dir() and Path(entry.filename.replace('\\','/')).name.lower() == name.lower()]
            if len(matches) != 1:
                raise ValueError(f'exactly one packaged {name} required; found {len(matches)}')
            blob = source.read(matches[0])
            (root / name).write_bytes(blob)
            digests[name] = hashlib.sha256(blob).hexdigest()
        manifest = dict(release=RELEASE, commit=COMMIT, asset='LunaTranslator_x64.zip',
                        archive_sha256=ZIP_SHA256, source_url=URL, sha256=digests,
                        license='GPL-3.0, see vendor/luna/LICENSE')
        (root / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        target.parent.mkdir(parents=True, exist_ok=True)
        root.rename(target)
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    paths = parser.add_mutually_exclusive_group(required=True)
    paths.add_argument('--archive', type=Path)
    paths.add_argument('--download', action='store_true')
    parser.add_argument('--destination', type=Path, default=Path('autoadv/luna_assets'))
    args = parser.parse_args()
    blob = urlopen(URL, timeout=180).read() if args.download else args.archive.read_bytes()
    result = adopt(blob, args.destination)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
