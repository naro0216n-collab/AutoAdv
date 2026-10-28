"""Provision runtime-only media for Windows auto-advance accuracy measurement.

The manifest pins small individual downloads. Assets are cached outside the Git
checkout and copied into an already staged Ren'Py runtime only for the native
phase. Non-redistributable JVS samples therefore never enter the repository,
prebuilt fixture, GitHub artifact, or uploaded validation report.
"""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from http.cookiejar import CookieJar
from urllib.parse import urlencode, urljoin, urlparse
import urllib.request

HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
MANIFEST = HERE / 'ADVANCEMENT_ACCURACY_ASSETS.json'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'AppleWebKit/537.36 AutoAdv-AdvancementAccuracy/1.0')


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
    if data.get('schema') != 'autoadv-advancement-accuracy-assets-v1':
        raise ValueError('unsupported advancement-accuracy asset manifest')
    assets = data.get('assets')
    if not isinstance(assets, list) or not assets:
        raise ValueError('advancement-accuracy asset manifest has no assets')
    ids = set()
    runtime_paths = set()
    for asset in assets:
        required = ('id', 'filename', 'runtime_path', 'sha256', 'max_bytes',
                    'source_page', 'urls', 'license', 'redistributable')
        if any(key not in asset for key in required):
            raise ValueError(f'incomplete advancement-accuracy asset entry: {asset.get("id", "<unknown>")}')
        asset_id = str(asset['id'])
        runtime_path = str(asset['runtime_path']).replace('\\', '/')
        if asset_id in ids or runtime_path in runtime_paths:
            raise ValueError('duplicate advancement-accuracy asset id or runtime path')
        if not runtime_path.startswith('game/audio/runtime/') or '..' in Path(runtime_path).parts:
            raise ValueError(f'unsafe advancement-accuracy runtime path: {runtime_path}')
        if len(str(asset['sha256'])) != 64:
            raise ValueError(f'invalid advancement-accuracy asset digest: {asset_id}')
        if not isinstance(asset['urls'], list) or not asset['urls']:
            raise ValueError(f'advancement-accuracy asset has no individual download URL: {asset_id}')
        if int(asset['max_bytes']) <= 0 or int(asset['max_bytes']) > 16 * 1024 * 1024:
            raise ValueError(f'advancement-accuracy asset is not a bounded small download: {asset_id}')
        size = asset.get('size_bytes')
        if size is not None and (int(size) <= 0 or int(size) > int(asset['max_bytes'])):
            raise ValueError(f'invalid advancement-accuracy expected size: {asset_id}')
        ids.add(asset_id)
        runtime_paths.add(runtime_path)
    return data


def _assert_cache_outside_repository(destination: Path) -> Path:
    resolved = destination.expanduser().resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError:
        return resolved
    raise ValueError('Advancement accuracy media cache must be outside the repository')


class _DriveDownloadForm(HTMLParser):
    """Extract a public Google Drive confirmation form without browser automation."""

    def __init__(self):
        super().__init__()
        self.action: str | None = None
        self.method = 'get'
        self.fields: dict[str, str] = {}
        self._inside = False

    def handle_starttag(self, tag: str, attrs) -> None:
        values = {str(key).lower(): str(value or '') for key, value in attrs}
        if tag.lower() == 'form' and (
                values.get('id') in ('download-form', 'downloadForm') or
                'drive.usercontent.google.com/download' in values.get('action', '')):
            self._inside = True
            self.action = values.get('action') or self.action
            self.method = (values.get('method') or 'get').lower()
            return
        if self._inside and tag.lower() == 'input':
            name = values.get('name')
            if name:
                self.fields[name] = values.get('value', '')

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == 'form' and self._inside:
            self._inside = False


def _request_headers(url: str) -> dict[str, str]:
    host = urlparse(url).hostname or ''
    headers = {'User-Agent': USER_AGENT, 'Accept': '*/*'}
    if host.endswith('opengameart.org'):
        headers['Referer'] = 'https://opengameart.org/'
    elif host.endswith('google.com') or host.endswith('googleusercontent.com'):
        headers['Referer'] = 'https://drive.google.com/'
    elif host.endswith('githubusercontent.com'):
        headers['Referer'] = 'https://github.com/'
    return headers


def _write_response(response, destination: Path, max_bytes: int) -> tuple[int, str]:
    total = 0
    content_type = str(response.headers.get('Content-Type', '')).lower()
    with destination.open('wb') as stream:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError('advancement-accuracy asset exceeded its pinned download size limit')
            stream.write(chunk)
    return total, content_type


