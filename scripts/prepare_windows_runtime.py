"""Prepare AutoAdv's shared Windows Python/native runtime and optionally launch it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import venv


def runtime_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "AutoAdv" / "runtime"


RUNTIME_EXTRAS = ("gui", "audio", "ocr", "native")


def runtime_requirements(repo: Path) -> tuple[str, ...]:
    data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    requirements: list[str] = list(project.get("dependencies", ()))
    optional = project.get("optional-dependencies", {})
    for extra in RUNTIME_EXTRAS:
        requirements.extend(optional.get(extra, ()))
    return tuple(dict.fromkeys(str(item) for item in requirements))


def dependency_fingerprint(repo: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"autoadv-runtime-dependencies-v2\0")
    digest.update(f"python={sys.version_info.major}.{sys.version_info.minor}\0".encode())
    for requirement in runtime_requirements(repo):
        digest.update(requirement.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def ensure_environment(repo: Path, root: Path) -> Path:
    environment = root / "python"
    python = environment / "Scripts" / "python.exe"
    marker = environment / ".autoadv-dependencies.json"
    expected = dependency_fingerprint(repo)
    installed = None
    if marker.is_file():
        try:
            installed = json.loads(marker.read_text(encoding="utf-8")).get("fingerprint")
        except (OSError, ValueError):
            installed = None
    if not python.is_file():
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        installed = None
    if installed != expected:
        requirements = runtime_requirements(repo)
        subprocess.run([
            str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--only-binary=:all:", *requirements,
        ], cwd=repo, check=True)
        marker.write_text(json.dumps({
            "fingerprint": expected,
            "requirements": requirements,
        }, indent=2) + "\n", encoding="utf-8")
    return python


def prepare(repo: Path, *, require_native: bool) -> dict:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    python = ensure_environment(repo, root)
    command = [
        str(python), "-m", "autoadv.native_runtime", "prepare",
        "--repo", str(repo), "--runtime-root", str(root / "native"),
    ]
    if not require_native:
        command.append("--allow-missing-helper")
    native = None
    native_error = None
    try:
        completed = subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)
        native = json.loads(completed.stdout.strip().splitlines()[-1])
    except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError) as exc:
        if require_native:
            raise
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
        else:
            detail = str(exc)
        native_error = detail or type(exc).__name__
    return {
        "python": str(python),
        "pythonw": str(python.with_name("pythonw.exe")),
        "runtime_root": str(root),
        "native": native,
        "native_error": native_error,
    }


def main() -> int:
    if os.name != "nt":
        raise SystemExit("AutoAdv Windows runtime preparation requires Windows")
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--require-native", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    repo = args.repo.resolve()
    result = prepare(repo, require_native=args.require_native)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        native = result["native"]
        print(f"AutoAdv runtime ready: {result['runtime_root']}")
        if result.get("native_error"):
            print(f"Native audio runtime is not ready: {result['native_error']}")
        elif native and native.get("helper_error"):
            print(f"Native audio helper is not ready: {native['helper_error']}")
    if args.launch:
        pythonw = Path(result["pythonw"])
        subprocess.Popen([str(pythonw), "-m", "autoadv"], cwd=repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
