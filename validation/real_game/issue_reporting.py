"""Publish bounded GitHub Issue evidence for Windows validation.

The Issue surface intentionally carries raw, compact facts that a later chat can
inspect directly. It does not publish validation-side safety/quality verdicts.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

HISTORY_SLOT_COUNT = 4
RUN_MARKER_RE = re.compile(
    r"<!--\s*autoadv-validation-run:(?P<run>\d{8}T\d{6}Z-[A-Za-z0-9_-]+)\s*-->"
)
TOKEN_RE = re.compile(r"(?i)(gh[pousr]_[a-z0-9_]{12,}|github_pat_[a-z0-9_]{12,})")
USER_PATH_RE = re.compile(r"(?i)[A-Z]:(?:\\){1,2}Users(?:\\){1,2}[^\\/\s\"']+")
DIAGNOSTIC_RE = re.compile(
    r"Advancement accuracy media|Accuracy setup failed|Traceback|RuntimeError|ValueError|Exception|警告|WARNING"
)

# GitHub does not document a stable issue-comment body limit in the REST API.
# Keep each physical comment comfortably bounded, but never treat this as a
# limit on one validation run: a run is split across as many comments as needed.
COMMENT_BODY_TARGET_CHARS = 60_000
COMMENT_METADATA_RESERVE_CHARS = 512
ARCHIVED_MARKER = "<!-- autoadv-validation-archived -->"
PART_MARKER_RE = re.compile(
    r"<!--\s*autoadv-validation-part:(?P<run>\d{8}T\d{6}Z-[A-Za-z0-9_-]+):(?P<part>\d+)\s*-->"
)


def history_marker(slot: int) -> str:
    if slot < 1 or slot > HISTORY_SLOT_COUNT:
        raise ValueError(f"invalid validation history slot: {slot}")
    return f"<!-- autoadv-validation-slot-{slot} -->"


def part_marker(run: str, part: int) -> str:
    if part < 2:
        raise ValueError(f"invalid validation continuation part: {part}")
    return f"<!-- autoadv-validation-part:{run}:{part} -->"


def parts_marker(count: int) -> str:
    if count < 1:
        raise ValueError(f"invalid validation part count: {count}")
    return f"<!-- autoadv-validation-parts:{count} -->"


def sanitize_public_issue(content: str) -> str:
    content = USER_PATH_RE.sub("<USERPROFILE>", content)
    return TOKEN_RE.sub("<REDACTED-TOKEN>", content)


def _metric(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


@dataclass(frozen=True)
class Snapshot:
    run: str
    parts: tuple[str, ...]
    summary: dict[str, Any]

    @property
    def body(self) -> str:
        """Logical run body, independent of physical Issue comment partitioning."""
        return "\n\n".join(self.parts)


def _render_timeline_part(prefix: list[str], rows: list[str]) -> str:
    lines = list(prefix)
    if rows:
        if lines:
            lines.append("")
        lines.extend((
            "<details open><summary>analysis evidence timeline</summary>", "", "```jsonl",
            *rows,
            "```", "</details>",
        ))
    return "\n".join(lines)


def _partition_snapshot(prefix: list[str], timeline_rows: list[str]) -> tuple[str, ...]:
    """Partition one logical run without dropping or compacting factual rows."""
    payload_limit = COMMENT_BODY_TARGET_CHARS - COMMENT_METADATA_RESERVE_CHARS
    if payload_limit <= 0:
        raise ValueError("invalid Issue comment payload budget")
    if not timeline_rows:
        body = "\n".join(prefix)
        if len(body) > payload_limit:
            raise ValueError(
                "validation summary itself exceeds the Issue comment payload budget: "
                f"{len(body)} > {payload_limit}"
            )
        return (body,)

    parts: list[str] = []
    current: list[str] = []
    current_prefix = prefix
    for row in timeline_rows:
        candidate = _render_timeline_part(current_prefix, [*current, row])
        if len(candidate) <= payload_limit:
            current.append(row)
            continue
        if not current:
            raise ValueError(
                "one validation evidence record exceeds the Issue comment payload budget: "
                f"{len(candidate)} > {payload_limit}"
            )
        parts.append(_render_timeline_part(current_prefix, current))
        current_prefix = []
        current = [row]
        candidate = _render_timeline_part(current_prefix, current)
        if len(candidate) > payload_limit:
            raise ValueError(
                "one validation evidence record exceeds the Issue comment payload budget: "
                f"{len(candidate)} > {payload_limit}"
            )
    parts.append(_render_timeline_part(current_prefix, current))
    return tuple(parts)


def format_snapshot(root: Path, sha: str) -> Snapshot:
    summary = _read_json(root / "validation-summary.json")
    if summary is None:
        raise ValueError("No verification summary exists to upload")
    run = root.name
    lines: list[str] = [
        f"### `{run}` · `{sha}`",
        f"Requested: `{summary.get('phase_requested', '-')}` · "
        f"{summary.get('started_utc', '-')} → {summary.get('ended_utc', '-')}",
        "",
        "| phase | fixture/requested | hook outputs | utterances | presentations | inputs | recovery | report | error |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for phase in summary.get("phases", ()):
        facts = phase.get("facts") if isinstance(phase.get("facts"), dict) else {}
        lines.append(
            f"| {_cell(phase.get('phase'))} | "
            f"{_metric(facts.get('fixture_cases'))}/{_metric(facts.get('requested_cases'))} | "
            f"{_metric(facts.get('hook_outputs'))} | {_metric(facts.get('utterances'))} | "
            f"{_metric(facts.get('presentation_completions'))} | {_metric(facts.get('advance_inputs'))} | "
            f"{_metric(facts.get('validation_recovery_inputs'))} | "
            f"{_metric(phase.get('report_present'))} | {_cell(phase.get('runner_error'))} |"
        )
    if summary.get("launcher_error"):
        lines.extend(("", f"Launcher error: `{_cell(summary.get('launcher_error'))}`"))

    timeline_rows: list[str] = []
    report = _read_json(root / "autoadv-accuracy.result.json")
    if report is not None:
        verdict = report.get("verdict")
        if verdict:
            lines.extend(("", f"Offline verifier: `{_cell(verdict)}`"))
        issues = [str(item) for item in report.get("evidence_issues", ()) if str(item).strip()]
        if issues:
            lines.extend(("", "**Evidence issues**"))
            lines.extend(f"- {sanitize_public_issue(item)}" for item in issues[:12])
        timeline = report.get("timeline")
        if isinstance(timeline, list):
            timeline_rows = [
                sanitize_public_issue(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
                for item in timeline if isinstance(item, dict)
            ]

    accuracy_phase = next((p for p in summary.get("phases", ()) if p.get("phase") == "accuracy"), None)
    if isinstance(accuracy_phase, dict) and not accuracy_phase.get("report_present"):
        log_path = root / "validation.log"
        if log_path.is_file():
            diagnostic = [
                line.rstrip("\r\n")
                for line in log_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                if DIAGNOSTIC_RE.search(line)
            ][-80:]
            if diagnostic:
                lines.extend((
                    "", "<details open><summary>setup diagnostics</summary>", "", "```text",
                    sanitize_public_issue("\n".join(diagnostic)), "```", "</details>",
                ))

    return Snapshot(
        run=run,
        parts=_partition_snapshot(lines, timeline_rows),
        summary=summary,
    )


def _run_from_body(body: str) -> str | None:
    match = RUN_MARKER_RE.search(body or "")
    return match.group("run") if match else None


def _flatten_comments(value: Any) -> list[dict[str, Any]]:
    """Normalize GitHub CLI --paginate --slurp output without inferring absence.

    gh emits one JSON array per page and --slurp wraps those pages in one outer
    array.  A plain one-page JSON array is accepted as well so the planner is
    testable independently of the shell.  Anything else is an acquisition
    failure, not an empty Issue.
    """
    if not isinstance(value, list):
        raise ValueError("GitHub issue comments must be a JSON array")
    if value and all(isinstance(page, list) for page in value):
        items = [comment for page in value for comment in page]
    else:
        items = value
    if not all(isinstance(comment, dict) for comment in items):
        raise ValueError("GitHub issue comments contain a non-object entry")
    return list(items)


def _fixed_issue_surface(comments: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Require exactly one pre-existing comment for each evidence-history slot."""
    matches: dict[str, list[dict[str, Any]]] = {
        history_marker(slot): [] for slot in range(1, HISTORY_SLOT_COUNT + 1)
    }
    for comment in comments:
        body = str(comment.get("body") or "").lstrip()
        for marker in matches:
            if body.startswith(marker):
                matches[marker].append(comment)
                break
    invalid = [f"{marker}: {len(found)}" for marker, found in matches.items() if len(found) != 1]
    if invalid:
        raise ValueError(
            "Validation Issue surface is not canonical; expected exactly one "
            "comment for each evidence history marker (" + ", ".join(invalid) + ")"
        )
    return {slot: matches[history_marker(slot)][0]
            for slot in range(1, HISTORY_SLOT_COUNT + 1)}


