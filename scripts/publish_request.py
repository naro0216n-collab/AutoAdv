#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

STATE_NAME = "autoadv-publish-state.json"
WORKFLOW_REHYDRATE_MARKER_NAME = "autoadv-workflow-maintenance-rehydrate-required"
MANIFEST_VERSION = 8
CONNECTOR_CALL_BUDGET_BYTES = 1024 * 1024
MAX_TREE_CALLS_PER_EXECUTION = 10
MAX_PAYLOAD_PARTS = 256
CONNECTOR_STATE_NAME = "connector-state.json"
CONNECTOR_STATE_VERSION = 11
CONNECTOR_SUMMARY_NAME = "summary.json"
TRANSACTION_DIR_NAME = "autoadv-publish-transaction"
WORKFLOW_TRANSACTION_DIR_NAME = "autoadv-workflow-maintenance-transaction"
MANIFEST_NAME = "manifest.json"
CONNECTOR_DIR_NAME = "connector"
PUBLISH_BUNDLE_REF = "refs/autoadv/publish-request"
PUBLISH_IDENTITY_NAME = "autoadv-publish-gateway"
PUBLISH_IDENTITY_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
GITHUB_REPOSITORY = "naro0216n-collab/AutoAdv"
PUBLISH_BRANCH = "publish"
TARGET_BRANCH = "develop"
TRUSTED_CONTROL_WORKFLOW = ".github/workflows/publish-gateway.yml"


class PublishStateError(RuntimeError):
    pass


def _git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _git_bytes(*args: str, cwd: Path) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout


def _git_dir(repo: Path) -> Path:
    raw = _git("rev-parse", "--git-dir", cwd=repo)
    path = Path(raw)
    return path if path.is_absolute() else repo / path


def _repo_from_cwd() -> Path:
    cwd = Path.cwd().resolve()
    return Path(_git("rev-parse", "--show-toplevel", cwd=cwd)).resolve()


def _state_path(repo: Path) -> Path:
    return _git_dir(repo) / STATE_NAME


def _transaction_dir(repo: Path) -> Path:
    return _git_dir(repo) / TRANSACTION_DIR_NAME


def _manifest_path(repo: Path) -> Path:
    return _transaction_dir(repo) / MANIFEST_NAME


def _connector_dir(repo: Path) -> Path:
    return _transaction_dir(repo) / CONNECTOR_DIR_NAME


def _working_tree_clean(repo: Path) -> bool:
    return not bool(_git("status", "--porcelain", cwd=repo))