def _drive_confirmation(path: Path) -> tuple[str, str, dict[str, str]] | None:
    # Public Drive can return a small confirmation form instead of the file.
    # Parse only HTML-looking responses; actual WAV/OGG bytes are never decoded.
    data = path.read_bytes()
    prefix = data[:1024].lstrip().lower()
    if not (prefix.startswith(b'<!doctype html') or prefix.startswith(b'<html')
            or b'<form' in prefix):
        return None
    document = data.decode('utf-8', errors='replace')
    parser = _DriveDownloadForm()
    parser.feed(document)
    if not parser.action or not parser.fields:
        return None
    return parser.action, parser.method, parser.fields


def _download(url: str, destination: Path, max_bytes: int) -> None:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def fetch(target: str, *, data: bytes | None = None) -> str:
        request = urllib.request.Request(
            target, data=data, headers=_request_headers(target),
            method='POST' if data is not None else 'GET')
        with opener.open(request, timeout=90) as response:
            _, content_type = _write_response(response, destination, max_bytes)
        return content_type

    content_type = fetch(url)
    host = urlparse(url).hostname or ''
    if 'google' not in host:
        return
    followup = _drive_confirmation(destination)
    if followup is None:
        if content_type.startswith('text/html'):
            raise ValueError('Google Drive returned HTML instead of the pinned media file')
        return
    action, method, fields = followup
    target = urljoin(url, action)
    encoded = urlencode(fields)
    if method == 'post':
        followup_type = fetch(target, data=encoded.encode('utf-8'))
    else:
        separator = '&' if '?' in target else '?'
        followup_type = fetch(target + separator + encoded)
    if (_drive_confirmation(destination) is not None
            or followup_type.startswith('text/html')):
        raise ValueError('Google Drive confirmation did not resolve to the media file')


def _cache_path(cache: Path, asset: dict) -> Path:
    suffix = Path(str(asset['filename'])).suffix
    return cache / f"{asset['id']}{suffix}"


def _verify(path: Path, asset: dict) -> None:
    expected_size = asset.get('size_bytes')
    if expected_size is not None and path.stat().st_size != int(expected_size):
        raise ValueError(f"advancement-accuracy asset size mismatch: {asset['id']}")
    actual = _sha256(path)
    if actual != str(asset['sha256']).lower():
        raise ValueError(f"advancement-accuracy asset hash mismatch: {asset['id']}")


def prepare(destination: Path | None = None, *, allow_download: bool = True,
            manifest_path: Path = MANIFEST, downloader=_download) -> dict:
    cache = _assert_cache_outside_repository(destination or default_cache_directory())
    info = _load_manifest(manifest_path)
    cache.mkdir(parents=True, exist_ok=True)
    prepared = []
    count = len(info['assets'])
    for position, asset in enumerate(info['assets'], start=1):
        target = _cache_path(cache, asset)
        if target.is_file():
            _verify(target, asset)
            print(f"Advancement accuracy media [{position}/{count}] {asset['id']}: cache")
            prepared.append({'id': asset['id'], 'source': 'cache', 'path': str(target)})
            continue
        if not allow_download:
            raise FileNotFoundError(f"Pinned advancement-accuracy asset is not cached: {asset['id']}")
        errors = []
        for index, url in enumerate(asset['urls']):
            with tempfile.NamedTemporaryFile(
                    dir=cache, prefix=f".{asset['id']}.", suffix='.part', delete=False) as handle:
                temporary = Path(handle.name)
            try:
                host = urlparse(str(url)).hostname or '<unknown-host>'
                print(f"Advancement accuracy media [{position}/{count}] {asset['id']}: "
                      f"download {index + 1}/{len(asset['urls'])} from {host}")
                downloader(str(url), temporary, int(asset['max_bytes']))
                _verify(temporary, asset)
                os.replace(temporary, target)
                prepared.append({'id': asset['id'], 'source': 'downloaded',
                                 'url_index': index, 'path': str(target)})
                break
            except Exception as exc:
                errors.append(f'{type(exc).__name__}: {exc}')
                print(f"Advancement accuracy media {asset['id']}: source {index + 1} failed: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                temporary.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Unable to acquire pinned advancement-accuracy asset {asset['id']}: "
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
            raise FileNotFoundError(f"Pinned advancement-accuracy asset is not cached: {asset['id']}")
        _verify(source, asset)
        target = (runtime / str(asset['runtime_path'])).resolve()
        try:
            target.relative_to(runtime)
        except ValueError as exc:
            raise ValueError(f"unsafe advancement-accuracy runtime target: {asset['id']}") from exc
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
        description='Cache and hydrate pinned small media for Windows auto-advance accuracy measurement')
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
