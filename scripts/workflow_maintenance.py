#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import json
import subprocess
from pathlib import Path

PUBLISH_STATE_NAME = "autoadv-publish-state.json"
REHYDRATE_MARKER_NAME = "autoadv-workflow-maintenance-rehydrate-required"
MANIFEST_VERSION = 2
CONNECTOR_CALL_BUDGET_BYTES = 1024 * 1024
CONNECTOR_STATE_NAME = "workflow-maintenance-state.json"
SUMMARY_NAME = "summary.json"
TRANSACTION_DIR_NAME = "autoadv-workflow-maintenance-transaction"
MANIFEST_NAME = "manifest.json"
CONNECTOR_DIR_NAME = "connector"
TARGET_BRANCH = "develop"
WORKFLOW_PREFIX = ".github/workflows/"
GITHUB_REPOSITORY = "naro0216n-collab/AutoAdv"


class WorkflowMaintenanceError(RuntimeError):
    pass


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _git_bytes(*args: str, cwd: Path) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _git_dir(repo: Path) -> Path:
    raw = _git("rev-parse", "--git-dir", cwd=repo)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _publish_state_path(repo: Path) -> Path:
    return _git_dir(repo) / PUBLISH_STATE_NAME


def _rehydrate_marker_path(repo: Path) -> Path:
    return _git_dir(repo) / REHYDRATE_MARKER_NAME


def _repo_from_cwd() -> Path:
    cwd = Path.cwd().resolve()
    return Path(_git("rev-parse", "--show-toplevel", cwd=cwd)).resolve()


def _transaction_dir(repo: Path) -> Path:
    return _git_dir(repo) / TRANSACTION_DIR_NAME


def _manifest_path(repo: Path) -> Path:
    return _transaction_dir(repo) / MANIFEST_NAME


def _connector_dir(repo: Path) -> Path:
    return _transaction_dir(repo) / CONNECTOR_DIR_NAME


def _require_no_active_transaction(repo: Path) -> None:
    transaction = _transaction_dir(repo)
    if transaction.exists() and any(transaction.iterdir()):
        raise WorkflowMaintenanceError(
            "an active workflow maintenance transaction already exists; finish it or rehydrate from source-snapshot"
        )


