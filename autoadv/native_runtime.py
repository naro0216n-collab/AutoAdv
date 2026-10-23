"""Managed Windows runtime assets for AutoAdv native audio judgement.

The product and the Windows advancement-accuracy measurement use the same
pinned ASR model, embedded Haqumei G2P runtime, and Process Loopback helper.
Large ASR weights and build outputs live under the user's local AutoAdv runtime,
not in the source checkout or per-validation-run directories.
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

ASR_REPOSITORY = "twmht/sherpa-onnx-sense-voice-small"
ASR_REVISION = "229c9743cc68c051d09a49aa07fa88b1d4463465"
ASR_FILES = {
    "model.int8.onnx": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    "tokens.txt": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
}
MAX_ASR_FILE_BYTES = 320 * 1024 * 1024
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
    asr_model: str
    asr_tokens: str
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
    asr_root = runtime_root / "asr" / ASR_REVISION
    helper_root = runtime_root / "loopback" / digest
    return NativeRuntimePaths(
        root=str(runtime_root),
        asr_model=str(asr_root / "model.int8.onnx"),
        asr_tokens=str(asr_root / "tokens.txt"),
        helper_dll=str(helper_root / "AutoAdv.ProcessLoopback.dll"),
        helper_digest=digest,
    )


def prepare_asr_model(*, runtime_root: Path | None = None,
                      allow_download: bool = True) -> dict:
    paths = managed_paths(runtime_root=runtime_root)
    destination = Path(paths.asr_model).parent
    destination.mkdir(parents=True, exist_ok=True)
    sources = []
    for filename, expected in ASR_FILES.items():
        target = destination / filename
        if target.exists():
            if _sha256(target) != expected:
                raise ValueError(f"Existing managed ASR asset hash mismatch: {target}")
            sources.append({"file": filename, "sha256": expected, "source": "existing"})
            continue
        if not allow_download:
            raise FileNotFoundError(f"Managed ASR asset is not installed: {target}")
        url = f"https://huggingface.co/{ASR_REPOSITORY}/resolve/{ASR_REVISION}/{filename}"
        with tempfile.NamedTemporaryFile(dir=destination, prefix=f".{filename}.", delete=False) as stream:
            temp = Path(stream.name)
        try:
            total = 0
            digest = hashlib.sha256()
            with urlopen(url, timeout=90) as response, temp.open("wb") as output:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(chunk)
                    if total > MAX_ASR_FILE_BYTES:
                        raise ValueError("Unexpected managed ASR asset size")
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError(f"Downloaded ASR asset digest differs from pinned revision: {filename}")
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        sources.append({"file": filename, "sha256": expected, "source": "downloaded"})
    return {"revision": ASR_REVISION, "directory": str(destination), "files": sources}


def verify_g2p() -> dict:
    # Haqumei's Windows wheel embeds its dictionary/model data. Instantiating the
    # actual product adapter verifies both the Python extension and those assets.
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
    probe = subprocess.run(["dotnet", "--list-sdks"], capture_output=True, text=True, check=True)
    if not any(line.lstrip().startswith("9.") for line in probe.stdout.splitlines()):
        raise RuntimeError(".NET 9 SDK is required for AutoAdv.ProcessLoopback")

    # Use the same ordinary project build that has already been exercised on
    # the target Windows validation machines. The managed runtime copies the
    # result once; source-tree bin/obj are disposable checkout build products.
    subprocess.run(_dotnet_build_command(repo_root), cwd=repo_root, check=True)
    output = repo_root / "integrations/windows-loopback/bin/Release/net9.0-windows"
    missing = [name for name in HELPER_REQUIRED_OUTPUTS if not (output / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "Process Loopback build output incomplete: " + ", ".join(missing))
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
    asr = prepare_asr_model(runtime_root=Path(paths.root), allow_download=allow_download)
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
        "asr": asr,
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
