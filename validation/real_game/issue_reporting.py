"""Build bounded GitHub Issue diagnostics for Windows auto-advance accuracy validation.

The Windows runner owns GitHub authentication and HTTP mutation. This module owns
only deterministic snapshot formatting and fixed-slot selection so the behavior
is executable on every Fast CI platform.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

HISTORY_SLOT_COUNT = 4
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
        "| phase | observed/requested | collection | measurement | auto | safe | recovery | early | overhold | misattr | error |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for phase in summary.get("phases", ()):
        quality = phase.get("quality") if isinstance(phase.get("quality"), dict) else {}
        lines.append(
            f"| {_cell(phase.get('phase'))} | "
            f"{_metric(quality.get('observed_cases'))}/{_metric(quality.get('requested_cases'))} | "
            f"{_metric(phase.get('collection_valid'))} | {_metric(phase.get('measurement_valid'))} | "
            f"{_metric(quality.get('automatic_advance_rate'))} | "
            f"{_metric(quality.get('safe_automatic_advance_rate'))} | "
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
                "| case | speaker | environment | outcome | completion | ASR | game | end→input ms |",
                "| --- | --- | --- | --- | --- | ---: | --- | ---: |",
            ))
            for line in report["line_results"]:
                if not isinstance(line, dict):
                    continue
                lines.append(
                    f"| {_cell(line.get('scenario'))} | {_cell(line.get('speaker_family'))} | "
                    f"{_cell(line.get('audio_environment'))} | {_cell(line.get('outcome'))} | "
                    f"{_cell(line.get('completion_reason'))} | {_metric(line.get('asr_inference_count'))} | "
                    f"{_cell(line.get('game_outcome'))} | {_metric(line.get('game_end_to_input_ms'))} |"
                )
        _limited_items(lines, "Quality issues", report.get("quality_issues"))
        _limited_items(lines, "Errors", report.get("errors"))
        _limited_items(lines, "Diagnostics", report.get("diagnostics"), 8)
        lines.extend(("", "</details>"))

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

    return Snapshot(run=run, body="\n".join(lines), summary=summary)


def _run_from_body(body: str) -> str | None:
    match = RUN_MARKER_RE.search(body or "")
    return match.group("run") if match else None


def choose_history_slot(comments: Iterable[dict[str, Any]], run: str) -> tuple[int, int]:
    """Return (slot_number, comment_id) for one idempotent ring update.

    Existing content for the same run is rewritten in place. Otherwise an empty
    slot is preferred, then the slot containing the lexicographically oldest
    timestamped run. The run prefix is UTC YYYYMMDDTHHMMSSZ, so lexical order
    is chronological.
    """
    slots: dict[int, dict[str, Any]] = {}
    for comment in comments:
        body = str(comment.get("body") or "")
        for slot in range(1, HISTORY_SLOT_COUNT + 1):
            if body.lstrip().startswith(history_marker(slot)):
                slots.setdefault(slot, comment)
                break
    missing = [slot for slot in range(1, HISTORY_SLOT_COUNT + 1) if slot not in slots]
    if missing:
        raise ValueError(f"Validation history slot {missing[0]} is missing")

    for slot, comment in slots.items():
        if _run_from_body(str(comment.get("body") or "")) == run:
            return slot, int(comment["id"])

    candidates: list[tuple[str, int, int]] = []
    for slot, comment in slots.items():
        existing = _run_from_body(str(comment.get("body") or ""))
        # Malformed/empty fixed slots are the first replacement candidates.
        sort_key = existing or ""
        candidates.append((sort_key, slot, int(comment["id"])))
    _, slot, comment_id = min(candidates)
    return slot, comment_id


def build_upload_plan(root: Path, sha: str,
                      comments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    snapshot = format_snapshot(root, sha)
    comments = list(comments)
    slot, comment_id = choose_history_slot(comments, snapshot.run)
    operations = [{
        "role": "history",
        "slot": slot,
        "comment_id": comment_id,
        "body": f"{history_marker(slot)}\n{snapshot.body}",
    }]
    if baseline_eligible(snapshot.summary):
        baseline = next((
            comment for comment in comments
            if str(comment.get("body") or "").lstrip().startswith(BASELINE_MARKER)
        ), None)
        if baseline is None:
            raise ValueError("Validation baseline slot is missing")
        operations.append({
            "role": "baseline",
            "comment_id": int(baseline["id"]),
            "body": f"{BASELINE_MARKER}\n{snapshot.body}",
        })
    return {"run": snapshot.run, "operations": operations}


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan bounded Issue #2 validation updates")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--comments", type=Path, required=True)
    args = parser.parse_args()
    with args.comments.open("r", encoding="utf-8-sig") as stream:
        comments = json.load(stream)
    if not isinstance(comments, list):
        raise ValueError("comments JSON must be a list")
    plan = build_upload_plan(args.root, args.sha, comments)
    print(json.dumps(plan, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
