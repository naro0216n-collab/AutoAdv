#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

GITHUB_REPOSITORY = "naro0216n-collab/AutoAdv"
TARGET_BRANCH = "publish"
TRANSACTION_DIR_NAME = "autoadv-publish-control-maintenance-transaction"
MANIFEST_NAME = "manifest.json"
CONNECTOR_DIR_NAME = "connector"
STATE_NAME = "state.json"
SUMMARY_NAME = "summary.json"
PUBLISH_STATE_NAME = "autoadv-publish-state.json"
MANIFEST_VERSION = 2
CONNECTOR_CALL_HARD_CEILING_BYTES = 1024 * 1024
CONTROL_PATHS = (
    ".github/workflows/publish-gateway.yml",
    "scripts/publish_gateway_validate.py",
)

class ControlMaintenanceError(RuntimeError):
    pass


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _repo() -> Path:
    cwd = Path.cwd().resolve()
    return Path(_git("rev-parse", "--show-toplevel", cwd=cwd)).resolve()


def _git_dir(repo: Path) -> Path:
    raw = _git("rev-parse", "--git-dir", cwd=repo)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _transaction_dir(repo: Path) -> Path:
    return _git_dir(repo) / TRANSACTION_DIR_NAME


def _manifest_path(repo: Path) -> Path:
    return _transaction_dir(repo) / MANIFEST_NAME


def _connector_dir(repo: Path) -> Path:
    return _transaction_dir(repo) / CONNECTOR_DIR_NAME


def _state_path(repo: Path) -> Path:
    return _connector_dir(repo) / STATE_NAME