def _choose_history_slot(slots: dict[int, dict[str, Any]], run: str) -> tuple[int, int]:
    same_run = [
        (slot, int(comment["id"]))
        for slot, comment in slots.items()
        if _run_from_body(str(comment.get("body") or "")) == run
    ]
    if len(same_run) > 1:
        raise ValueError(f"Validation run {run} exists in multiple history slots")
    if same_run:
        return same_run[0]

    candidates = [
        (
            _run_from_body(str(comment.get("body") or "")) or "",
            slot,
            int(comment["id"]),
        )
        for slot, comment in slots.items()
    ]
    _, slot, comment_id = min(candidates)
    return slot, comment_id


def choose_history_slot(comments: Iterable[dict[str, Any]], run: str) -> tuple[int, int]:
    """Return the one fixed history comment to PATCH for this run."""
    return _choose_history_slot(_fixed_issue_surface(comments), run)


def _continuation_from_body(body: str) -> tuple[str, int] | None:
    match = PART_MARKER_RE.search(body or "")
    if not match:
        return None
    return match.group("run"), int(match.group("part"))


def _comment_body(marker: str, run: str, part: int, total: int, payload: str) -> str:
    lines = [marker, f"<!-- autoadv-validation-run:{run} -->", parts_marker(total)]
    if part > 1:
        lines.append(f"Continuation `{part}/{total}`")
    lines.extend(("", payload))
    body = "\n".join(lines)
    if len(body) > COMMENT_BODY_TARGET_CHARS:
        raise ValueError(
            f"Issue comment partition exceeds target size: {len(body)} > {COMMENT_BODY_TARGET_CHARS}"
        )
    return body


