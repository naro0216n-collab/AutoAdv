"""Provision runtime-only media for dedicated Windows RealAudio measurement.

The manifest pins small individual downloads. Assets are cached outside the Git
checkout and copied into an already staged Ren'Py runtime only for the native
phase. Non-redistributable JVS samples therefore never enter the repository,
prebuilt fixture, GitHub artifact, or uploaded validation report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
MANIFEST = HERE / 'REAL_AUDIO_ASSETS.json'


def default_cache_directory() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        return base / 'AutoAdv' / 'validation' / 'media'
    base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'autoadv' / 'validation' / 'media'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def _load_manifest(path: Path = MANIFEST) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 'autoadv-real-audio-assets-v1':
        raise ValueError('unsupported RealAudio asset manifest')
    assets = data.get('assets')
    if not isinstance(assets, list) or not assets:
        raise ValueError('RealAudio asset manifest has no assets')
    ids = set()
    runtime_paths = set()
    for asset in assets:
        required = ('id', 'filename', 'runtime_path', 'sha256', 'max_bytes',
                    'source_page', 'urls', 'license', 'redistributable')
        if any(key not in asset for key in required):
            raise ValueError(f'incomplete RealAudio asset entry: {asset.get("id", "<unknown>")}')
        asset_id = str(asset['id'])
        runtime_path = str(asset['runtime_path']).replace('\\', '/')
        if asset_id in ids or runtime_path in runtime_paths:
            raise ValueError('duplicate RealAudio asset id or runtime path')
        if not runtime_path.startswith('game/audio/runtime/') or '..' in Path(runtime_path).parts:
            raise ValueError(f'unsafe RealAudio runtime path: {runtime_path}')
        if len(str(asset['sha256'])) != 64:
            raise ValueError(f'invalid RealAudio asset digest: {asset_id}')
        if not isinstance(asset['urls'], list) or not asset['urls']:
            raise ValueError(f'RealAudio asset has no individual download URL: {asset_id}')
        if int(asset['max_bytes']) <= 0 or int(asset['max_bytes']) > 16 * 1024 * 1024:
            raise ValueError(f'RealAudio asset is not a bounded small download: {asset_id}')
        size = asset.get('size_bytes')
        if size is not None and (int(size) <= 0 or int(size) > int(asset['max_bytes'])):
            raise ValueError(f'invalid RealAudio expected size: {asset_id}')
        ids.add(asset_id)
        runtime_paths.add(runtime_path)
    return data


def _assert_cache_outside_repository(destination: Path) -> Path:
    resolved = destination.expanduser().resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError:
        return resolved
    raise ValueError('RealAudio media cache must be outside the repository')


def _download(url: str, destination: Path, max_bytes: int) -> None:
    request = urllib.request.Request(url, headers={'User-Agent': 'AutoAdv-RealAudio/1'})
    with urllib.request.urlopen(request, timeout=90) as response, destination.open('wb') as stream:
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError('RealAudio asset exceeded its pinned download size limit')
            stream.write(chunk)


def _cache_path(cache: Path, asset: dict) -> Path:
    suffix = Path(str(asset['filename'])).suffix
    return cache / f"{asset['id']}{suffix}"


def _verify(path: Path, asset: dict) -> None:
    expected_size = asset.get('size_bytes')
    if expected_size is not None and path.stat().st_size != int(expected_size):
        raise ValueError(f"RealAudio asset size mismatch: {asset['id']}")
    actual = _sha256(path)
    if actual != str(asset['sha256']).lower():
        raise ValueError(f"RealAudio asset hash mismatch: {asset['id']}")


def prepare(destination: Path | None = None, *, allow_download: bool = True,
            manifest_path: Path = MANIFEST, downloader=_download) -> dict:
    cache = _assert_cache_outside_repository(destination or default_cache_directory())
    info = _load_manifest(manifest_path)
    cache.mkdir(parents=True, exist_ok=True)
    prepared = []
    for asset in info['assets']:
        target = _cache_path(cache, asset)
        if target.is_file():
            _verify(target, asset)
            prepared.append({'id': asset['id'], 'source': 'cache', 'path': str(target)})
            continue
        if not allow_download:
            raise FileNotFoundError(f"Pinned RealAudio asset is not cached: {asset['id']}")
        errors = []
        for index, url in enumerate(asset['urls']):
            with tempfile.NamedTemporaryFile(
                    dir=cache, prefix=f".{asset['id']}.", suffix='.part', delete=False) as handle:
                temporary = Path(handle.name)
            try:
                downloader(str(url), temporary, int(asset['max_bytes']))
                _verify(temporary, asset)
                os.replace(temporary, target)
                prepared.append({'id': asset['id'], 'source': 'downloaded',
                                 'url_index': index, 'path': str(target)})
                break
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
                temporary.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Unable to acquire pinned RealAudio asset {asset['id']}: "
                               + ' | '.join(errors))
    return {'cache': str(cache), 'assets': prepared}


def hydrate(runtime: Path, cache: Path | None = None, *,
            manifest_path: Path = MANIFEST) -> dict:
    runtime = runtime.expanduser().resolve()
    if not (runtime / 'AutoAdvValidationGame.exe').is_file():
        raise FileNotFoundError('staged validation-game runtime is missing its executable')
    cache = _assert_cache_outside_repository(cache or default_cache_directory())
    info = _load_manifest(manifest_path)
    staged = []
    for asset in info['assets']:
        source = _cache_path(cache, asset)
        if not source.is_file():
            raise FileNotFoundError(f"Pinned RealAudio asset is not cached: {asset['id']}")
        _verify(source, asset)
        target = (runtime / str(asset['runtime_path'])).resolve()
        try:
            target.relative_to(runtime)
        except ValueError as exc:
            raise ValueError(f"unsafe RealAudio runtime target: {asset['id']}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name('.' + target.name + '.part')
        temporary.unlink(missing_ok=True)
        shutil.copyfile(source, temporary)
        _verify(temporary, asset)
        os.replace(temporary, target)
        staged.append({'id': asset['id'], 'runtime_path': str(asset['runtime_path'])})
    return {'runtime': str(runtime), 'assets': staged}


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Cache and hydrate pinned small media for Windows RealAudio measurement')
    parser.add_argument('--cache', type=Path, default=default_cache_directory())
    parser.add_argument('--runtime', type=Path,
                        help='staged validation-game root; copies verified cached media into game/audio/runtime')
    parser.add_argument('--verify-only', action='store_true',
                        help='require all manifest assets to already exist in the persistent cache')
    args = parser.parse_args()
    result = {'prepare': prepare(args.cache, allow_download=not args.verify_only)}
    if args.runtime is not None:
        result['hydrate'] = hydrate(args.runtime, args.cache)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
