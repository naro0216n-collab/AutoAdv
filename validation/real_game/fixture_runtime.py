"""Build-time/runtime contract for the prebuilt validation game artifact.

Live AutoAdv validation consumes a ready-to-run game. Ren'Py SDK acquisition
and distribution construction are owned by a separate fixture workflow. Runtime
consumers derive one immutable release asset name from the fixture source and
wait briefly for the automatic builder instead of duplicating release metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import urllib.error
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'renpy'
MANIFEST_NAME = 'fixture-manifest.json'
SCHEMA = 'autoadv-validation-fixture-v1'
FIXTURE_REPOSITORY = 'naro0216n-collab/AutoAdv'
FIXTURE_RELEASE = 'autoadv-validation-fixtures'
FIXTURE_ASSET_PREFIX = 'autoadv-validation-game-'
DEFAULT_WAIT_SECONDS = 600
POLL_SECONDS = 10

_IGNORED_SOURCE_PARTS = {'cache', 'saves', '__pycache__'}
_IGNORED_SOURCE_SUFFIXES = {'.rpyc', '.pyc', '.log'}
_NORMALIZED_TEXT_SUFFIXES = {'.py', '.rpy', '.txt'}


def _iter_source_files(root: Path):
    files = []
    for path in root.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _IGNORED_SOURCE_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in _IGNORED_SOURCE_SUFFIXES:
            continue
        files.append((relative, path))
    # pathlib ordering follows the host path flavour: Windows compares paths
    # case-insensitively while POSIX does not. Fixture identity must not depend
    # on the machine calculating it, so order by one canonical POSIX spelling.
    yield from sorted(files, key=lambda item: item[0].as_posix())


def _digest_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix.lower() in _NORMALIZED_TEXT_SUFFIXES:
        # Fixture identity is content identity, not checkout-platform identity.
        # Git/zip tools may materialize CRLF on Windows; normalize only the
        # known text inputs while leaving licensed audio byte-exact.
        data = data.replace(b'\r\n', b'\n').replace(b'\r', b'\n')
    return data


def _tree_digest(entries) -> str:
    digest = hashlib.sha256()
    for relative, path in entries:
        name = relative.as_posix().encode('utf-8')
        data = _digest_bytes(path)
        digest.update(len(name).to_bytes(4, 'big'))
        digest.update(name)
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.hexdigest()


def source_digest(root: Path = SOURCE) -> str:
    return _tree_digest(_iter_source_files(root))


def asset_name(*, source: Path = SOURCE) -> str:
    return f'{FIXTURE_ASSET_PREFIX}{source_digest(source)}.zip'


def asset_url(*, source: Path = SOURCE, repository: str = FIXTURE_REPOSITORY,
              release: str = FIXTURE_RELEASE) -> str:
    return f'https://github.com/{repository}/releases/download/{release}/{asset_name(source=source)}'


def _download_once(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={'User-Agent': 'AutoAdv-validation-fixture'})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open('wb') as stream:
        shutil.copyfileobj(response, stream)


def acquire_archive(cache: Path, *, source: Path = SOURCE,
                    wait_seconds: int = DEFAULT_WAIT_SECONDS,
                    repository: str = FIXTURE_REPOSITORY,
                    release: str = FIXTURE_RELEASE,
                    clock=time.monotonic, sleep=time.sleep, downloader=_download_once) -> Path:
    """Return the exact immutable fixture asset, waiting for automatic publish.

    A 404 means the source-matched fixture has not reached the release yet. That
    is expected briefly after a develop publish that changes fixture inputs, so
    retry only that status. Other HTTP/network failures remain real setup errors.
    """
    if wait_seconds < 0:
        raise ValueError('wait_seconds must be nonnegative')
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    name = asset_name(source=source)
    destination = cache / name
    if destination.is_file():
        return destination
    url = asset_url(source=source, repository=repository, release=release)
    deadline = clock() + wait_seconds
    announced = False
    while True:
        temporary = cache / f'.{name}.{os.getpid()}.part'
        temporary.unlink(missing_ok=True)
        try:
            downloader(url, temporary)
            os.replace(temporary, destination)
            return destination
        except urllib.error.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            if exc.code != 404 or clock() >= deadline:
                if exc.code == 404:
                    raise FileNotFoundError(
                        f'validation fixture was not published within {wait_seconds}s: {name}; '
                        'automatic Validation Fixture Runtime may have failed') from exc
                raise
            if not announced:
                print(
                    f'Validation fixture {name} is not published yet; waiting for the automatic builder...',
                    file=sys.stderr,
                )
                announced = True
            sleep(min(POLL_SECONDS, max(0, deadline - clock())))
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = (destination / info.filename).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise ValueError(f'fixture archive contains unsafe path: {info.filename}') from exc
        bundle.extractall(destination)


def stage_runtime(runtime: Path, cache: Path, *, source: Path = SOURCE,
                  wait_seconds: int = DEFAULT_WAIT_SECONDS) -> Path:
    """Acquire, expand and verify one source-matched ready-to-run fixture."""
    archive = acquire_archive(cache, source=source, wait_seconds=wait_seconds)
    runtime = runtime.resolve()
    for attempt in range(2):
        if runtime.exists():
            shutil.rmtree(runtime)
        runtime.mkdir(parents=True)
        try:
            _safe_extract(archive, runtime)
            return verify_runtime(runtime, source=source)
        except (OSError, ValueError, zipfile.BadZipFile):
            shutil.rmtree(runtime, ignore_errors=True)
            if attempt:
                raise
            # A truncated/stale cache is never authoritative. Remove it once and
            # reacquire the immutable source-addressed asset from the release.
            archive.unlink(missing_ok=True)
            archive = acquire_archive(cache, source=source, wait_seconds=wait_seconds)
    raise RuntimeError('unreachable fixture staging state')


def write_manifest(runtime: Path, executable: Path, *, source: Path = SOURCE,
                   renpy_version: str, sdk_sha256: str) -> dict:
    runtime = runtime.resolve()
    executable = executable.resolve()
    try:
        executable_relative = executable.relative_to(runtime)
    except ValueError as exc:
        raise ValueError('fixture executable must be inside runtime directory') from exc
    if not executable.is_file() or executable.suffix.lower() != '.exe':
        raise ValueError('fixture executable is not a Windows executable')
    data = {
        'schema': SCHEMA,
        'source_sha256': source_digest(source),
        'renpy_version': str(renpy_version),
        'sdk_sha256': str(sdk_sha256).lower(),
        'executable': executable_relative.as_posix(),
    }
    (runtime / MANIFEST_NAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return data


def verify_runtime(runtime: Path, *, source: Path = SOURCE) -> Path:
    runtime = runtime.resolve()
    manifest_path = runtime / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f'fixture manifest is missing: {manifest_path}')
    data = json.loads(manifest_path.read_text(encoding='utf-8'))
    if data.get('schema') != SCHEMA:
        raise ValueError('unsupported validation fixture manifest')
    expected_source = source_digest(source)
    if data.get('source_sha256') != expected_source:
        raise ValueError('prebuilt validation game does not match current fixture source')
    executable = runtime / str(data.get('executable', ''))
    if not executable.is_file() or executable.suffix.lower() != '.exe':
        raise ValueError('prebuilt validation game executable is missing')
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description='Create, acquire or verify the prebuilt validation-game runtime')
    sub = parser.add_subparsers(dest='command', required=True)

    create = sub.add_parser('manifest')
    create.add_argument('--runtime', type=Path, required=True)
    create.add_argument('--executable', type=Path, required=True)
    create.add_argument('--source', type=Path, default=SOURCE)
    create.add_argument('--renpy-version', required=True)
    create.add_argument('--sdk-sha256', required=True)

    digest = sub.add_parser('source-digest')
    digest.add_argument('--source', type=Path, default=SOURCE)

    verify = sub.add_parser('verify')
    verify.add_argument('--runtime', type=Path, required=True)
    verify.add_argument('--source', type=Path, default=SOURCE)

    stage = sub.add_parser('stage')
    stage.add_argument('--runtime', type=Path, required=True)
    stage.add_argument('--cache', type=Path, required=True)
    stage.add_argument('--source', type=Path, default=SOURCE)
    stage.add_argument('--wait-seconds', type=int, default=DEFAULT_WAIT_SECONDS)

    args = parser.parse_args()
    if args.command == 'source-digest':
        print(source_digest(args.source))
    elif args.command == 'manifest':
        result = write_manifest(
            args.runtime, args.executable, source=args.source,
            renpy_version=args.renpy_version, sdk_sha256=args.sdk_sha256)
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == 'stage':
        print(stage_runtime(args.runtime, args.cache, source=args.source,
                            wait_seconds=args.wait_seconds))
    else:
        print(verify_runtime(args.runtime, source=args.source))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