def build_upload_plan(root: Path, sha: str,
                      comments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    snapshot = format_snapshot(root, sha)
    comments = list(comments)
    slots = _fixed_issue_surface(comments)
    history_slot, comment_id = _choose_history_slot(slots, snapshot.run)
    total = len(snapshot.parts)

    existing_parts: dict[int, dict[str, Any]] = {}
    reusable: list[dict[str, Any]] = []
    active_runs = {
        run for run in (_run_from_body(str(comment.get("body") or "")) for comment in slots.values())
        if run
    }
    for comment in comments:
        body = str(comment.get("body") or "").lstrip()
        continuation = _continuation_from_body(body)
        if continuation is not None:
            run, part = continuation
            if run == snapshot.run:
                if part in existing_parts:
                    raise ValueError(f"Validation run {run} has duplicate continuation part {part}")
                existing_parts[part] = comment
            elif run not in active_runs:
                reusable.append(comment)
        elif body.startswith(ARCHIVED_MARKER):
            reusable.append(comment)

    operations: list[dict[str, Any]] = []
    for part in range(2, total + 1):
        payload = snapshot.parts[part - 1]
        body = _comment_body(part_marker(snapshot.run, part), snapshot.run, part, total, payload)
        existing = existing_parts.get(part)
        if existing is not None:
            operations.append({
                "action": "patch", "role": "continuation", "slot": history_slot,
                "part": part, "comment_id": int(existing["id"]), "body": body,
            })
        elif reusable:
            recycled = reusable.pop(0)
            operations.append({
                "action": "patch", "role": "continuation", "slot": history_slot,
                "part": part, "comment_id": int(recycled["id"]), "body": body,
            })
        else:
            operations.append({
                "action": "create", "role": "continuation", "slot": history_slot,
                "part": part, "body": body,
            })

    root_body = _comment_body(
        history_marker(history_slot), snapshot.run, 1, total, snapshot.parts[0]
    )
    operations.append({
        "action": "patch", "role": "history", "slot": history_slot,
        "part": 1, "comment_id": comment_id, "body": root_body,
    })

    # The root is the logical commit point. Only after it changes may obsolete
    # continuation comments from the evicted run/current retry be reclaimed.
    old_run = _run_from_body(str(slots[history_slot].get("body") or ""))
    obsolete_ids: set[int] = set()
    for comment in comments:
        continuation = _continuation_from_body(str(comment.get("body") or ""))
        if continuation is None:
            continue
        run, part = continuation
        if (old_run and old_run != snapshot.run and run == old_run) or (
            run == snapshot.run and part > total
        ):
            obsolete_ids.add(int(comment["id"]))
    for obsolete_id in sorted(obsolete_ids):
        operations.append({
            "action": "patch", "role": "archive", "slot": history_slot,
            "comment_id": obsolete_id, "body": ARCHIVED_MARKER,
        })

    return {"run": snapshot.run, "parts": total, "operations": operations}


class IssueClient(Protocol):
    def list_comments(self) -> list[dict[str, Any]]: ...

    def patch_comment(self, comment_id: int, body: str) -> dict[str, Any]: ...

    def create_comment(self, body: str) -> dict[str, Any]: ...

    def get_comment(self, comment_id: int) -> dict[str, Any]: ...


class GitHubCliIssueClient:
    """Minimal UTF-8 byte-oriented GitHub Issue client backed by ``gh api``."""

    def __init__(self, executable: Path, repository: str, issue_number: int):
        self.executable = str(executable)
        self.repository = repository
        self.issue_number = issue_number

    def _run_json(self, args: list[str], *, input_body: str | None = None) -> Any:
        request_path: Path | None = None
        try:
            command = [self.executable, "api", *args]
            if input_body is not None:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", suffix=".json", delete=False
                ) as request:
                    json.dump({"body": input_body}, request, ensure_ascii=False)
                    request_path = Path(request.name)
                command.extend(("--input", str(request_path)))
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if completed.returncode != 0:
                diagnostic = completed.stderr.decode("utf-8", errors="replace").strip()
                diagnostic = sanitize_public_issue(diagnostic)
                raise RuntimeError(
                    f"GitHub issue API failed (exit code {completed.returncode})"
                    + (f": {diagnostic}" if diagnostic else "")
                )
            try:
                return json.loads(completed.stdout.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("GitHub issue API returned invalid UTF-8 JSON") from exc
        finally:
            if request_path is not None:
                request_path.unlink(missing_ok=True)

    def list_comments(self) -> list[dict[str, Any]]:
        value = self._run_json([
            "--method", "GET", "--paginate", "--slurp",
            f"repos/{self.repository}/issues/{self.issue_number}/comments?per_page=100",
        ])
        return _flatten_comments(value)

    def patch_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        value = self._run_json(
            ["--method", "PATCH", f"repos/{self.repository}/issues/comments/{comment_id}"],
            input_body=body,
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub PATCH returned a non-object response")
        return value

    def create_comment(self, body: str) -> dict[str, Any]:
        value = self._run_json(
            ["--method", "POST", f"repos/{self.repository}/issues/{self.issue_number}/comments"],
            input_body=body,
        )
        if not isinstance(value, dict):
            raise RuntimeError("GitHub POST returned a non-object response")
        return value

    def get_comment(self, comment_id: int) -> dict[str, Any]:
        value = self._run_json([
            "--method", "GET", f"repos/{self.repository}/issues/comments/{comment_id}"
        ])
        if not isinstance(value, dict):
            raise RuntimeError("GitHub comment verification returned a non-object response")
        return value


class IssuePublishError(RuntimeError):
    def __init__(self, message: str, completed: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.completed = list(completed or ())


def _verify_operation(operation: dict[str, Any], run: str, comment: dict[str, Any]) -> None:
    expected_id = int(operation["comment_id"])
    if int(comment.get("id", -1)) != expected_id:
        raise RuntimeError(f"GitHub verification returned the wrong comment for {operation['role']}")
    body = str(comment.get("body") or "").lstrip()
    role = operation["role"]
    if role == "archive":
        if not body.startswith(ARCHIVED_MARKER):
            raise RuntimeError("GitHub verification did not archive the continuation comment")
        return
    if role == "history":
        expected_marker = history_marker(int(operation["slot"]))
    else:
        expected_marker = part_marker(run, int(operation["part"]))
    if not body.startswith(expected_marker):
        raise RuntimeError(f"GitHub verification lost the {role} marker")
    if _run_from_body(body) != run:
        raise RuntimeError(f"GitHub verification found the wrong run in {role}")


def publish_snapshot(root: Path, sha: str, client: IssueClient) -> dict[str, Any]:
    """Publish one logical run across one or more Issue comments."""
    completed: list[dict[str, Any]] = []
    try:
        plan = build_upload_plan(root, sha, client.list_comments())
        for operation in plan["operations"]:
            action = operation.get("action", "patch")
            if action == "create":
                response = client.create_comment(str(operation["body"]))
                comment_id = int(response.get("id", -1))
                if comment_id < 0:
                    raise RuntimeError("GitHub returned no valid comment id for created continuation")
                operation = {**operation, "comment_id": comment_id}
            else:
                comment_id = int(operation["comment_id"])
                response = client.patch_comment(comment_id, str(operation["body"]))
            url = str(response.get("html_url") or "")
            if not url.startswith("https://github.com/"):
                raise RuntimeError(f"GitHub returned no valid URL for {operation['role']} comment")
            verified = client.get_comment(comment_id)
            _verify_operation(operation, plan["run"], verified)
            completed.append({
                "role": operation["role"],
                "slot": operation.get("slot"),
                "part": operation.get("part"),
                "comment_id": comment_id,
                "url": url,
            })
        return {"run": plan["run"], "parts": plan["parts"], "operations": completed}
    except Exception as exc:
        if isinstance(exc, IssuePublishError):
            raise
        raise IssuePublishError(str(exc), completed) from exc


def _write_status(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish bounded Issue validation updates")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--gh", type=Path, required=True)
    parser.add_argument("--status", type=Path)
    args = parser.parse_args()
    status_path = args.status or (args.root / "upload-status.json")
    base_status = {
        "schema": "autoadv-issue-upload-v1",
        "repository": args.repository,
        "issue": args.issue,
        "run": args.root.name,
        "commit": args.sha,
    }
    try:
        client = GitHubCliIssueClient(args.gh, args.repository, args.issue)
        result = publish_snapshot(args.root, args.sha, client)
        _write_status(status_path, {
            **base_status,
            "status": "uploaded",
            "parts": result["parts"],
            "operations": result["operations"],
            "error": None,
        })
        # Keep console output ASCII-only so locale cannot affect publication.
        print(f"Issue upload complete: run={result['run']} operations={len(result['operations'])}")
        return 0
    except Exception as exc:
        error = sanitize_public_issue(f"{type(exc).__name__}: {exc}")
        completed = exc.completed if isinstance(exc, IssuePublishError) else []
        _write_status(status_path, {
            **base_status,
            "status": "failed",
            "operations": completed,
            "error": error,
        })
        print("Issue upload failed; see upload-status.json")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