def _publish_state_path(repo: Path) -> Path:
    return _git_dir(repo) / PUBLISH_STATE_NAME


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        raise ControlMaintenanceError(f"required control maintenance state is missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ControlMaintenanceError(f"invalid control maintenance state: {path}")
    return value


def _require_sha(value: str, *, name: str) -> str:
    if len(value) not in {40, 64}:
        raise ControlMaintenanceError(f"invalid {name}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ControlMaintenanceError(f"invalid {name}") from exc
    return value


def _packet_bytes(packet: dict[str, object]) -> int:
    return len(json.dumps(packet["action_args"], separators=(",", ":")).encode("utf-8"))


def _write_packet(path: Path, packet: dict[str, object]) -> int:
    size = _packet_bytes(packet)
    if size >= CONNECTOR_CALL_HARD_CEILING_BYTES:
        raise ControlMaintenanceError(
            f"control connector call exceeds 1 MiB hard ceiling: {path.name} ({size} bytes)"
        )
    _write_json(path, packet)
    return size


def _require_clean(repo: Path) -> None:
    if _git("status", "--porcelain", cwd=repo):
        raise ControlMaintenanceError("publish control maintenance requires a clean worktree")


def _head_entry(repo: Path, path: str) -> tuple[str, str, str]:
    output = _git("ls-tree", "HEAD", "--", path, cwd=repo)
    if not output:
        raise ControlMaintenanceError(f"publish control file is not committed at HEAD: {path}")
    metadata, actual_path = output.split("\t", 1)
    if actual_path != path:
        raise ControlMaintenanceError(f"unexpected Git path while reading {path}")
    mode, object_type, oid = metadata.split()
    if object_type != "blob":
        raise ControlMaintenanceError(f"publish control path is not a blob: {path}")
    content = subprocess.check_output(["git", "cat-file", "blob", oid], cwd=repo)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ControlMaintenanceError(f"publish control file must be UTF-8 text: {path}") from exc
    return mode, oid, text


def _load_manifest(repo: Path) -> dict[str, object]:
    manifest = _read_json(_manifest_path(repo))
    if manifest.get("version") != MANIFEST_VERSION or manifest.get("kind") != "publish-control-maintenance":
        raise ControlMaintenanceError("unsupported publish control maintenance manifest")
    return manifest


def _load_state(repo: Path) -> dict[str, object]:
    return _read_json(_state_path(repo))


def _load_publish_state(repo: Path) -> dict[str, object]:
    state = _read_json(_publish_state_path(repo))
    for key in ("publish_commit", "publish_tree"):
        value = state.get(key)
        if not isinstance(value, str):
            raise ControlMaintenanceError(
                "normal publish state has no source-snapshot publish base; rerun publish_request.py init"
            )
        _require_sha(value, name=f"normal publish state {key}")
    return state


def _summary(repo: Path, state: dict[str, object], **extra: object) -> dict[str, object]:
    result = {
        "strategy": "publish-control-known-base-git-data",
        "stage": state["stage"],
        "target_branch": TARGET_BRANCH,
        "base_commit": state.get("base_commit"),
        "base_tree": state.get("base_tree"),
        **extra,
    }
    _write_json(_connector_dir(repo) / SUMMARY_NAME, result)
    return result


def _control_tree_packet(base_tree: str, elements: list[dict[str, object]], batch_index: int, expected_tree: str | None = None) -> dict[str, object]:
    packet: dict[str, object] = {
        "stage": "assemble-publish-control-tree",
        "tree_batch_index": batch_index,
        "action": "GitHub.create_tree",
        "action_args": {
            "repository_full_name": GITHUB_REPOSITORY,
            "base_tree_sha": base_tree,
            "tree_elements": elements,
        },
    }
    if expected_tree is not None:
        packet["expected_tree"] = expected_tree
    return packet


def _write_blob(repo: Path, content: str) -> str:
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content.encode("utf-8"),
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return _require_sha(result.stdout.decode("ascii").strip(), name="local control blob OID")


def _pack_control_elements(base_tree: str, files: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    elements = [
        {
            "path": str(file["path"]),
            "mode": str(file["mode"]),
            "type": "blob",
            "content": str(file["content"]),
        }
        for file in files
    ]
    batches: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for element in elements:
        candidate = [*current, element]
        packet = _control_tree_packet(base_tree, candidate, len(batches))
        if _packet_bytes(packet) < CONNECTOR_CALL_HARD_CEILING_BYTES:
            current = candidate
            continue
        if not current:
            raise ControlMaintenanceError(f"single control file exceeds connector ceiling: {element['path']}")
        batches.append(current)
        current = [element]
        if _packet_bytes(_control_tree_packet(base_tree, current, len(batches))) >= CONNECTOR_CALL_HARD_CEILING_BYTES:
            raise ControlMaintenanceError(f"single control file exceeds connector ceiling: {element['path']}")
    if current:
        batches.append(current)
    return batches


def _expected_control_batches(
    repo: Path, base_tree: str, batches: list[list[dict[str, object]]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="autoadv-control-index-") as tmp:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
        _git("read-tree", base_tree, cwd=repo, env=env)
        current_tree = base_tree
        for index, elements in enumerate(batches):
            for element in elements:
                oid = _write_blob(repo, str(element["content"]))
                _git(
                    "update-index", "--add", "--cacheinfo", str(element["mode"]), oid, str(element["path"]),
                    cwd=repo, env=env,
                )
            expected_tree = _git("write-tree", cwd=repo, env=env)
            result.append({
                "batch_index": index,
                "base_tree": current_tree,
                "expected_tree": expected_tree,
                "elements": elements,
            })
            current_tree = expected_tree
    return result


def _manifest_sha256(repo: Path) -> str:
    import hashlib
    return hashlib.sha256(_manifest_path(repo).read_bytes()).hexdigest()


def cmd_prepare(_: argparse.Namespace) -> int:
    repo = _repo()
    _require_clean(repo)
    transaction = _transaction_dir(repo)
    if transaction.exists() and any(transaction.iterdir()):
        raise ControlMaintenanceError("an active publish control maintenance transaction already exists; continue it")
    files: list[dict[str, object]] = []
    for path in CONTROL_PATHS:
        mode, _oid, text = _head_entry(repo, path)
        files.append({"path": path, "mode": mode, "content": text})
    manifest = {
        "version": MANIFEST_VERSION,
        "kind": "publish-control-maintenance",
        "target_branch": TARGET_BRANCH,
        "local_head": _git("rev-parse", "HEAD^{commit}", cwd=repo),
        "message": "Update publish gateway control plane",
        "files": files,
    }
    transaction.mkdir(parents=True, exist_ok=False)
    _write_json(_manifest_path(repo), manifest)
    print(json.dumps({
        "prepared": True,
        "target_branch": TARGET_BRANCH,
        "local_head": manifest["local_head"],
        "control_paths": list(CONTROL_PATHS),
        "next": "observe publish HEAD once, then run connector-plan",
    }, indent=2))
    return 0


def cmd_connector_plan(args: argparse.Namespace) -> int:
    repo = _repo()
    manifest = _load_manifest(repo)
    publish_state = _load_publish_state(repo)
    base_commit = _require_sha(args.publish_head, name="observed publish HEAD")
    if base_commit != publish_state["publish_commit"]:
        raise ControlMaintenanceError(
            f"publish HEAD moved from recorded source state: expected {publish_state['publish_commit']}, got {base_commit}"
        )
    base_tree = str(publish_state["publish_tree"])
    try:
        _git("cat-file", "-e", f"{base_tree}^{{tree}}", cwd=repo)
    except subprocess.CalledProcessError as exc:
        raise ControlMaintenanceError("recorded publish base tree is unavailable locally") from exc
    connector = _connector_dir(repo)
    if _state_path(repo).exists():
        raise ControlMaintenanceError("publish control connector plan already exists; continue it")
    connector.mkdir(parents=True, exist_ok=True)

    files = list(manifest["files"])
    raw_batches = _pack_control_elements(base_tree, files)
    batches = _expected_control_batches(repo, base_tree, raw_batches)
    expected_tree = str(batches[-1]["expected_tree"])

    tree_packets: list[str] = []
    tree_call_bytes: list[int] = []
    for batch in batches:
        packet = _control_tree_packet(
            str(batch["base_tree"]), list(batch["elements"]), int(batch["batch_index"]), expected_tree=str(batch["expected_tree"])
        )
        path = connector / f"control-tree-{int(batch['batch_index']):03d}.json"
        tree_call_bytes.append(_write_packet(path, packet))
        tree_packets.append(str(path))

    commit_packet = {
        "action": "GitHub.create_commit",
        "action_args": {
            "repository_full_name": GITHUB_REPOSITORY,
            "message": manifest["message"],
            "tree_sha": expected_tree,
            "parent_sha": base_commit,
        },
    }
    commit_path = connector / "create-control-commit.json"
    _write_packet(commit_path, commit_packet)

    state = {
        "version": 3,
        "stage": "execution-plan-ready",
        "base_commit": base_commit,
        "base_tree": base_tree,
        "expected_tree": expected_tree,
        "manifest_sha256": _manifest_sha256(repo),
        "tree_packets": tree_packets,
        "commit_packet": str(commit_path),
    }
    _write_json(_state_path(repo), state)
    result = _summary(
        repo, state,
        strategy="publish-control-tree-content-batches",
        tree_packets=tree_packets,
        tree_call_bytes=tree_call_bytes,
        expected_tree=expected_tree,
        commit_packet=str(commit_path),
        normal_pre_ref_helper_round_trips=0,
        post_commit_ref_update={
            "action": "GitHub.update_ref",
            "repository_full_name": GITHUB_REPOSITORY,
            "branch_name": TARGET_BRANCH,
            "sha": "<GitHub.create_commit returned SHA>",
            "force": False,
        },
        next=(
            "execute the generated create_tree packets in order and compare each returned SHA with packet expected_tree; "
            "then execute create_commit and use its returned SHA directly in one non-force publish ref update. "
            "No helper call is required between writes. After update_ref succeeds, run record-update with that commit SHA."
        ),
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_record_update(args: argparse.Namespace) -> int:
    repo = _repo()
    state = _load_state(repo)
    if state.get("stage") != "execution-plan-ready":
        raise ControlMaintenanceError(f"record-update requires execution-plan-ready, found {state.get('stage')}")
    if args.result != "success":
        raise ControlMaintenanceError("publish control ref update did not succeed; keep the active transaction")
    commit_sha = _require_sha(args.commit_sha, name="updated publish commit SHA")
    if _manifest_sha256(repo) != state.get("manifest_sha256"):
        raise ControlMaintenanceError("publish control manifest changed after connector-plan")
    publish_state = _load_publish_state(repo)
    if publish_state["publish_commit"] != state["base_commit"] or publish_state["publish_tree"] != state["base_tree"]:
        raise ControlMaintenanceError("normal publish base changed during control maintenance")
    publish_state["publish_commit"] = commit_sha
    publish_state["publish_tree"] = state["expected_tree"]
    _write_json(_publish_state_path(repo), publish_state)
    result = {
        "verified": True,
        "target_branch": TARGET_BRANCH,
        "published_commit": commit_sha,
        "published_tree": state["expected_tree"],
        "normal_publish_base_updated": True,
        "record_verification": "manifest-identity-and-successful-ref-update",
    }
    shutil.rmtree(_transaction_dir(repo))
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain the fixed Publish Gateway control plane on publish.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare").set_defaults(func=cmd_prepare)
    plan = sub.add_parser("connector-plan")
    plan.add_argument("--publish-head", required=True)
    plan.set_defaults(func=cmd_connector_plan)
    record = sub.add_parser("record-update")
    record.add_argument("--commit-sha", required=True)
    record.add_argument("--result", required=True, choices=("success", "failure"))
    record.set_defaults(func=cmd_record_update)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (ControlMaintenanceError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
