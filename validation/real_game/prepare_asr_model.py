"""Explicitly provision the pinned real ASR model for dedicated verification.

No model download occurs during ordinary application startup or Fast CI.
The files remain out of Git and are checked against committed SHA-256 values.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / 'ASR_MODEL.json'
# Keep large, licensed model assets out of the source checkout. This is a
# dedicated validation cache, separate from the model location configured by
# the AutoAdv product for ordinary game sessions.
def default_model_directory() -> Path:
    if os.name == 'nt':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
        return base / 'AutoAdv' / 'validation' / 'asr'
    base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'autoadv' / 'validation' / 'asr'


MODEL_DIR = default_model_directory()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def prepare(destination: Path = MODEL_DIR, *, allow_download: bool = True) -> dict:
    # Even an explicit --directory must not install licensed weights in Git.
    if destination.expanduser().resolve().is_relative_to(HERE.parents[1].resolve()):
        raise ValueError('ASR verification model directory must be outside the repository')
    info = json.loads(MANIFEST.read_text(encoding='utf-8'))
    destination.mkdir(parents=True, exist_ok=True)
    sources = []
    for filename, expected in info['files'].items():
        if filename not in ('model.int8.onnx', 'tokens.txt'):
            raise ValueError('Only pinned SenseVoice files may be downloaded')
        target = destination / filename
        if target.exists():
            if _sha256(target) != expected:
                raise ValueError(f'Existing model asset hash mismatch; refusing overwrite: {target}')
            sources.append({'file': filename, 'sha256': expected, 'source': 'existing'})
            continue
        if not allow_download:
            raise FileNotFoundError(f'Pinned model asset is not installed: {target}')
        url = f"https://huggingface.co/{info['repository']}/resolve/{info['revision']}/{filename}"
        # Stream rather than allocating hundreds of MB. Avoid publishing a
        # partially downloaded file, including on checksum failure.
        with tempfile.NamedTemporaryFile(dir=destination, prefix=f'.{filename}.', delete=False) as f:
            temp = Path(f.name)
        try:
            total = 0
            h = hashlib.sha256()
            with urlopen(url, timeout=90) as response, temp.open('wb') as stream:
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    total += len(chunk)
                    if total > 320 * 1024 * 1024:
                        raise ValueError('Unexpected ASR model asset size')
                    h.update(chunk)
                    stream.write(chunk)
            if h.hexdigest() != expected:
                raise ValueError(f'Downloaded ASR asset digest differs from pinned revision: {filename}')
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        sources.append({'file': filename, 'sha256': expected, 'source': 'downloaded'})
    return {'revision': info['revision'], 'directory': str(destination), 'files': sources}


def main() -> int:
    parser = argparse.ArgumentParser(description='Install or verify the pinned SenseVoice real ASR model')
    parser.add_argument('--directory', type=Path, default=MODEL_DIR)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    result = prepare(args.directory, allow_download=not args.verify_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
