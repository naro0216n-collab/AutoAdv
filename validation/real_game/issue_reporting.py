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


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan bounded Issue #2 validation updates")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--comments", type=Path, required=True)
    args = parser.parse_args()
    with args.comments.open("r", encoding="utf-8-sig") as stream:
        comments = _flatten_comments(json.load(stream))
    plan = build_upload_plan(args.root, args.sha, comments)
    print(json.dumps(plan, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