def _read_publish_state(repo: Path) -> dict[str, str]:
    path = _publish_state_path(repo)
    if not path.exists():
        raise WorkflowMaintenanceError(
            "normal publish state is not initialized; restore the latest develop source-snapshot first"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("remote_commit", "remote_tree", "local_head"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise WorkflowMaintenanceError(f"invalid publish state: missing {key}")
    return data


def _require_hex_sha(value: str, *, name: str) -> None:
    if len(value) not in {40, 64}:
        raise WorkflowMaintenanceError(f"invalid {name}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise WorkflowMaintenanceError(f"invalid {name}") from exc


def _changed_paths(repo: Path, base_commit: str, target_commit: str) -> list[tuple[str, str]]:
    output = _git(
        "diff",
        "--name-status",
        "--no-renames",
        base_commit,
        target_commit,
        "--",
        cwd=repo,
    )
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        status = status[0]
        if status not in {"A", "M", "D"}:
            raise WorkflowMaintenanceError(f"unsupported workflow change status {status}: {path}")
        changes.append((status, path))
    return changes


def _tree_entry(repo: Path, commit: str, path: str) -> tuple[str, str] | None:
    output = _git("ls-tree", commit, "--", path, cwd=repo)
    if not output:
        return None
    metadata, actual_path = output.split("\t", 1)
    if actual_path != path:
        raise WorkflowMaintenanceError(f"unexpected Git path while reading {path}")
    mode, object_type, oid = metadata.split()
    if object_type != "blob":
        raise WorkflowMaintenanceError(f"workflow path is not a blob: {path}")
    return mode, oid


def _connector_call_bytes(packet: dict[str, object]) -> int:
    return len(json.dumps(packet["action_args"], separators=(",", ":")).encode("utf-8"))


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_manifest(path: Path) -> dict[str, object]:
    if not path.exists():
        raise WorkflowMaintenanceError(f"maintenance manifest not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != MANIFEST_VERSION or data.get("kind") != "workflow-maintenance":
        raise WorkflowMaintenanceError("unsupported workflow maintenance manifest")
    if data.get("target_branch") != TARGET_BRANCH:
        raise WorkflowMaintenanceError("workflow maintenance target must be develop")
    return data


def _state_path(plan_dir: Path) -> Path:
    return plan_dir / CONNECTOR_STATE_NAME


def _read_connector_state(plan_dir: Path) -> dict[str, object]:
    path = _state_path(plan_dir)
    if not path.exists():
        raise WorkflowMaintenanceError(f"workflow maintenance connector state not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_connector_state(plan_dir: Path, state: dict[str, object]) -> None:
    _write_json(_state_path(plan_dir), state)


def _write_summary(plan_dir: Path, state: dict[str, object], **extra: object) -> dict[str, object]:
    summary = {
        "strategy": "workflow-maintenance-staged-git-data",
        "stage": state["stage"],
        "target_branch": state["target_branch"],
        "base_commit": state["base_commit"],
        "base_tree": state["base_tree"],
        "target_tree": state["target_tree"],
        "connector_profile": "github-connector-fixed",
        "connector_call_budget_bytes": CONNECTOR_CALL_BUDGET_BYTES,
        **extra,
    }
    _write_json(plan_dir / SUMMARY_NAME, summary)
    return summary


def cmd_prepare(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    state = _read_publish_state(repo)
    if _rehydrate_marker_path(repo).exists():
        raise WorkflowMaintenanceError(
            "a prior workflow maintenance update requires source-snapshot rehydration before another change"
        )
    _require_no_active_transaction(repo)
    target_commit = _git("rev-parse", "HEAD^{commit}", cwd=repo)
    target_tree = _git("rev-parse", "HEAD^{tree}", cwd=repo)
    if target_tree == state["remote_tree"]:
        raise WorkflowMaintenanceError("local target tree already matches the recorded develop tree")

    changes = _changed_paths(repo, state["remote_commit"], target_commit)
    if not changes:
        raise WorkflowMaintenanceError("workflow maintenance has no committed changes")
    non_workflow = [path for _, path in changes if not path.startswith(WORKFLOW_PREFIX)]
    if non_workflow:
        raise WorkflowMaintenanceError(
            "workflow maintenance accepts workflow-only commits; publish non-workflow changes first: "
            + ", ".join(non_workflow)
        )

    manifest_changes: list[dict[str, object]] = []
    for status, path in changes:
        if status == "D":
            base_entry = _tree_entry(repo, state["remote_commit"], path)
            if base_entry is None:
                raise WorkflowMaintenanceError(f"deleted workflow is absent from recorded base: {path}")
            mode, _ = base_entry
            manifest_changes.append({"status": status, "path": path, "mode": mode})
            continue
        entry = _tree_entry(repo, target_commit, path)
        if entry is None:
            raise WorkflowMaintenanceError(f"workflow target blob is missing: {path}")
        mode, oid = entry
        content = _git_bytes("cat-file", "blob", oid, cwd=repo)
        try:
            content_text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowMaintenanceError(f"workflow file must be UTF-8 text: {path}") from exc
        manifest_changes.append(
            {
                "status": status,
                "path": path,
                "mode": mode,
                "blob_oid": oid,
                "content": content_text,
            }
        )

    message = _git("show", "-s", "--format=%B", target_commit, cwd=repo).rstrip()
    if not message:
        raise WorkflowMaintenanceError("workflow maintenance commit message must not be empty")
    manifest = {
        "version": MANIFEST_VERSION,
        "kind": "workflow-maintenance",
        "target_branch": TARGET_BRANCH,
        "base_commit": state["remote_commit"],
        "base_tree": state["remote_tree"],
        "target_tree": target_tree,
        "local_target_commit": target_commit,
        "message": message,
        "changes": manifest_changes,
    }
    output = _manifest_path(repo)
    output.parent.mkdir(parents=True, exist_ok=False)
    _write_json(output, manifest)
    print(
        json.dumps(
            {
                "prepared": True,
                "kind": "workflow-maintenance",
                "target_branch": TARGET_BRANCH,
                "base_commit": state["remote_commit"],
                "target_tree": target_tree,
                "local_target_commit": target_commit,
                "workflow_paths": [item["path"] for item in manifest_changes],
                "next": "fetch develop HEAD once, then run connector-plan",
            },
            indent=2,
        )
    )
    return 0


def _workflow_tree_packet(
    base_tree: str, elements: list[dict[str, object]], batch_index: int, expected_tree: str | None = None,
) -> dict[str, object]:
    packet: dict[str, object] = {
        "stage": "assemble-workflow-maintenance-tree",
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


def _workflow_elements(manifest: dict[str, object]) -> list[dict[str, object]]:
    elements: list[dict[str, object]] = []
    for change in manifest["changes"]:
        assert isinstance(change, dict)
        if change["status"] == "D":
            elements.append({
                "path": change["path"], "mode": change["mode"], "type": "blob", "sha": None,
            })
        else:
            elements.append({
                "path": change["path"], "mode": change["mode"], "type": "blob", "content": change["content"],
            })
    return elements


def _pack_workflow_elements(base_tree: str, elements: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    batches: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for element in elements:
        candidate = [*current, element]
        if _connector_call_bytes(_workflow_tree_packet(base_tree, candidate, len(batches))) < CONNECTOR_CALL_BUDGET_BYTES:
            current = candidate
            continue
        if not current:
            raise WorkflowMaintenanceError(f"single workflow tree element exceeds connector budget: {element['path']}")
        batches.append(current)
        current = [element]
        if _connector_call_bytes(_workflow_tree_packet(base_tree, current, len(batches))) >= CONNECTOR_CALL_BUDGET_BYTES:
            raise WorkflowMaintenanceError(f"single workflow tree element exceeds connector budget: {element['path']}")
    if current:
        batches.append(current)
    return batches


def _expected_workflow_batches(
    repo: Path, base_tree: str, batches: list[list[dict[str, object]]], manifest: dict[str, object],
) -> list[dict[str, object]]:
    oid_by_path = {
        str(change["path"]): str(change["blob_oid"])
        for change in manifest["changes"]
        if change["status"] != "D"
    }
    result: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="autoadv-workflow-index-") as tmp:
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
        subprocess.run(
            ["git", "read-tree", base_tree], cwd=repo, env=env, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        current_tree = base_tree
        for index, elements in enumerate(batches):
            for element in elements:
                path = str(element["path"])
                if element.get("sha") is None and "content" not in element:
                    subprocess.run(
                        ["git", "update-index", "--force-remove", "--", path], cwd=repo, env=env, check=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                else:
                    subprocess.run(
                        [
                            "git", "update-index", "--add", "--cacheinfo",
                            str(element["mode"]), oid_by_path[path], path,
                        ],
                        cwd=repo, env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
            expected_tree = subprocess.check_output(
                ["git", "write-tree"], cwd=repo, env=env, text=True
            ).strip()
            result.append({
                "batch_index": index,
                "base_tree": current_tree,
                "expected_tree": expected_tree,
                "elements": elements,
            })
            current_tree = expected_tree
    return result


def _manifest_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cmd_connector_plan(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    manifest_path = _manifest_path(repo)
    manifest = _load_manifest(manifest_path)
    _require_hex_sha(args.develop_head, name="observed develop HEAD")
    if args.develop_head != manifest["base_commit"]:
        raise WorkflowMaintenanceError(
            "develop HEAD moved since workflow maintenance prepare; do not create connector packets"
        )
    plan_dir = _connector_dir(repo)
    if _state_path(plan_dir).exists():
        raise WorkflowMaintenanceError(
            "workflow maintenance connector plan already exists; continue its recorded stage instead of replanning"
        )
    plan_dir.mkdir(parents=True, exist_ok=True)

    elements = _workflow_elements(manifest)
    raw_batches = _pack_workflow_elements(str(manifest["base_tree"]), elements)
    batches = _expected_workflow_batches(repo, str(manifest["base_tree"]), raw_batches, manifest)
    final_tree = str(batches[-1]["expected_tree"])
    if final_tree != manifest["target_tree"]:
        raise WorkflowMaintenanceError(
            f"precomputed workflow tree mismatch: expected local target {manifest['target_tree']}, got {final_tree}"
        )

    tree_packets: list[str] = []
    tree_call_bytes: list[int] = []
    for batch in batches:
        packet = _workflow_tree_packet(
            str(batch["base_tree"]), list(batch["elements"]), int(batch["batch_index"]), str(batch["expected_tree"])
        )
        size = _connector_call_bytes(packet)
        if size >= CONNECTOR_CALL_BUDGET_BYTES:
            raise WorkflowMaintenanceError("workflow tree packet exceeds the fixed connector call budget")
        path = plan_dir / f"workflow-tree-{int(batch['batch_index']):03d}.json"
        _write_json(path, packet)
        tree_packets.append(str(path))
        tree_call_bytes.append(size)

    commit_packet = {
        "action": "GitHub.create_commit",
        "action_args": {
            "repository_full_name": GITHUB_REPOSITORY,
            "message": manifest["message"],
            "tree_sha": manifest["target_tree"],
            "parent_sha": manifest["base_commit"],
        },
    }
    commit_path = plan_dir / "create-workflow-commit.json"
    _write_json(commit_path, commit_packet)

    state = {
        "version": 2,
        "stage": "execution-plan-ready",
        "manifest": str(manifest_path),
        "manifest_sha256": _manifest_digest(manifest_path),
        "github_repository": GITHUB_REPOSITORY,
        "target_branch": TARGET_BRANCH,
        "base_commit": manifest["base_commit"],
        "base_tree": manifest["base_tree"],
        "target_tree": manifest["target_tree"],
        "local_target_commit": manifest["local_target_commit"],
        "tree_packets": tree_packets,
        "commit_packet": str(commit_path),
    }
    _write_connector_state(plan_dir, state)
    summary = _write_summary(
        plan_dir,
        state,
        strategy="workflow-maintenance-tree-content-batches",
        tree_packets=tree_packets,
        tree_call_bytes=tree_call_bytes,
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
            "execute the generated create_tree packets in order and require each returned SHA to equal packet expected_tree; "
            "execute create_commit, then use its returned SHA directly in one non-force develop ref update. "
            "After update_ref succeeds, run record-update with the same commit SHA; no commit refetch or remote ref/tree read is required."
        ),
    )
    print(json.dumps(summary, indent=2))
    return 0


def cmd_record_update(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    plan_dir = _connector_dir(repo)
    state = _read_connector_state(plan_dir)
    if state.get("stage") != "execution-plan-ready":
        raise WorkflowMaintenanceError(
            f"record-update requires execution-plan-ready, found {state.get('stage')}"
        )
    if args.result != "success":
        raise WorkflowMaintenanceError("workflow develop ref update did not succeed; keep the active transaction")
    _require_hex_sha(args.commit_sha, name="updated workflow commit SHA")
    manifest_path = Path(str(state["manifest"]))
    if _manifest_digest(manifest_path) != state.get("manifest_sha256"):
        raise WorkflowMaintenanceError("workflow maintenance manifest changed after connector-plan")
    _rehydrate_marker_path(repo).write_text(
        json.dumps(
            {
                "published_commit": args.commit_sha,
                "published_tree": state["target_tree"],
                "local_target_commit": state["local_target_commit"],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = _write_summary(
        plan_dir,
        state,
        published_commit=args.commit_sha,
        published_tree=state["target_tree"],
        verified=True,
        record_verification="manifest-identity-and-successful-ref-update",
        rehydrate_required=True,
        next="restore the new develop source-snapshot and run publish_request.py init before normal development/publish",
    )
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Publish the single active committed .github/workflows-only develop maintenance transaction. "
            "Normal source/game changes belong to publish_request.py."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="prepare HEAD as the workflow-only develop maintenance target")
    prepare.set_defaults(func=cmd_prepare)

    connector_plan = sub.add_parser(
        "connector-plan", help="after one develop HEAD check, generate the complete workflow Git-data execution plan"
    )
    connector_plan.add_argument(
        "--develop-head",
        required=True,
        help="observed develop HEAD from the single pre-maintenance remote check; verification input, not a target selector",
    )
    connector_plan.set_defaults(func=cmd_connector_plan)

    record = sub.add_parser(
        "record-update", help="record one successful non-force develop ref update and require source-snapshot rehydration"
    )
    record.add_argument("--commit-sha", required=True)
    record.add_argument("--result", required=True, choices=("success", "failure"))
    record.set_defaults(func=cmd_record_update)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (WorkflowMaintenanceError, subprocess.CalledProcessError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
