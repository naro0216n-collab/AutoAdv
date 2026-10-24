"""Publish bounded GitHub Issue diagnostics for Windows accuracy validation.

The reporter owns the complete GitHub Issue transaction: acquisition, fixed-slot
selection, PATCH and post-write verification.  PowerShell only ensures that the
GitHub CLI is authenticated and launches this module; issue bodies never cross a
Windows console encoding boundary.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

HISTORY_SLOT_COUNT = 4
MAX_ISSUE_BODY_CHARS = 64_000
EVIDENCE_MARKER = "<!-- autoadv-analysis-evidence:zip+base64 -->"
BASELINE_MARKER = "<!-- autoadv-validation-baseline -->"
RUN_MARKER_RE = re.compile(
    r"<!--\s*autoadv-validation-run:(?P<run>\d{8}T\d{6}Z-[A-Za-z0-9_-]+)\s*-->"
)
TOKEN_RE = re.compile(r"(?i)(gh[pousr]_[a-z0-9_]{12,}|github_pat_[a-z0-9_]{12,})")
USER_PATH_RE = re.compile(r"(?i)[A-Z]:(?:\\){1,2}Users(?:\\){1,2}[^\\/\s\"']+")
DIAGNOSTIC_RE = re.compile(
    r"Advancement accuracy media|Accuracy setup failed|Traceback|RuntimeError|ValueError|Exception|警告|WARNING"
)


def history_marker(slot: int) -> str:
    if slot < 1 or slot > HISTORY_SLOT_COUNT:
        raise ValueError(f"invalid validation history slot: {slot}")
    return f"<!-- autoadv-validation-slot-{slot} -->"


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


def _limited_items(lines: list[str], heading: str, items: Iterable[Any] | None,
                   limit: int = 12) -> None:
    values = [str(item) for item in (items or ()) if str(item).strip()]
    if not values:
        return
    lines.extend(("", f"**{heading}**"))
    lines.extend(f"- {sanitize_public_issue(item)}" for item in values[:limit])
    if len(values) > limit:
        lines.append(f"- … {len(values) - limit} more")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value




def _analysis_evidence_archive(root: Path, sha: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Return a compact, reconstructable bundle of the evidence needed for analysis.

    The bundle intentionally excludes redistributable/non-redistributable media and runtime
    artifacts.  It contains only structured measurement evidence plus a sanitized transcript.
    """
    candidates: list[Path] = [
        root / "validation-summary.json",
        root / "autoadv-accuracy.result.json",
        root / "autoadv-accuracy.jsonl",
    ]
    candidates.extend(sorted(root.glob("autoadv-game-*.jsonl")))

    payloads: list[tuple[str, bytes]] = []
    for path in candidates:
        if path.is_file():
            payloads.append((path.name, path.read_bytes()))

    log_path = root / "validation.log"
    if log_path.is_file():
        sanitized = sanitize_public_issue(
            log_path.read_text(encoding="utf-8-sig", errors="replace")
        ).encode("utf-8")
        payloads.append(("validation.log", sanitized))

    if not payloads:
        raise ValueError("No analysis evidence exists to upload")

    manifest_files = [
        {
            "name": name,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in payloads
    ]
    manifest = {
        "schema": "autoadv-analysis-evidence-v1",
        "run": root.name,
        "commit": sha,
        "files": manifest_files,
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        for name, content in payloads:
            archive.writestr(name, content)
    archive_bytes = buffer.getvalue()
    return (
        base64.b64encode(archive_bytes).decode("ascii"),
        hashlib.sha256(archive_bytes).hexdigest(),
        manifest_files,
    )


def _append_analysis_evidence(lines: list[str], root: Path, sha: str) -> None:
    encoded, digest, manifest_files = _analysis_evidence_archive(root, sha)
    listed = ", ".join(item["name"] for item in manifest_files)
    lines.extend((
        "",
        "<details><summary>analysis evidence bundle</summary>",
        "",
        EVIDENCE_MARKER,
        f"ZIP SHA-256: `{digest}`",
        f"Files: `{listed}`",
        "Decode the following base64 as a ZIP archive; `manifest.json` contains per-file SHA-256 values.",
        "",
        "```text",
        encoded,
        "```",
        "</details>",
    ))


def baseline_eligible(summary: dict[str, Any]) -> bool:
    phase = next((p for p in summary.get("phases", ()) if p.get("phase") == "accuracy"), None)
    if not isinstance(phase, dict):
        return False
    return (
        phase.get("report_present") is True
        and phase.get("measurement_valid") is True
        and phase.get("collection_complete") is True
        and phase.get("validation_recovery_inputs") == 0
        and phase.get("voice_safety_verified") is True
        and isinstance(phase.get("quality"), dict)
        and phase["quality"].get("process_audio_observation_rate") == 1.0
        and phase["quality"].get("alignment_observation_rate") == 1.0
        and phase["quality"].get("mix_environment_observation_rate") == 1.0
        and not phase.get("runner_error")
    )


@dataclass(frozen=True)
class Snapshot:
    run: str
    body: str
    summary: dict[str, Any]


def format_snapshot(root: Path, sha: str) -> Snapshot:
    summary_path = root / "validation-summary.json"
    summary = _read_json(summary_path)
    if summary is None:
        raise ValueError("No verification summary exists to upload")

    run = root.name
    lines: list[str] = [
        f"<!-- autoadv-validation-run:{run} -->",
        f"### `{run}` · `{sha}`",
        f"Requested: `{summary.get('phase_requested', '-')}` · "
        f"{summary.get('started_utc', '-')} → {summary.get('ended_utc', '-')}",
        "",
        "| phase | observed/requested | collection | measurement | auto | safe | PCM | alignment | mix | native fg | recovery | early | overhold | misattr | error |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for phase in summary.get("phases", ()):
        quality = phase.get("quality") if isinstance(phase.get("quality"), dict) else {}
        lines.append(
            f"| {_cell(phase.get('phase'))} | "
            f"{_metric(quality.get('observed_cases'))}/{_metric(quality.get('requested_cases'))} | "
            f"{_metric(phase.get('collection_valid'))} | {_metric(phase.get('measurement_valid'))} | "
            f"{_metric(quality.get('automatic_advance_rate'))} | "
            f"{_metric(quality.get('safe_automatic_advance_rate'))} | "
            f"{_metric(quality.get('process_audio_observation_rate'))} | "
            f"{_metric(quality.get('alignment_observation_rate'))} | "
            f"{_metric(quality.get('mix_environment_observation_rate'))} | "
            f"{_metric(quality.get('foreground_native_completion_rate'))} | "
            f"{_metric(quality.get('recovery_rate'))} | {_metric(quality.get('early_input_rate'))} | "
            f"{_metric(quality.get('overhold_rate'))} | {_metric(quality.get('misattribution_rate'))} | "
            f"{_cell(phase.get('runner_error'))} |"
        )
    if summary.get("launcher_error"):
        lines.extend(("", f"Launcher error: `{_cell(summary.get('launcher_error'))}`"))

    for phase_name in ("accuracy",):
        report = _read_json(root / f"autoadv-{phase_name}.result.json")
        if report is None:
            continue
        lines.extend(("", f"<details><summary>{phase_name} diagnostics</summary>"))
        performance = report.get("performance")
        if isinstance(performance, dict) and performance:
            lines.extend((
                "",
                "| metric | count | p50 | p95 | max |",
                "| --- | ---: | ---: | ---: | ---: |",
            ))
            for name, stats in performance.items():
                stats = stats if isinstance(stats, dict) else {}
                lines.append(
                    f"| {_cell(name)} | {_metric(stats.get('count'))} | {_metric(stats.get('p50'))} | "
                    f"{_metric(stats.get('p95'))} | {_metric(stats.get('max'))} |"
                )
        if phase_name == "accuracy" and isinstance(report.get("line_results"), list):
            lines.extend((
                "",
                "| case | scene | speaker | environment | mix | outcome | completion | PCM | alignment | game | end→input ms |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- | ---: |",
            ))
            for line in report["line_results"]:
                if not isinstance(line, dict):
                    continue
                lines.append(
                    f"| {_cell(line.get('scenario'))} | {_cell(line.get('scene'))} | "
                    f"{_cell(line.get('speaker_family'))} | {_cell(line.get('audio_environment'))} | "
                    f"{_cell(','.join(line.get('mix_channels_observed') or []))} | {_cell(line.get('outcome'))} | "
                    f"{_cell(line.get('completion_reason'))} | {_metric(line.get('pcm_observed'))} | "
                    f"{_metric(line.get('alignment_inference_count'))} | "
                    f"{_cell(line.get('game_outcome'))} | {_metric(line.get('game_end_to_input_ms'))} |"
                )
        _limited_items(lines, "Quality issues", report.get("quality_issues"))
        _limited_items(lines, "Errors", report.get("errors"))
        _limited_items(lines, "Diagnostics", report.get("diagnostics"), 8)
        lines.extend(("", "</details>"))

    _append_analysis_evidence(lines, root, sha)

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

    body = "\n".join(lines)
    if len(body) > MAX_ISSUE_BODY_CHARS:
        raise ValueError(
            f"Issue diagnostics exceed bounded comment size: {len(body)} > {MAX_ISSUE_BODY_CHARS}"
        )
    return Snapshot(run=run, body=body, summary=summary)


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


def _fixed_issue_surface(comments: Iterable[dict[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
    """Require exactly one pre-existing comment for every machine-owned marker."""
    matches: dict[str, list[dict[str, Any]]] = {
        **{history_marker(slot): [] for slot in range(1, HISTORY_SLOT_COUNT + 1)},
        BASELINE_MARKER: [],
    }
    for comment in comments:
        body = str(comment.get("body") or "").lstrip()
        for marker in matches:
            if body.startswith(marker):
                matches[marker].append(comment)
                break

    invalid = [
        f"{marker}: {len(found)}"
        for marker, found in matches.items()
        if len(found) != 1
    ]
    if invalid:
        raise ValueError(
            "Validation Issue surface is not canonical; expected exactly one "
            "comment for each fixed marker (" + ", ".join(invalid) + ")"
        )

    slots = {
        slot: matches[history_marker(slot)][0]
        for slot in range(1, HISTORY_SLOT_COUNT + 1)
    }
    return slots, matches[BASELINE_MARKER][0]


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
    slots, _ = _fixed_issue_surface(comments)
    return _choose_history_slot(slots, run)


def build_upload_plan(root: Path, sha: str,
                      comments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    snapshot = format_snapshot(root, sha)
    comments = list(comments)
    slots, baseline = _fixed_issue_surface(comments)
    history_slot, comment_id = _choose_history_slot(slots, snapshot.run)

    operations = [{
        "role": "history",
        "slot": history_slot,
        "comment_id": comment_id,
        "body": f"{history_marker(history_slot)}\n{snapshot.body}",
    }]
    if baseline_eligible(snapshot.summary):
        operations.append({
            "role": "baseline",
            "comment_id": int(baseline["id"]),
            "body": f"{BASELINE_MARKER}\n{snapshot.body}",
        })
    return {"run": snapshot.run, "operations": operations}


class IssueClient(Protocol):
    def list_comments(self) -> list[dict[str, Any]]: ...

    def patch_comment(self, comment_id: int, body: str) -> dict[str, Any]: ...

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
    body = str(comment.get("body") or "")
    expected_marker = (
        history_marker(int(operation["slot"]))
        if operation["role"] == "history"
        else BASELINE_MARKER
    )
    if not body.lstrip().startswith(expected_marker):
        raise RuntimeError(f"GitHub verification lost the {operation['role']} marker")
    if _run_from_body(body) != run:
        raise RuntimeError(f"GitHub verification found the wrong run in {operation['role']}")


def publish_snapshot(root: Path, sha: str, client: IssueClient) -> dict[str, Any]:
    """Publish one run while preserving one-run-per-history-comment identity."""
    completed: list[dict[str, Any]] = []
    try:
        plan = build_upload_plan(root, sha, client.list_comments())
        for operation in plan["operations"]:
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
                "comment_id": comment_id,
                "url": url,
            })
        return {"run": plan["run"], "operations": completed}
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
