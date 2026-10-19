#!/usr/bin/env python3
"""Reproducible port of Space-Idle's Publish Gateway control plane.

This is a one-time, offline-*reviewable* source acquisition step, NOT an
alternate publisher. It downloads upstream from an immutable develop commit,
rewrites only repository identity and private ref namespace, and refuses to
silently overwrite maintained assets. It does not create or mutate any branch.

The usual source-snapshot -> publish_request.py init -> prepare ->
connector-plan -> fixed launcher -> record flow remains unchanged.
"""
from __future__ import annotations
import argparse
import pathlib
import re
import urllib.request

UPSTREAM_COMMIT = "ece390a07f24bbcc3f3b5a44e7c672ed198f5c2c"
UPSTREAM_REPO = "naro0216n-collab/Space-Idle"
TARGET_REPO = "naro0216n-collab/AutoAdv"

FILES = (
    "scripts/publish_request.py",
    "scripts/publish_gateway_validate.py",
    "scripts/publish_connector_launch.js",
    "scripts/publish_connector_executor.js",
    "scripts/workflow_maintenance.py",
    "scripts/publish_control_maintenance.py",
    ".github/workflows/publish-gateway.yml",
)


def adapt(content: str, *, require_identity: bool = True) -> str:
    if require_identity and "Space-Idle" not in content and "space-idle" not in content:
        raise ValueError("unexpected source file: lacks upstream identity")
    result = content.replace(UPSTREAM_REPO, TARGET_REPO)
    result = result.replace("Space-Idle", "AutoAdv")
    result = result.replace("space-idle", "autoadv")
    # Covers uppercase variables in maintenance scripts where relevant.
    if UPSTREAM_REPO in result or "refs/space-idle/" in result:
        raise ValueError("unported source identity")
    return result


def port(root: pathlib.Path, *, write: bool) -> list[pathlib.Path]:
    prepared = []
    for rel in FILES:
        url = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{UPSTREAM_COMMIT}/{rel}"
        with urllib.request.urlopen(url, timeout=20) as response:
            raw = response.read().decode("utf-8")
        dest = root / rel
        if dest.exists():
            raise FileExistsError(f"refusing overwrite: {dest}")
        prepared.append((dest, adapt(raw, require_identity=rel != ".github/workflows/publish-gateway.yml")))
    if write:
        for dest, data in prepared:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(data, encoding="utf-8")
    return [path for path, _ in prepared]


def main() -> int:
    p = argparse.ArgumentParser(description="Port exact Space-Idle Publish Gateway sources to AutoAdv")
    p.add_argument("--write", action="store_true", help="write files only after all downloads validate")
    args = p.parse_args()
    root = pathlib.Path(__file__).resolve().parent.parent
    for path in port(root, write=args.write):
        print(path.relative_to(root))
    print("All files acquired and mapped. This does NOT publish or initialize branches.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())