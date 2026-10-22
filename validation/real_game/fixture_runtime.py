"""Build-time/runtime contract for the prebuilt validation game artifact.

Live AutoAdv validation consumes a ready-to-run game. Ren'Py SDK acquisition
and distribution construction are owned by a separate fixture workflow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'renpy'
MANIFEST_NAME = 'fixture-manifest.json'
SCHEMA = 'autoadv-validation-fixture-v1'

_IGNORED_SOURCE_PARTS = {'cache', 'saves', '__pycache__'}
_IGNORED_SOURCE_SUFFIXES = {'.rpyc', '.pyc', '.log'}


def _iter_source_files(root: Path):
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        relative = path.relative_to(root)
        if any(part in _IGNORED_SOURCE_PARTS for part in relative.parts):
            continue
        if path.suffix.lower() in _IGNORED_SOURCE_SUFFIXES:
            continue
        yield relative, path


def _tree_digest(entries) -> str:
    digest = hashlib.sha256()
    for relative, path in entries:
        name = relative.as_posix().encode('utf-8')
        data = path.read_bytes()
        digest.update(len(name).to_bytes(4, 'big'))
        digest.update(name)
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.hexdigest()


def source_digest(root: Path = SOURCE) -> str:
    return _tree_digest(_iter_source_files(root))


def runtime_digest(root: Path) -> str:
    files = []
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        relative = path.relative_to(root)
        if relative.as_posix() == MANIFEST_NAME:
            continue
        files.append((relative, path))
    if not files:
        raise ValueError('validation fixture runtime is empty')
    return _tree_digest(files)


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
        'runtime_sha256': runtime_digest(runtime),
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
    if data.get('runtime_sha256') != runtime_digest(runtime):
        raise ValueError('prebuilt validation game runtime hash mismatch')
    executable = runtime / str(data.get('executable', ''))
    if not executable.is_file() or executable.suffix.lower() != '.exe':
        raise ValueError('prebuilt validation game executable is missing')
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description='Create or verify the prebuilt validation-game manifest')
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

    args = parser.parse_args()
    if args.command == 'source-digest':
        print(source_digest(args.source))
    elif args.command == 'manifest':
        result = write_manifest(
            args.runtime, args.executable, source=args.source,
            renpy_version=args.renpy_version, sdk_sha256=args.sdk_sha256)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(verify_runtime(args.runtime, source=args.source))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
