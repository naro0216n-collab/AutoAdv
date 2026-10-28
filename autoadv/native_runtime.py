"""Managed Windows assets for target-conditioned native-audio alignment.

The standard runtime owns pinned phone-CTC and human-vocal ONNX artifacts,
Haqumei G2P, and the Process Loopback helper. Model weights remain outside the
source checkout and are downloaded once into the user's AutoAdv runtime.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.request import urlopen

ALIGNMENT_REPOSITORY = "air-air/japanese-hubert-base-phoneme-ctc-v4-ONNX"
ALIGNMENT_REVISION = "a36617c5fbf7cfde02caa067716961c03cf4d87e"
ALIGNMENT_REMOTE_MODEL = "onnx/model_q4.onnx"
ALIGNMENT_MODEL_SHA256 = "0151f5bd899a86aab0d34c06dba74689e60a95e6dce0a6642b6c005b74aef630"
ALIGNMENT_MODEL_BYTES = 63_010_793
MAX_ALIGNMENT_FILE_BYTES = 80 * 1024 * 1024

HUMAN_VOCAL_REPOSITORY = "audiomagic/yamnet-onnx"
HUMAN_VOCAL_REVISION = "f25b741c2f0bdc6d7e6db24b5fddda23347dbafd"
HUMAN_VOCAL_REMOTE_MODEL = "yamnet.onnx"
HUMAN_VOCAL_MODEL_SHA256 = "d3835ffbbd4a1bb3e777f0ca217b5007907f5171dd5d17c4236b95b2af8f908e"
HUMAN_VOCAL_MODEL_BYTES = 16_093_355
MAX_HUMAN_VOCAL_FILE_BYTES = 24 * 1024 * 1024

# Exact vocabulary of the ONNX source model.  It is deliberately kept beside
# the pinned artifact contract rather than fetched from an independent moving
# revision.  PAD=0 is the CTC blank consumed by the backend adapter.
ALIGNMENT_VOCAB = {
    "EOS": 3, "I": 9, "N": 28, "PAD": 0, "SOS": 2, "U": 10, "UNK": 1,
    "a": 4, "b": 19, "by": 36, "ch": 31, "cl": 29, "d": 16, "dy": 43,
    "e": 7, "f": 25, "fy": 41, "g": 12, "gw": 45, "gy": 34, "h": 18,
    "hy": 35, "i": 5, "j": 26, "k": 11, "kw": 44, "ky": 33, "m": 21,
    "my": 38, "n": 17, "ny": 39, "o": 8, "p": 20, "pau": 46, "py": 37,
    "r": 23, "ry": 40, "s": 13, "sh": 30, "sil": 47, "t": 15, "ts": 32,
    "ty": 42, "u": 6, "v": 27, "w": 24, "y": 22, "z": 14,
}

HELPER_FILES = (
    "integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj",
    "integrations/windows-loopback/Program.cs",
)
HELPER_REQUIRED_OUTPUTS = (
    "AutoAdv.ProcessLoopback.dll",
    "AutoAdv.ProcessLoopback.deps.json",
    "AutoAdv.ProcessLoopback.runtimeconfig.json",
)


@dataclass(frozen=True)
class NativeRuntimePaths:
    root: str
    alignment_model: str
    alignment_vocab: str
    human_vocal_model: str
    helper_dll: str
    helper_digest: str


def default_runtime_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "AutoAdv" / "runtime" / "native"
    base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "autoadv" / "runtime" / "native"


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def helper_source_digest(repo_root: Path | None = None) -> str:
    repo_root = (repo_root or default_repo_root()).resolve()
    digest = hashlib.sha256()
    for relative in HELPER_FILES:
        path = repo_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"Process Loopback helper source missing: {path}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def managed_paths(*, repo_root: Path | None = None,
                  runtime_root: Path | None = None) -> NativeRuntimePaths:
    runtime_root = (runtime_root or default_runtime_root()).expanduser().resolve()
    digest = helper_source_digest(repo_root)
    alignment_root = runtime_root / "alignment" / ALIGNMENT_REVISION
    human_vocal_root = runtime_root / "human-vocal" / HUMAN_VOCAL_REVISION
    helper_root = runtime_root / "loopback" / digest
    return NativeRuntimePaths(
        root=str(runtime_root),
        alignment_model=str(alignment_root / "model.q4.onnx"),
        alignment_vocab=str(alignment_root / "vocab.json"),
        human_vocal_model=str(human_vocal_root / "yamnet.onnx"),
        helper_dll=str(helper_root / "AutoAdv.ProcessLoopback.dll"),
        helper_digest=digest,
    )


def _download_verified(*, url: str, destination: Path, expected_sha256: str,
                       expected_bytes: int, max_bytes: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".model.", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        total = 0
        digest = hashlib.sha256()
        with urlopen(url, timeout=90) as response, temporary.open("wb") as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                total += len(chunk)
                if total > int(max_bytes):
                    raise ValueError("Unexpected managed model size")
                digest.update(chunk)
                output.write(chunk)
        if total != int(expected_bytes) or digest.hexdigest() != expected_sha256:
            raise ValueError("Downloaded managed model differs from pinned artifact")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_alignment_model(*, runtime_root: Path | None = None,
                            allow_download: bool = True) -> dict:
    paths = managed_paths(runtime_root=runtime_root)
    destination = Path(paths.alignment_model).parent
    destination.mkdir(parents=True, exist_ok=True)
    model = Path(paths.alignment_model)
    source = "existing"
    if model.exists():
        if _sha256(model) != ALIGNMENT_MODEL_SHA256:
            raise ValueError(f"Existing managed alignment model hash mismatch: {model}")
    else:
        if not allow_download:
            raise FileNotFoundError(f"Managed alignment model is not installed: {model}")
        url = (f"https://huggingface.co/{ALIGNMENT_REPOSITORY}/resolve/"
               f"{ALIGNMENT_REVISION}/{ALIGNMENT_REMOTE_MODEL}")
        _download_verified(
            url=url, destination=model,
            expected_sha256=ALIGNMENT_MODEL_SHA256,
            expected_bytes=ALIGNMENT_MODEL_BYTES,
            max_bytes=MAX_ALIGNMENT_FILE_BYTES,
        )
        source = "downloaded"

    vocab = Path(paths.alignment_vocab)
    expected_vocab = json.dumps(ALIGNMENT_VOCAB, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if not vocab.exists() or vocab.read_text(encoding="utf-8") != expected_vocab:
        vocab.write_text(expected_vocab, encoding="utf-8")
    return {
        "repository": ALIGNMENT_REPOSITORY,
        "revision": ALIGNMENT_REVISION,
        "source_model": "prj-beatrice/japanese-hubert-base-phoneme-ctc-v4",
        "license": "Apache-2.0",
        "notice": str(Path(__file__).resolve().parent / "alignment_assets" / "NOTICE.txt"),
        "directory": str(destination),
        "model": {"sha256": ALIGNMENT_MODEL_SHA256, "bytes": ALIGNMENT_MODEL_BYTES,
                  "source": source},
        "vocab": str(vocab),
    }


def prepare_human_vocal_model(*, runtime_root: Path | None = None,
                              allow_download: bool = True) -> dict:
    paths = managed_paths(runtime_root=runtime_root)
    model = Path(paths.human_vocal_model)
    source = "existing"
    if model.exists():
        if (model.stat().st_size != HUMAN_VOCAL_MODEL_BYTES
                or _sha256(model) != HUMAN_VOCAL_MODEL_SHA256):
            raise ValueError(f"Existing managed human-vocal model hash mismatch: {model}")
    else:
        if not allow_download:
            raise FileNotFoundError(f"Managed human-vocal model is not installed: {model}")
        url = (f"https://huggingface.co/{HUMAN_VOCAL_REPOSITORY}/resolve/"
               f"{HUMAN_VOCAL_REVISION}/{HUMAN_VOCAL_REMOTE_MODEL}")
        _download_verified(
            url=url, destination=model,
            expected_sha256=HUMAN_VOCAL_MODEL_SHA256,
            expected_bytes=HUMAN_VOCAL_MODEL_BYTES,
            max_bytes=MAX_HUMAN_VOCAL_FILE_BYTES,
        )
        source = "downloaded"
    return {
        "repository": HUMAN_VOCAL_REPOSITORY,
        "revision": HUMAN_VOCAL_REVISION,
        "source_model": "google/yamnet/1",
        "license": "Apache-2.0",
        "notice": str(Path(__file__).resolve().parent / "human_vocal_assets" / "NOTICE.txt"),
        "model": {"sha256": HUMAN_VOCAL_MODEL_SHA256,
                  "bytes": HUMAN_VOCAL_MODEL_BYTES, "source": source},
        "path": str(model),
    }


def verify_g2p() -> dict:
    from .g2p import HaqumeiG2P
    candidates = HaqumeiG2P().candidates("東京へ行く", max_candidates=2)
    if not candidates:
        raise RuntimeError("Haqumei G2P runtime returned no pronunciation")
    return {"backend": "haqumei", "ready": True}


def _dotnet_build_command(repo_root: Path) -> list[str]:
    return [
        "dotnet", "build",
        str(repo_root / "integrations/windows-loopback/AutoAdv.ProcessLoopback.csproj"),
        "--configuration", "Release", "--nologo",
    ]


def prepare_helper(*, repo_root: Path | None = None,
                   runtime_root: Path | None = None) -> dict:
    repo_root = (repo_root or default_repo_root()).resolve()
    paths = managed_paths(repo_root=repo_root, runtime_root=runtime_root)
    helper = Path(paths.helper_dll)
    target = helper.parent
    if all((target / name).is_file() for name in HELPER_REQUIRED_OUTPUTS):
        return {"digest": paths.helper_digest, "directory": str(target), "source": "existing"}
    if os.name != "nt":
        raise OSError("Process Loopback helper build requires Windows")
    if shutil.which("dotnet") is None:
        raise FileNotFoundError(".NET 9 SDK is required for AutoAdv.ProcessLoopback")
    probe = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, check=True)
    if not any(line.lstrip().startswith(b"9.") for line in probe.stdout.splitlines()):
        raise RuntimeError(".NET 9 SDK is required for AutoAdv.ProcessLoopback")
    # dotnet diagnostics are not part of this module's machine-readable stdout
    # contract.  Keep them inside the helper preparation boundary so a localized
    # or UTF-8 CLI cannot corrupt the parent process' JSON transport.
    subprocess.run(
        _dotnet_build_command(repo_root), cwd=repo_root, check=True, capture_output=True)
    output = repo_root / "integrations/windows-loopback/bin/Release/net9.0-windows"
    missing = [name for name in HELPER_REQUIRED_OUTPUTS if not (output / name).is_file()]
    if missing:
        raise FileNotFoundError("Process Loopback build output incomplete: " + ", ".join(missing))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for item in output.iterdir():
        destination = target / item.name
        if item.is_file():
            shutil.copy2(item, destination)
        elif item.is_dir():
            shutil.copytree(item, destination)
    return {"digest": paths.helper_digest, "directory": str(target), "source": "built"}


def prepare(*, repo_root: Path | None = None, runtime_root: Path | None = None,
            allow_download: bool = True, require_helper: bool = True) -> dict:
    repo_root = (repo_root or default_repo_root()).resolve()
    paths = managed_paths(repo_root=repo_root, runtime_root=runtime_root)
    alignment = prepare_alignment_model(runtime_root=Path(paths.root), allow_download=allow_download)
    human_vocal = prepare_human_vocal_model(
        runtime_root=Path(paths.root), allow_download=allow_download)
    g2p = verify_g2p()
    helper = None
    helper_error = None
    try:
        helper = prepare_helper(repo_root=repo_root, runtime_root=Path(paths.root))
    except Exception as exc:
        if require_helper:
            raise
        helper_error = str(exc)
    return {
        "paths": asdict(paths),
        "alignment": alignment,
        "human_vocal": human_vocal,
        "g2p": g2p,
        "helper": helper,
        "helper_error": helper_error,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare AutoAdv managed native-audio runtime")
    parser.add_argument("prepare", nargs="?")
    parser.add_argument("--repo", type=Path, default=default_repo_root())
    parser.add_argument("--runtime-root", type=Path, default=default_runtime_root())
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--allow-missing-helper", action="store_true")
    args = parser.parse_args()
    result = prepare(
        repo_root=args.repo,
        runtime_root=args.runtime_root,
        allow_download=not args.verify_only,
        require_helper=not args.allow_missing_helper,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