def _require_hex_sha(value: str, *, name: str) -> str:
    if len(value) not in {40, 64}:
        raise PublishStateError(f"invalid {name}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PublishStateError(f"invalid {name}") from exc
    return value


def _require_object(repo: Path, spec: str, *, name: str) -> None:
    result = subprocess.run(
        ["git", "cat-file", "-e", spec], cwd=repo,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PublishStateError(f"{name} is not available locally: {spec}")


def _read_state(repo: Path) -> dict[str, str]:
    marker = _git_dir(repo) / WORKFLOW_REHYDRATE_MARKER_NAME
    if marker.exists():
        raise PublishStateError(
            "workflow maintenance updated develop; restore the latest source-snapshot and run init before normal publish"
        )
    path = _state_path(repo)
    if not path.exists():
        raise PublishStateError("publish state is not initialized; initialize it from the verified source artifact")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ("remote_commit", "remote_tree", "local_head", "publish_commit", "publish_tree")
    for key in required:
        value = data.get(key)
        if not isinstance(value, str) or not value:
            raise PublishStateError(f"invalid publish state: missing {key}")
        _require_hex_sha(value, name=f"publish state {key}")
    return {key: str(data[key]) for key in required}


def _write_state(
    repo: Path, *, remote_commit: str, remote_tree: str, local_head: str,
    publish_commit: str, publish_tree: str,
) -> None:
    _state_path(repo).write_text(
        json.dumps(
            {
                "remote_commit": remote_commit,
                "remote_tree": remote_tree,
                "local_head": local_head,
                "publish_commit": publish_commit,
                "publish_tree": publish_tree,
            },
            indent=2, sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )


def _require_no_active_transaction(repo: Path) -> None:
    transaction = _transaction_dir(repo)
    if transaction.exists() and any(transaction.iterdir()):
        raise PublishStateError(
            "an active publish transaction already exists; continue or close it before preparing another"
        )


def _changed_workflow_paths(repo: Path, base_commit: str, target_commit: str) -> list[str]:
    output = _git(
        "diff", "--name-only", base_commit, target_commit, "--", ".github/workflows", cwd=repo
    )
    return [line for line in output.splitlines() if line.strip()]


def _message_bytes(message: str) -> bytes:
    if not message.strip():
        raise PublishStateError("commit message must not be empty")
    if not message.endswith("\n"):
        message += "\n"
    return message.encode("utf-8")


def _default_message(repo: Path, state: dict[str, str], target_commit: str) -> str:
    local_head = state["local_head"]
    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", local_head, target_commit], cwd=repo,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0
    if is_ancestor:
        commits = _git("rev-list", "--reverse", f"{local_head}..{target_commit}", cwd=repo).splitlines()
        if len(commits) == 1:
            return _git("show", "-s", "--format=%B", commits[0], cwd=repo).rstrip() + "\n"
    return _git("show", "-s", "--format=%B", target_commit, cwd=repo).rstrip() + "\n"


def _create_publish_commit(
    repo: Path, base_commit: str, target_tree: str, target_commit: str, message: bytes,
) -> str:
    _require_object(repo, f"{base_commit}^{{commit}}", name="recorded develop base commit")
    target_date = _git("show", "-s", "--format=%cI", target_commit, cwd=repo)
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": PUBLISH_IDENTITY_NAME,
            "GIT_AUTHOR_EMAIL": PUBLISH_IDENTITY_EMAIL,
            "GIT_AUTHOR_DATE": target_date,
            "GIT_COMMITTER_NAME": PUBLISH_IDENTITY_NAME,
            "GIT_COMMITTER_EMAIL": PUBLISH_IDENTITY_EMAIL,
            "GIT_COMMITTER_DATE": target_date,
        }
    )
    result = subprocess.run(
        ["git", "commit-tree", target_tree, "-p", base_commit], cwd=repo, env=env,
        input=message, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return _require_hex_sha(result.stdout.decode("ascii").strip(), name="publish commit")


def _bundle_bytes(repo: Path, base_commit: str, publish_commit: str) -> bytes:
    old_ref = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", PUBLISH_BUNDLE_REF], cwd=repo,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="autoadv-publish-bundle-") as tmp:
        bundle_path = Path(tmp) / "request.bundle"
        try:
            subprocess.run(
                ["git", "update-ref", PUBLISH_BUNDLE_REF, publish_commit], cwd=repo,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            subprocess.run(
                ["git", "bundle", "create", str(bundle_path), PUBLISH_BUNDLE_REF, f"^{base_commit}"],
                cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            return bundle_path.read_bytes()
        finally:
            if old_ref:
                subprocess.run(
                    ["git", "update-ref", PUBLISH_BUNDLE_REF, old_ref], cwd=repo,
                    check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            else:
                subprocess.run(
                    ["git", "update-ref", "-d", PUBLISH_BUNDLE_REF], cwd=repo,
                    check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )


def _parse_commit_object(repo: Path, commit: str) -> tuple[dict[str, list[str]], bytes]:
    raw = _git_bytes("cat-file", "commit", commit, cwd=repo)
    header, sep, body = raw.partition(b"\n\n")
    if not sep:
        raise PublishStateError(f"invalid commit object: {commit}")
    fields: dict[str, list[str]] = {}
    for line in header.decode("utf-8").splitlines():
        key, _, value = line.partition(" ")
        fields.setdefault(key, []).append(value)
    return fields, body


def _read_prepared_request(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishStateError(f"invalid prepared publish request: {exc}") from exc
    required = {
        "version", "request_id", "target_branch", "base_sha", "target_tree",
        "publish_commit", "payload_sha256", "payload_encoding", "payload_b64",
        "local_target_commit",
    }
    missing = required - data.keys()
    if missing:
        raise PublishStateError(f"invalid prepared publish request: missing fields {sorted(missing)}")
    if data["version"] != MANIFEST_VERSION:
        raise PublishStateError(f"unsupported prepared request version: {data['version']}")
    request_id = data["request_id"]
    if not isinstance(request_id, str) or len(request_id) != 32:
        raise PublishStateError("invalid prepared request id")
    try:
        int(request_id, 16)
    except ValueError as exc:
        raise PublishStateError("invalid prepared request id") from exc
    if data["target_branch"] != TARGET_BRANCH:
        raise PublishStateError("standard publish targets develop only")
    for key in ("base_sha", "target_tree", "publish_commit", "local_target_commit"):
        if not isinstance(data[key], str):
            raise PublishStateError(f"invalid prepared {key}")
        _require_hex_sha(str(data[key]), name=f"prepared {key}")
    digest = data["payload_sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise PublishStateError("invalid prepared payload sha256")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise PublishStateError("invalid prepared payload sha256") from exc
    if data["payload_encoding"] != "git-bundle-base64":
        raise PublishStateError("unsupported prepared payload encoding")
    if not isinstance(data["payload_b64"], str) or not data["payload_b64"]:
        raise PublishStateError("prepared request has no payload")
    return data


def _decode_prepared_payload(request: dict[str, object]) -> bytes:
    try:
        return base64.b64decode(str(request["payload_b64"]), validate=True)
    except Exception as exc:
        raise PublishStateError(f"invalid prepared bundle Base64: {exc}") from exc


def _verify_bundle_payload(repo: Path, request: dict[str, object], payload_bytes: bytes) -> dict[str, object]:
    actual_digest = hashlib.sha256(payload_bytes).hexdigest()
    if actual_digest != request["payload_sha256"]:
        raise PublishStateError(
            f"publish bundle sha256 mismatch: expected={request['payload_sha256']} actual={actual_digest}"
        )
    publish_commit = str(request["publish_commit"])
    with tempfile.TemporaryDirectory(prefix="autoadv-publish-bundle-verify-") as tmp:
        bundle_path = Path(tmp) / "request.bundle"
        bundle_path.write_bytes(payload_bytes)
        try:
            subprocess.run(
                ["git", "bundle", "verify", str(bundle_path)], cwd=repo,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            heads = _git("bundle", "list-heads", str(bundle_path), cwd=repo).splitlines()
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
            raise PublishStateError(f"invalid publish bundle: {(stderr or '').strip()}") from exc
    advertised = [line.split(maxsplit=1) for line in heads if line.strip()]
    if advertised != [[publish_commit, PUBLISH_BUNDLE_REF]]:
        raise PublishStateError(
            f"publish bundle advertises unexpected heads: expected={[[publish_commit, PUBLISH_BUNDLE_REF]]} actual={advertised}"
        )
    fields, _ = _parse_commit_object(repo, publish_commit)
    if fields.get("tree", []) != [request["target_tree"]]:
        raise PublishStateError("publish commit tree mismatch")
    if fields.get("parent", []) != [request["base_sha"]]:
        raise PublishStateError("publish commit parent mismatch")
    return {
        "payload_sha256": actual_digest,
        "payload_bytes": len(payload_bytes),
        "publish_commit": publish_commit,
        "target_tree": str(request["target_tree"]),
    }


def _verify_prepared_request(repo: Path, path: Path) -> dict[str, object]:
    state = _read_state(repo)
    request = _read_prepared_request(path)
    if request["base_sha"] != state["remote_commit"]:
        raise PublishStateError("publish request base does not match recorded develop commit")
    local_target = str(request["local_target_commit"])
    if _git("rev-parse", f"{local_target}^{{tree}}", cwd=repo) != request["target_tree"]:
        raise PublishStateError("prepared local target tree mismatch")
    payload = _decode_prepared_payload(request)
    bundle = _verify_bundle_payload(repo, request, payload)
    return {
        "manifest": str(path), "version": MANIFEST_VERSION, "transport": "git-bundle",
        "request_id": request["request_id"], "payload_bytes": bundle["payload_bytes"],
        "payload_chars": len(str(request["payload_b64"])), "payload_sha256": bundle["payload_sha256"],
        "publish_commit": bundle["publish_commit"], "target_tree": bundle["target_tree"], "verified": True,
    }


def _source_snapshot_from_origin(repo: Path) -> Path:
    try:
        origin = _git("remote", "get-url", "origin", cwd=repo)
    except subprocess.CalledProcessError as exc:
        raise PublishStateError("restored repository has no readable origin") from exc
    if origin.startswith("file://"):
        parsed = urlparse(origin)
        if parsed.netloc not in {"", "localhost"}:
            raise PublishStateError("origin must be the local source-snapshot repository.bundle")
        bundle_path = Path(unquote(parsed.path))
    else:
        if "://" in origin or (":" in origin and not Path(origin).is_absolute()):
            raise PublishStateError("origin must point to the local source-snapshot repository.bundle")
        bundle_path = Path(origin)
    bundle_path = (repo / bundle_path).resolve() if not bundle_path.is_absolute() else bundle_path.resolve()
    if bundle_path.name != "repository.bundle" or not bundle_path.is_file():
        raise PublishStateError("origin must point to an existing source-snapshot/repository.bundle")
    return bundle_path.parent


def _read_source_snapshot_metadata(source_snapshot: Path, *, repo: Path) -> dict[str, str]:
    names = (
        ".source-commit", ".source-tree", ".source-branch",
        ".source-publish-commit", ".source-publish-tree", "repository.bundle",
    )
    missing = [name for name in names if not (source_snapshot / name).is_file()]
    if missing:
        raise PublishStateError("source-snapshot directory is incomplete; missing: " + ", ".join(missing))
    values = {
        "remote_commit": (source_snapshot / ".source-commit").read_text(encoding="utf-8").strip(),
        "remote_tree": (source_snapshot / ".source-tree").read_text(encoding="utf-8").strip(),
        "branch": (source_snapshot / ".source-branch").read_text(encoding="utf-8").strip(),
        "publish_commit": (source_snapshot / ".source-publish-commit").read_text(encoding="utf-8").strip(),
        "publish_tree": (source_snapshot / ".source-publish-tree").read_text(encoding="utf-8").strip(),
    }
    for key in ("remote_commit", "remote_tree", "publish_commit", "publish_tree"):
        _require_hex_sha(values[key], name=f"source-snapshot {key}")
    if values["branch"] != TARGET_BRANCH:
        raise PublishStateError(f"source-snapshot branch must be {TARGET_BRANCH!r}, got {values['branch']!r}")
    bundle = source_snapshot / "repository.bundle"
    verify = subprocess.run(
        ["git", "bundle", "verify", str(bundle)], cwd=repo, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if verify.returncode != 0:
        raise PublishStateError("source-snapshot bundle verification failed: " + verify.stderr.strip())
    heads = subprocess.run(
        ["git", "bundle", "list-heads", str(bundle)], cwd=source_snapshot,
        check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.splitlines()
    expected = {
        f"{values['remote_commit']} refs/heads/{TARGET_BRANCH}",
        f"{values['publish_commit']} refs/autoadv/publish-base",
    }
    if not expected.issubset(set(heads)):
        raise PublishStateError("source-snapshot metadata does not match repository.bundle refs")
    return values


def cmd_init(_: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    source_snapshot = _source_snapshot_from_origin(repo)
    metadata = _read_source_snapshot_metadata(source_snapshot, repo=repo)
    local_branch = _git("branch", "--show-current", cwd=repo)
    local_commit = _git("rev-parse", "HEAD^{commit}", cwd=repo)
    local_tree = _git("rev-parse", "HEAD^{tree}", cwd=repo)
    if local_branch != TARGET_BRANCH:
        raise PublishStateError(f"restored repository branch must be {TARGET_BRANCH!r}, got {local_branch!r}")
    if local_commit != metadata["remote_commit"] or local_tree != metadata["remote_tree"]:
        raise PublishStateError("artifact/local develop commit or tree mismatch")
    _git("config", "--local", "user.name", PUBLISH_IDENTITY_NAME, cwd=repo)
    _git("config", "--local", "user.email", PUBLISH_IDENTITY_EMAIL, cwd=repo)
    subprocess.run(
        [
            "git", "fetch", "origin",
            "refs/autoadv/publish-base:refs/autoadv/publish-base",
        ],
        cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    publish_ref = _git("rev-parse", "refs/autoadv/publish-base^{commit}", cwd=repo)
    publish_tree = _git("rev-parse", "refs/autoadv/publish-base^{tree}", cwd=repo)
    if publish_ref != metadata["publish_commit"] or publish_tree != metadata["publish_tree"]:
        raise PublishStateError("artifact/local publish base commit or tree mismatch")
    _write_state(
        repo, remote_commit=local_commit, remote_tree=local_tree, local_head=local_commit,
        publish_commit=publish_ref, publish_tree=publish_tree,
    )
    for path in (
        _transaction_dir(repo), _git_dir(repo) / WORKFLOW_TRANSACTION_DIR_NAME,
    ):
        if path.exists():
            shutil.rmtree(path)
    marker = _git_dir(repo) / WORKFLOW_REHYDRATE_MARKER_NAME
    if marker.exists():
        marker.unlink()
    print(json.dumps({
        "remote_commit": local_commit, "remote_tree": local_tree, "local_head": local_commit,
        "publish_commit": publish_ref, "publish_tree": publish_tree,
    }, indent=2))
    return 0


def cmd_prepare(_: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    state = _read_state(repo)
    _require_no_active_transaction(repo)
    target_commit = _git("rev-parse", "HEAD^{commit}", cwd=repo)
    target_tree = _git("rev-parse", "HEAD^{tree}", cwd=repo)
    if target_tree == state["remote_tree"]:
        raise PublishStateError("local target tree already matches the last published tree")
    workflow_paths = _changed_workflow_paths(repo, state["remote_commit"], target_commit)
    disallowed = [path for path in workflow_paths if path != TRUSTED_CONTROL_WORKFLOW]
    if disallowed:
        raise PublishStateError(
            "standard Publish Gateway cannot publish untrusted .github/workflows changes; "
            "use `python scripts/workflow_maintenance.py prepare` for workflow-only maintenance: "
            + ", ".join(disallowed)
        )
    publish_commit = _create_publish_commit(
        repo, state["remote_commit"], target_tree, target_commit,
        _message_bytes(_default_message(repo, state, target_commit)),
    )
    payload_bytes = _bundle_bytes(repo, state["remote_commit"], publish_commit)
    payload_b64 = base64.b64encode(payload_bytes).decode("ascii")
    request = {
        "version": MANIFEST_VERSION,
        "request_id": uuid.uuid4().hex,
        "target_branch": TARGET_BRANCH,
        "base_sha": state["remote_commit"],
        "target_tree": target_tree,
        "publish_commit": publish_commit,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "payload_encoding": "git-bundle-base64",
        "payload_b64": payload_b64,
        "local_target_commit": target_commit,
    }
    output = _manifest_path(repo)
    output.parent.mkdir(parents=True, exist_ok=False)
    output.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verified = _verify_prepared_request(repo, output)
    print(json.dumps({
        "manifest": str(output), "request_id": request["request_id"],
        "transport_slot": f".publish/transport/{TARGET_BRANCH}",
        "payload_bytes": len(payload_bytes), "payload_chars": len(payload_b64),
        "payload_sha256": request["payload_sha256"], "publish_commit": publish_commit,
        "target_tree": target_tree, "local_target_commit": target_commit,
        "working_tree_clean": _working_tree_clean(repo),
        "uncommitted_changes_excluded": not _working_tree_clean(repo),
        "verified": verified["verified"],
    }, indent=2))
    return 0


def _connector_call_bytes(packet: dict[str, object]) -> int:
    args = packet.get("action_args")
    if not isinstance(args, dict):
        raise PublishStateError("Connector packet is missing action_args")
    return len(json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _tree_content_element(path: str, content: str) -> dict[str, object]:
    return {"path": path, "mode": "100644", "type": "blob", "content": content}


def _tree_delete_element(path: str) -> dict[str, object]:
    return {"path": path, "mode": "100644", "type": "blob", "sha": None}


def _tree_packet(base_tree: str, elements: list[dict[str, object]], batch_index: int, expected_tree: str | None = None) -> dict[str, object]:
    packet: dict[str, object] = {
        "stage": "assemble-publish-transport-tree",
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


def _list_publish_paths(repo: Path, tree: str) -> list[str]:
    _require_object(repo, f"{tree}^{{tree}}", name="publish transport base tree")
    output = _git("ls-tree", "-r", "--name-only", tree, "--", ".publish", cwd=repo)
    return [line for line in output.splitlines() if line]


def _write_blob(repo: Path, content: str) -> str:
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=repo, input=content.encode("utf-8"),
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return _require_hex_sha(result.stdout.decode("ascii").strip(), name="local transport blob OID")


def _transport_part_capacity(*, target_branch: str) -> int:
    """Use each tree call's verified byte budget, not an arbitrary fixed slice."""
    path = f".publish/transport/{target_branch}/0000.b64"
    empty = _tree_content_element(path, "")
    # The base/tree SHA and numeric batch index have fixed widths. The Base64
    # alphabet needs no JSON escaping, so each additional character adds one byte.
    overhead = _connector_call_bytes(_tree_packet("0" * 40, [empty], 0))
    capacity = CONNECTOR_CALL_BUDGET_BYTES - overhead
    if capacity <= 0:
        raise PublishStateError("Connector budget cannot hold one transport file")
    return capacity


def _payload_chunks(payload: str, *, target_branch: str) -> list[dict[str, object]]:
    if not payload:
        raise PublishStateError("publish payload is empty")
    slot = f".publish/transport/{target_branch}"
    capacity = _transport_part_capacity(target_branch=target_branch)
    chunks: list[dict[str, object]] = []
    for start in range(0, len(payload), capacity):
        index = len(chunks)
        chunks.append({
            "chunk_index": index,
            "path": f"{slot}/{index:04d}.b64",
            "content": payload[start:start + capacity],
        })
    if len(chunks) > MAX_PAYLOAD_PARTS:
        raise PublishStateError(
            f"publish payload needs {len(chunks)} transport files, exceeding limit {MAX_PAYLOAD_PARTS}"
        )
    return chunks


def _desired_transport_elements(
    repo: Path, prepared: dict[str, object], *, base_tree: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[str]]:
    target = str(prepared["target_branch"])
    chunks = _payload_chunks(str(prepared["payload_b64"]), target_branch=target)
    desired_paths = {str(chunk["path"]) for chunk in chunks}
    slot_prefix = f".publish/transport/{target}/"
    stale: list[str] = []
    for path in _list_publish_paths(repo, base_tree):
        # The transport tree contains only active fixed slots. An unexpected path
        # is a control-plane inconsistency, not data for the next publish to erase.
        if re.fullmatch(r"\.publish/transport/(develop|temp)/[0-9]{4}\.b64", path) is None:
            raise PublishStateError(f"unsupported publish transport path: {path}")
        if path.startswith(slot_prefix) and path not in desired_paths:
            stale.append(path)
    deletes = [_tree_delete_element(path) for path in sorted(stale)]
    elements = [
        *[_tree_content_element(str(chunk["path"]), str(chunk["content"])) for chunk in chunks],
        *deletes,
    ]
    return chunks, elements, stale


def _pack_tree_elements(base_tree: str, elements: list[dict[str, object]]) -> list[list[dict[str, object]]]:
    batches: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for element in elements:
        candidate = [*current, element]
        if _connector_call_bytes(_tree_packet(base_tree, candidate, len(batches))) <= CONNECTOR_CALL_BUDGET_BYTES:
            current = candidate
            continue
        if not current:
            raise PublishStateError(f"single tree element exceeds Connector ceiling: {element['path']}")
        batches.append(current)
        current = [element]
        if _connector_call_bytes(_tree_packet(base_tree, current, len(batches))) > CONNECTOR_CALL_BUDGET_BYTES:
            raise PublishStateError(f"single tree element exceeds Connector ceiling: {element['path']}")
    if current:
        batches.append(current)
    return batches


def _expected_tree_batches(
    repo: Path, *, base_tree: str, batches: list[list[dict[str, object]]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="autoadv-publish-index-") as tmp:
        index_path = Path(tmp) / "index"
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = str(index_path)
        _git("read-tree", base_tree, cwd=repo, env=env)
        current_tree = base_tree
        for batch_index, elements in enumerate(batches):
            for element in elements:
                path = str(element["path"])
                if "content" in element:
                    oid = _write_blob(repo, str(element["content"]))
                    _git(
                        "update-index", "--add", "--cacheinfo", str(element["mode"]), oid, path,
                        cwd=repo, env=env,
                    )
                elif element.get("sha") is None and element.get("type") == "blob":
                    subprocess.run(
                        ["git", "update-index", "--force-remove", "--", path],
                        cwd=repo, env=env, check=True,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                else:
                    raise PublishStateError(f"invalid fixed-slot transport element: {path}")
            expected_tree = _git("write-tree", cwd=repo, env=env)
            result.append({
                "batch_index": batch_index,
                "base_tree": current_tree,
                "expected_tree": expected_tree,
                "elements": elements,
            })
            current_tree = expected_tree
    return result


def _build_transport_plan(
    repo: Path, prepared: dict[str, object], *, publish_head: str, publish_tree: str,
) -> dict[str, object]:
    chunks, elements, stale = _desired_transport_elements(repo, prepared, base_tree=publish_tree)
    raw_batches = _pack_tree_elements(publish_tree, elements)
    batches = _expected_tree_batches(repo, base_tree=publish_tree, batches=raw_batches)
    return {
        "publish_head": publish_head,
        "publish_tree": publish_tree,
        "target_branch": prepared["target_branch"],
        "transport_chunk_bytes": _transport_part_capacity(target_branch=str(prepared["target_branch"])),
        "payload_part_count": len(chunks),
        "chunks": chunks,
        "stale_transport_path_count": len(stale),
        "stale_transport_paths": stale,
        "batches": batches,
        "tree_call_count": len(batches),
        "final_tree": batches[-1]["expected_tree"],
    }


def _connector_state_path(repo: Path) -> Path:
    return _connector_dir(repo) / CONNECTOR_STATE_NAME


def _write_connector_state(repo: Path, state: dict[str, object]) -> None:
    _connector_state_path(repo).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_connector_state(repo: Path) -> dict[str, object]:
    path = _connector_state_path(repo)
    if not path.exists():
        raise PublishStateError("Connector plan is not initialized; run connector-plan")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("version") != CONNECTOR_STATE_VERSION:
        raise PublishStateError("unsupported Connector state version")
    return state


def _write_summary(repo: Path, summary: dict[str, object]) -> None:
    (_connector_dir(repo) / CONNECTOR_SUMMARY_NAME).write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_execution_packets(
    repo: Path, state: dict[str, object],
) -> tuple[list[str], list[int], str]:
    plan = state["plan"]
    assert isinstance(plan, dict)
    batches = plan["batches"]
    assert isinstance(batches, list)
    packet_dir = _connector_dir(repo) / f"inputs-{len(batches) + 1:03d}"
    packet_dir.mkdir(exist_ok=False)
    tree_packets: list[str] = []
    tree_call_bytes: list[int] = []
    for index, batch in enumerate(batches):
        assert isinstance(batch, dict)
        packet = _tree_packet(
            str(batch["base_tree"]), list(batch["elements"]), index, str(batch["expected_tree"])
        )
        call_bytes = _connector_call_bytes(packet)
        if call_bytes > CONNECTOR_CALL_BUDGET_BYTES:
            raise PublishStateError("generated create_tree call exceeds Connector hard ceiling")
        path = packet_dir / f"tree-batch-{index:03d}.json"
        path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tree_packets.append(str(path))
        tree_call_bytes.append(call_bytes)

    commit_packet = {
        "stage": "create-publish-transport-commit",
        "action": "GitHub.create_commit",
        "action_args": {
            "repository_full_name": GITHUB_REPOSITORY,
            "message": f"Publish transport {state['request_id']}",
            "tree_sha": plan["final_tree"],
            "parent_sha": state["publish_base_head"],
        },
    }
    commit_path = packet_dir / "create-transport-commit.json"
    commit_path.write_text(json.dumps(commit_packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return tree_packets, tree_call_bytes, str(commit_path)


def cmd_connector_plan(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    manifest = _manifest_path(repo)
    verified = _verify_prepared_request(repo, manifest)
    prepared = _read_prepared_request(manifest)
    state_base = _read_state(repo)
    develop_head = _require_hex_sha(args.develop_head, name="observed develop HEAD")
    publish_head = _require_hex_sha(args.publish_head, name="observed publish HEAD")
    if develop_head != prepared["base_sha"] or develop_head != state_base["remote_commit"]:
        raise PublishStateError(
            f"develop HEAD moved since prepare: expected={prepared['base_sha']} actual={develop_head}"
        )
    if publish_head != state_base["publish_commit"]:
        raise PublishStateError(
            "publish transport HEAD moved since source state; refresh the publish base through the standard control/publish path: "
            f"expected={state_base['publish_commit']} actual={publish_head}"
        )
    _require_object(repo, f"{state_base['publish_tree']}^{{tree}}", name="recorded publish base tree")
    output = _connector_dir(repo)
    if output.exists() and any(output.iterdir()):
        raise PublishStateError("Connector plan already exists; continue its current stage")
    output.mkdir(parents=True, exist_ok=True)
    plan = _build_transport_plan(
        repo, prepared, publish_head=publish_head, publish_tree=state_base["publish_tree"]
    )
    state: dict[str, object] = {
        "version": CONNECTOR_STATE_VERSION,
        "stage": "execution-plan-ready",
        "request_id": prepared["request_id"],
        "develop_head": develop_head,
        "publish_base_head": publish_head,
        "publish_base_tree": state_base["publish_tree"],
        "manifest_sha256": _manifest_sha256(manifest),
        "prepared_publish_commit": prepared["publish_commit"],
        "prepared_target_tree": prepared["target_tree"],
        "local_target_commit": prepared["local_target_commit"],
        "payload_sha256": prepared["payload_sha256"],
        "plan": plan,
        "request_verified": bool(verified["verified"]),
    }
    tree_packets, tree_call_bytes, commit_packet = _write_execution_packets(repo, state)
    state["tree_packets"] = tree_packets
    state["commit_packet"] = commit_packet
    _write_connector_state(repo, state)
    execution_index = _write_execution_index(repo, state)
    execution_segments = _write_execution_segments(repo, state, execution_index)
    execution_index = execution_segments[0]
    summary = {
        "stage": state["stage"],
        "request_id": prepared["request_id"],
        "strategy": "fixed-slot-tree-content-batches",
        "connector_call_budget_bytes": CONNECTOR_CALL_BUDGET_BYTES,
        "transport_chunk_bytes": plan["transport_chunk_bytes"],
        "payload_part_count": plan["payload_part_count"],
        "tree_call_count": plan["tree_call_count"],
        "tree_packets": tree_packets,
        "tree_call_bytes": tree_call_bytes,
        "tree_expected_shas": [batch["expected_tree"] for batch in plan["batches"]],
        "commit_packet": commit_packet,
        "execution_index": execution_index,
        "execution_segment_count": len(execution_segments),
        "max_tree_calls_per_execution": MAX_TREE_CALLS_PER_EXECUTION,
        "stale_transport_path_count": plan["stale_transport_path_count"],
        "normal_pre_ref_helper_round_trips": 0,
        "normal_pre_ref_verification_reads": 0,
        "post_commit_ref_update": {
            "action": "GitHub.update_ref",
            "repository_full_name": GITHUB_REPOSITORY,
            "branch_name": PUBLISH_BRANCH,
            "sha": "<GitHub.create_commit returned SHA>",
            "force": False,
        },
        "next": (
            "Run the fixed scripts/publish_connector_launch.js in functions.exec using execution_index. "
            "It verifies and clears each segment’s temporary Library inputs before GitHub writes, "
            "runs at most 10 create_tree calls, and returns the next execution_index when needed. "
            "Repeat the same fixed Launcher with that index until the final commit and ref update. "
            "After Gateway success call record with the run evidence."
        ),
        "verified": True,
    }
    _write_summary(repo, summary)
    print(json.dumps(summary, indent=2))
    return 0



def _tool_call_fingerprint(text: str) -> int:
    """Compact copy-integrity check; the Git tree SHA remains authoritative."""
    value = 2166136261
    for byte in text.encode("utf-8"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def _verified_execution_packets(repo: Path, state: dict[str, object]) -> list[dict[str, object]]:
    """Read every original packet and validate it against the immutable execution plan."""
    plan = state["plan"]
    assert isinstance(plan, dict)
    batches = plan["batches"]
    files = state["tree_packets"]
    if not isinstance(batches, list) or not isinstance(files, list) or len(batches) != len(files) or not files:
        raise PublishStateError("invalid Connector execution plan")
    packets: list[dict[str, object]] = []
    for index, (file, batch) in enumerate(zip(files, batches)):
        if not isinstance(batch, dict):
            raise PublishStateError("invalid Connector tree batch")
        packet = json.loads(Path(str(file)).read_text(encoding="utf-8"))
        expected = _tree_packet(
            str(batch["base_tree"]), list(batch["elements"]), index,
            str(batch["expected_tree"]),
        )
        if packet != expected:
            raise PublishStateError(f"tree packet no longer matches the active plan: index {index}")
        packets.append(packet)

    commit_packet = json.loads(Path(str(state["commit_packet"])).read_text(encoding="utf-8"))
    expected_commit = {
        "stage": "create-publish-transport-commit",
        "action": "GitHub.create_commit",
        "action_args": {
            "repository_full_name": GITHUB_REPOSITORY,
            "message": f"Publish transport {state['request_id']}",
            "tree_sha": plan["final_tree"],
            "parent_sha": state["publish_base_head"],
        },
    }
    if commit_packet != expected_commit:
        raise PublishStateError("commit packet no longer matches the active plan")
    packets.append(commit_packet)
    return packets


def _write_execution_index(repo: Path, state: dict[str, object]) -> str:
    """Index the original verified Connector packet files, without copying their contents."""
    _verified_execution_packets(repo, state)
    paths = [*state["tree_packets"], state["commit_packet"]]
    directory = Path(str(paths[0])).parent
    if directory.resolve() != (_connector_dir(repo) / f"inputs-{len(paths):03d}").resolve():
        raise PublishStateError("Connector packet directory does not encode the input count")
    entries: list[dict[str, object]] = []
    for path in paths:
        file = Path(str(path))
        if file.parent.resolve() != directory.resolve():
            raise PublishStateError("Connector packet path escapes active transaction")
        raw = file.read_bytes()
        raw.decode("ascii")
        entries.append({
            "name": file.name,
            "size": len(raw),
            "fnv32": _tool_call_fingerprint(raw.decode("ascii")),
        })
    index = {
        "version": 1,
        "request_id": state["request_id"],
        "repository": GITHUB_REPOSITORY,
        "develop_head": state["develop_head"],
        "publish_base_head": state["publish_base_head"],
        "publish_base_tree": state["publish_base_tree"],
        "packets": entries,
    }
    path = directory / f"execution-index-{state['request_id']}.json"
    encoded = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="ascii") != encoded:
        raise PublishStateError("active Connector execution index differs from verified plan")
    if not path.exists():
        path.write_text(encoded, encoding="ascii")
    return str(path)



def _write_execution_segments(
    repo: Path, state: dict[str, object], original_index: str,
) -> list[str]:
    """Prepare self-contained <=10-tree-call executions from one verified plan.

    Short plans keep the original immutable index. The next index path is embedded
    in each longer segment, so an operator only repeats the same fixed launcher.
    No source commit, gateway packet or tree action is rewritten.
    """
    packets = _verified_execution_packets(repo, state)
    trees = packets[:-1]
    if len(trees) <= MAX_TREE_CALLS_PER_EXECUTION:
        return [original_index]

    base = _connector_dir(repo) / "segments"
    count = (len(trees) + MAX_TREE_CALLS_PER_EXECUTION - 1) // MAX_TREE_CALLS_PER_EXECUTION
    targets: list[tuple[Path, list[dict[str, object]], int, int]] = []
    for segment in range(count):
        first = segment * MAX_TREE_CALLS_PER_EXECUTION
        end = min(first + MAX_TREE_CALLS_PER_EXECUTION, len(trees))
        segment_dir = base / f"segment-{segment:03d}" / f"inputs-{end - first + 1:03d}"
        segment_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, object]] = []
        for local_index, tree in enumerate(trees[first:end]):
            source = Path(str(state["tree_packets"][first + local_index]))
            filename = f"tree-batch-{local_index:03d}.json"
            destination = segment_dir / filename
            raw = source.read_bytes()
            raw.decode("ascii")
            if destination.exists() and destination.read_bytes() != raw:
                raise PublishStateError("segmented Connector packet differs from verified plan")
            if not destination.exists():
                destination.write_bytes(raw)
            rows.append({"name": filename, "size": len(raw),
                         "fnv32": _tool_call_fingerprint(raw.decode("ascii"))})
        destination = segment_dir / "create-transport-commit.json"
        raw = Path(str(state["commit_packet"])).read_bytes()
        raw.decode("ascii")
        if destination.exists() and destination.read_bytes() != raw:
            raise PublishStateError("segmented Connector commit packet differs from verified plan")
        if not destination.exists():
            destination.write_bytes(raw)
        rows.append({"name": "create-transport-commit.json", "size": len(raw),
                     "fnv32": _tool_call_fingerprint(raw.decode("ascii"))})
        targets.append((segment_dir, rows, first, end))

    indices: list[str] = []
    for segment, (directory, rows, first, end) in enumerate(targets):
        next_path = (targets[segment + 1][0] / f"execution-index-{state['request_id']}.json") if segment + 1 < count else None
        data = {
            "version": 2,
            "request_id": state["request_id"],
            "repository": GITHUB_REPOSITORY,
            "develop_head": state["develop_head"],
            "publish_base_head": state["publish_base_head"],
            "publish_base_tree": state["publish_base_tree"],
            "first_tree_index": first,
            "total_tree_count": len(trees),
            "start_tree": trees[first]["action_args"]["base_tree_sha"],
            "end_tree": trees[end-1]["expected_tree"],
            "final_tree": state["plan"]["final_tree"],
            "next_execution_index": str(next_path) if next_path else None,
            "packets": rows,
        }
        path = directory / f"execution-index-{state['request_id']}.json"
        encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
        if path.exists() and path.read_text(encoding="ascii") != encoded:
            raise PublishStateError("segmented Connector index differs from verified plan")
        if not path.exists():
            path.write_text(encoded, encoding="ascii")
        indices.append(str(path))
    return indices



def cmd_connector_segments(_: argparse.Namespace) -> int:
    """Recover a previously planned large transaction without reauthoring it."""
    repo = _repo_from_cwd()
    state = _read_connector_state(repo)
    _verify_prepared_request(repo, _manifest_path(repo))
    if state["manifest_sha256"] != _manifest_sha256(_manifest_path(repo)):
        raise PublishStateError("prepared publish request changed after connector-plan")
    original_index = _write_execution_index(repo, state)
    indices = _write_execution_segments(repo, state, original_index)
    print(json.dumps({"execution_index": indices[0],
                      "execution_segment_count": len(indices),
                      "max_tree_calls_per_execution": MAX_TREE_CALLS_PER_EXECUTION}, indent=2))
    return 0


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def _verify_stale_connector_recovery(
    repo: Path, *, prepared: dict[str, object], state: dict[str, str],
    develop_head: str, publish_head: str,
) -> None:
    """Prove that a planned request cannot still publish its target.

    Connector execution may have created unreachable Git tree/commit objects, or
    may even have advanced the transport branch.  Those writes are not product
    publication.  Once ``develop`` has fast-forwarded from the request base to a
    history that does not contain this request's exact publish commit, the fixed
    Gateway can no longer publish that request: its bundle parent is stale, and
    every later standard develop update is fast-forward only.
    """
    connector = _read_connector_state(repo)
    if connector.get("stage") != "execution-plan-ready":
        raise PublishStateError(
            f"refresh-base cannot recover connector stage {connector.get('stage')}"
        )
    if _manifest_sha256(_manifest_path(repo)) != connector.get("manifest_sha256"):
        raise PublishStateError("prepared publish manifest changed after connector-plan")
    expected = {
        "request_id": prepared["request_id"],
        "develop_head": prepared["base_sha"],
        "publish_base_head": state["publish_commit"],
        "prepared_publish_commit": prepared["publish_commit"],
        "prepared_target_tree": prepared["target_tree"],
        "local_target_commit": prepared["local_target_commit"],
        "payload_sha256": prepared["payload_sha256"],
    }
    for key, value in expected.items():
        if connector.get(key) != value:
            raise PublishStateError(f"connector plan no longer matches prepared publish identity: {key}")
    if develop_head == state["remote_commit"]:
        raise PublishStateError(
            "connector-plan recovery requires develop to have moved; continue or diagnose the active transaction"
        )
    if _is_ancestor(repo, str(prepared["publish_commit"]), develop_head):
        raise PublishStateError(
            "prepared publish commit is already in observed develop history; record or diagnose the completed transaction"
        )
    if not _is_ancestor(repo, state["publish_commit"], publish_head):
        raise PublishStateError("observed publish HEAD does not fast-forward the recorded publish base")


def cmd_refresh_base(args: argparse.Namespace) -> int:
    """Advance recorded bases after another standard publish won the race.

    Before connector-plan, the stale request can be discarded directly after the
    caller integrates the observed develop HEAD.  After connector-plan, recovery
    is also allowed when the observed develop history proves that the request's
    exact publish commit was never published and can no longer succeed through
    the fixed Gateway.
    """
    repo = _repo_from_cwd()
    manifest = _manifest_path(repo)
    if not manifest.exists():
        raise PublishStateError("refresh-base requires an active prepared transaction")
    if not _working_tree_clean(repo):
        raise PublishStateError("refresh-base requires a clean working tree")

    prepared = _read_prepared_request(manifest)
    state = _read_state(repo)
    develop_head = _require_hex_sha(args.develop_head, name="observed develop HEAD")
    publish_head = _require_hex_sha(args.publish_head, name="observed publish HEAD")
    for commit, name in ((develop_head, "observed develop HEAD"), (publish_head, "observed publish HEAD")):
        _require_object(repo, f"{commit}^{{commit}}", name=name)

    if prepared["base_sha"] != state["remote_commit"]:
        raise PublishStateError("prepared transaction does not match recorded develop base")
    if develop_head == state["remote_commit"] and publish_head == state["publish_commit"]:
        raise PublishStateError("publish bases have not advanced; continue the active transaction")
    if not _is_ancestor(repo, state["remote_commit"], develop_head):
        raise PublishStateError("observed develop HEAD does not advance the recorded develop base")
    local_head = _git("rev-parse", "HEAD^{commit}", cwd=repo)
    if not _is_ancestor(repo, develop_head, local_head):
        raise PublishStateError("integrate the observed develop HEAD into local history before refresh-base")

    connector_dir = _connector_dir(repo)
    planned = connector_dir.exists() and any(connector_dir.iterdir())
    if planned:
        _verify_stale_connector_recovery(
            repo, prepared=prepared, state=state,
            develop_head=develop_head, publish_head=publish_head,
        )
    elif not _is_ancestor(repo, state["publish_commit"], publish_head):
        raise PublishStateError("observed publish HEAD does not fast-forward the recorded publish base")

    develop_tree = _git("rev-parse", f"{develop_head}^{{tree}}", cwd=repo)
    publish_tree = _git("rev-parse", f"{publish_head}^{{tree}}", cwd=repo)
    _write_state(
        repo, remote_commit=develop_head, remote_tree=develop_tree, local_head=develop_head,
        publish_commit=publish_head, publish_tree=publish_tree,
    )
    shutil.rmtree(_transaction_dir(repo))
    print(json.dumps({
        "refreshed": True,
        "recovered_stage": "connector-plan" if planned else "prepared",
        "discarded_request_id": prepared["request_id"],
        "remote_commit": develop_head,
        "remote_tree": develop_tree,
        "local_head": develop_head,
        "publish_commit": publish_head,
        "publish_tree": publish_tree,
        "verified": True,
    }, indent=2))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    manifest = _manifest_path(repo)
    if not manifest.exists():
        raise PublishStateError("no active publish transaction to cancel")
    prepared = _read_prepared_request(manifest)
    state = _read_state(repo)
    connector = _read_connector_state(repo)
    if connector.get("stage") != "execution-plan-ready":
        raise PublishStateError(f"cancel requires execution-plan-ready, found {connector.get('stage')}")
    develop_head = _require_hex_sha(args.develop_head, name="observed develop HEAD")
    publish_head = _require_hex_sha(args.publish_head, name="observed publish HEAD")
    if develop_head != prepared["base_sha"] or develop_head != state["remote_commit"]:
        raise PublishStateError("cannot cancel after develop moved")
    if publish_head != connector["publish_base_head"] or publish_head != state["publish_commit"]:
        raise PublishStateError("cannot cancel after publish transport moved")
    shutil.rmtree(_transaction_dir(repo))
    print(json.dumps({
        "cancelled": True, "request_id": prepared["request_id"],
        "develop_head": develop_head, "publish_head": publish_head, "verified": True,
    }, indent=2))
    return 0


def _verify_record_identity(
    repo: Path, prepared: dict[str, object], connector: dict[str, object],
) -> None:
    manifest = _manifest_path(repo)
    if _manifest_sha256(manifest) != connector.get("manifest_sha256"):
        raise PublishStateError("prepared publish manifest changed after connector-plan")
    expected = {
        "request_id": prepared["request_id"],
        "prepared_publish_commit": prepared["publish_commit"],
        "prepared_target_tree": prepared["target_tree"],
        "local_target_commit": prepared["local_target_commit"],
        "payload_sha256": prepared["payload_sha256"],
    }
    for key, value in expected.items():
        if connector.get(key) != value:
            raise PublishStateError(f"connector plan no longer matches prepared publish identity: {key}")
    state = _read_state(repo)
    if prepared["base_sha"] != state["remote_commit"]:
        raise PublishStateError("prepared publish base no longer matches local publish state")


def cmd_record(args: argparse.Namespace) -> int:
    repo = _repo_from_cwd()
    prepared = _read_prepared_request(_manifest_path(repo))
    connector = _read_connector_state(repo)
    if connector.get("stage") != "execution-plan-ready":
        raise PublishStateError(f"record requires execution-plan-ready, found {connector.get('stage')}")
    _verify_record_identity(repo, prepared, connector)
    transport_commit = _require_hex_sha(args.gateway_transport_commit, name="Gateway transport commit")
    if args.gateway_conclusion != "success":
        raise PublishStateError("Gateway run has not completed successfully; keep the active transaction for rerun or diagnosis")
    try:
        run_id = int(args.gateway_run_id)
    except ValueError as exc:
        raise PublishStateError("invalid Gateway run id") from exc
    if run_id <= 0:
        raise PublishStateError("invalid Gateway run id")
    plan = connector["plan"]
    assert isinstance(plan, dict)
    local_target = str(prepared["local_target_commit"])
    _write_state(
        repo,
        remote_commit=str(prepared["publish_commit"]),
        remote_tree=str(prepared["target_tree"]),
        local_head=local_target,
        publish_commit=transport_commit,
        publish_tree=str(plan["final_tree"]),
    )
    shutil.rmtree(_transaction_dir(repo))
    print(json.dumps({
        "remote_commit": prepared["publish_commit"],
        "remote_tree": prepared["target_tree"],
        "local_head": local_target,
        "publish_commit": transport_commit,
        "publish_tree": plan["final_tree"],
        "gateway_run_id": run_id,
        "gateway_conclusion": args.gateway_conclusion,
        "record_verification": "manifest-identity-and-gateway-run",
        "working_tree_clean": _working_tree_clean(repo),
        "uncommitted_changes_excluded": not _working_tree_clean(repo),
        "verified": True,
    }, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate and track the single standard AutoAdv develop publish transaction."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="initialize state from the source-snapshot bundle and publish base").set_defaults(func=cmd_init)
    sub.add_parser("prepare", help="prepare HEAD as the single active develop publish transaction").set_defaults(func=cmd_prepare)

    plan = sub.add_parser(
        "connector-plan",
        help="plan fixed-slot publish transport after one combined develop/publish ref observation",
    )
    plan.add_argument("--develop-head", required=True)
    plan.add_argument("--publish-head", required=True)
    plan.set_defaults(func=cmd_connector_plan)
    sub.add_parser(
        "connector-segments", help="recover a previously planned large transaction as <=10-tree-call executions",
    ).set_defaults(func=cmd_connector_segments)

    refresh = sub.add_parser(
        "refresh-base",
        help="replace a stale transaction after integrating a newer develop HEAD",
    )
    refresh.add_argument("--develop-head", required=True)
    refresh.add_argument("--publish-head", required=True)
    refresh.set_defaults(func=cmd_refresh_base)

    cancel = sub.add_parser("cancel", help="cancel a pre-ref active transaction after one combined ref observation")
    cancel.add_argument("--develop-head", required=True)
    cancel.add_argument("--publish-head", required=True)
    cancel.set_defaults(func=cmd_cancel)

    record = sub.add_parser("record", help="close the transaction from one successful Publish Gateway run observation")
    record.add_argument("--gateway-transport-commit", required=True)
    record.add_argument("--gateway-run-id", required=True)
    record.add_argument("--gateway-conclusion", required=True, choices=("success", "failure", "cancelled"))
    record.set_defaults(func=cmd_record)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (PublishStateError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
