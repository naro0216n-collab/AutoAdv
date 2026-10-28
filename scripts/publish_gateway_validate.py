#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import binascii
import os
import re
import subprocess
from pathlib import Path

TRUSTED_CONTROL_WORKFLOW = ".github/workflows/publish-gateway.yml"
PUBLISH_BUNDLE_REF = "refs/autoadv/publish-request"
ALLOWED_TARGET_BRANCHES = {"develop", "temp"}


class GatewayRequestError(RuntimeError):
    pass


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _transport_parts(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise GatewayRequestError(f"transport directory is missing: {directory}")
    entries = sorted(path for path in directory.iterdir() if path.is_file())
    expected = [f"{index:04d}.b64" for index in range(len(entries))]
    actual = [path.name for path in entries]
    if not entries or actual != expected:
        raise GatewayRequestError(
            f"transport slot must contain one contiguous 0000.b64 sequence: {actual}"
        )
    return entries


def _decode_transport(directory: Path) -> bytes:
    parts = _transport_parts(directory)
    chunks: list[str] = []
    for path in parts:
        try:
            text = path.read_text(encoding="ascii")
        except UnicodeDecodeError as exc:
            raise GatewayRequestError(f"transport part is not ASCII Base64: {path.name}") from exc
        if not text or re.fullmatch(r"[A-Za-z0-9+/=]+", text) is None:
            raise GatewayRequestError(f"transport part contains invalid Base64 characters: {path.name}")
        chunks.append(text)
    try:
        return base64.b64decode("".join(chunks), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GatewayRequestError(f"invalid transport Base64: {exc}") from exc


def _verify_bundle(bundle_path: Path, target_branch: str) -> tuple[str, str, str]:
    try:
        subprocess.run(
            ["git", "bundle", "verify", str(bundle_path)], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        heads = _git("bundle", "list-heads", str(bundle_path)).splitlines()
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        raise GatewayRequestError(f"invalid publish bundle: {(stderr or '').strip()}") from exc
    parsed = [line.split(maxsplit=1) for line in heads if line.strip()]
    if len(parsed) != 1 or len(parsed[0]) != 2 or parsed[0][1] != PUBLISH_BUNDLE_REF:
        raise GatewayRequestError(f"bundle must advertise exactly {PUBLISH_BUNDLE_REF}")
    advertised_commit = parsed[0][0]
    incoming_ref = f"refs/autoadv/incoming/{os.environ.get('GITHUB_RUN_ID', 'gateway')}"
    try:
        subprocess.run(
            ["git", "fetch", "--quiet", str(bundle_path), f"{PUBLISH_BUNDLE_REF}:{incoming_ref}"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        publish_commit = _git("rev-parse", incoming_ref)
        if publish_commit != advertised_commit:
            raise GatewayRequestError("bundle advertised commit does not match fetched commit")
        base_sha = _git("rev-parse", f"{publish_commit}^")
        target_tree = _git("rev-parse", f"{publish_commit}^{{tree}}")
    finally:
        subprocess.run(
            ["git", "update-ref", "-d", incoming_ref], check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    remote_ref = f"refs/remotes/origin/{target_branch}"
    try:
        current = _git("rev-parse", remote_ref)
    except subprocess.CalledProcessError as exc:
        raise GatewayRequestError(f"checkout is missing {remote_ref}") from exc
    if current != base_sha:
        raise GatewayRequestError(
            f"target moved before publish: expected bundle parent {base_sha}, checkout has {current}"
        )
    return publish_commit, base_sha, target_tree


def verify_trusted_workflow() -> int:
    base_sha = os.environ.get("BASE_SHA", "")
    publish_commit = os.environ.get("PUBLISH_COMMIT", "")
    control_commit = os.environ.get("GITHUB_SHA", "")
    if not base_sha or not publish_commit or not control_commit:
        raise GatewayRequestError("trusted workflow verification environment is incomplete")
    output = _git(
        "diff", "--name-status", "--no-renames", base_sha, publish_commit, "--", ".github/workflows"
    )
    for line in output.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        status = status[0]
        if path != TRUSTED_CONTROL_WORKFLOW:
            raise GatewayRequestError(f"untrusted workflow change in publish target: {path}")
        if status not in {"A", "M"}:
            raise GatewayRequestError(f"trusted control workflow must exist in publish target: {path}")
        target_oid = _git("rev-parse", f"{publish_commit}:{path}")
        control_oid = _git("rev-parse", f"{control_commit}:{path}")
        if target_oid != control_oid:
            raise GatewayRequestError(
                f"trusted control workflow blob mismatch: target {target_oid}, control {control_oid}"
            )
    return 0


def main() -> int:
    target_branch = os.environ.get("TARGET_BRANCH", "")
    if target_branch not in ALLOWED_TARGET_BRANCHES:
        raise GatewayRequestError("unsupported target branch")
    transport_dir_value = os.environ.get("TRANSPORT_DIR", "")
    expected_dir = f".publish/transport/{target_branch}"
    if transport_dir_value != expected_dir:
        raise GatewayRequestError(
            f"transport directory does not match target branch: expected {expected_dir}, got {transport_dir_value}"
        )
    payload = _decode_transport(Path(transport_dir_value))
    bundle_path = Path(os.environ["RUNNER_TEMP"]) / "publish.bundle"
    bundle_path.write_bytes(payload)
    publish_commit, base_sha, target_tree = _verify_bundle(bundle_path, target_branch)
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as env:
        env.write(f"PUBLISH_COMMIT={publish_commit}\n")
        env.write(f"BASE_SHA={base_sha}\n")
        env.write(f"TARGET_TREE={target_tree}\n")
        env.write(f"BUNDLE_FILE={bundle_path}\n")
    print(f"Validated fixed transport slot with {len(_transport_parts(Path(transport_dir_value)))} part(s)")
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-trusted-workflow", action="store_true")
    args = parser.parse_args()
    if args.verify_trusted_workflow:
        return verify_trusted_workflow()
    return main()


if __name__ == "__main__":
    try:
        raise SystemExit(cli())
    except GatewayRequestError as exc:
        raise SystemExit(str(exc)) from exc
