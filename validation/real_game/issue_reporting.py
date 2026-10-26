"""Publish analysis-oriented Windows-validation evidence to a GitHub Issue.

The local run directory remains the durable, complete evidence store.  The Issue
surface carries the evidence needed for normal remote analysis plus conditional
runner/observer diagnostics, without duplicating every durable file on every
run.  Transport success never becomes a validation verdict.
"""
from __future__ import annotations

import argparse
import hashlib
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
# GitHub does not document a stable issue-comment body limit in the REST API.
# Keep each physical comment comfortably bounded, but never treat this as a
# limit on one validation run: a run is split across as many comments as needed.
COMMENT_BODY_TARGET_CHARS = 60_000
COMMENT_BODY_TARGET_BYTES = 60_000
COMMENT_METADATA_RESERVE_CHARS = 512
COMMENT_METADATA_RESERVE_BYTES = 1_024
ARCHIVED_MARKER = "<!-- autoadv-validation-archived -->"
PART_MARKER_RE = re.compile(
    r"<!--\s*autoadv-validation-part:(?P<run>\d{8}T\d{6}Z-[A-Za-z0-9_-]+):(?P<part>\d+)\s*-->"
)
DIAGNOSTIC_DIGEST_RE = re.compile(
    r"<!--\s*autoadv-validation-diagnostic:(?P<name>[A-Za-z0-9_.-]+):"
    r"(?P<digest>[0-9a-f]{64})\s*-->"
)
HUMAN_VOCAL_REPORT = "autoadv-human-vocal.result.json"
TRACE_ENCODING = "presentation-analysis-jsonl-v1"


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


def _needs_validation_log(summary: dict[str, Any]) -> bool:
    if summary.get("launcher_error"):
        return True
    for phase in summary.get("phases", ()):
        if not isinstance(phase, dict):
            continue
        if phase.get("runner_error") or phase.get("report_present") is False:
            return True
    return False


def _evidence_paths(root: Path, summary: dict[str, Any]) -> tuple[Path, ...]:
    """Return evidence that belongs on every analysis surface for this run.

    The transcript is a runner/setup diagnostic and is shared only when the
    launcher or a phase runner failed.  The isolated human-vocal report has its
    own digest/summary policy and is handled separately.
    """
    paths: list[Path] = []
    for path in root.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name == "validation-summary.json":
            paths.append(path)
        elif name == "validation.log" and _needs_validation_log(summary):
            paths.append(path)
        elif name == HUMAN_VOCAL_REPORT:
            continue
        elif name.startswith("autoadv-") and (
            name.endswith(".jsonl") or name.endswith(".result.json")
        ):
            paths.append(path)
    priority = {
        "validation-summary.json": 0,
        "validation.log": 4,
    }
    return tuple(sorted(paths, key=lambda path: (
        priority.get(path.name, 2 if path.name.endswith(".jsonl") else 3),
        path.name,
    )))


def _evidence_language(path: Path) -> str:
    if path.name.endswith(".jsonl"):
        return "jsonl"
    if path.name.endswith(".json"):
        return "json"
    return "text"




def _markdown_fence(content: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", content)), default=0)
    return "`" * max(3, longest + 1)


def _compact_product_trace(content: str) -> tuple[str, str]:
    """Keep causal events, summarize only cadence-level acoustic telemetry.

    The local JSONL is the complete trace.  The Issue copy keeps all low-rate
    product/runtime events and every Presentation, but replaces repeated native
    observations/alignment inferences with transition snapshots and factual
    per-Presentation rollups.  Remote size therefore follows actual state change
    rather than polling cadence.
    """
    try:
        records = [json.loads(line) for line in content.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return content, "full"
    if not records or not all(isinstance(record, dict) for record in records):
        return content, "full"
    if records[0].get("event") != "trace_started":
        return content, "full"

    header = dict(records[0])
    header["issue_encoding"] = TRACE_ENCODING
    header["source_record_count"] = len(records)
    header["analysis_columns"] = {
        "observation": ["seq", "evidence_at_ms", "aligned_units", "associated", "complete",
                        "committed", "target_start_ms", "aligned_end_ms", "target_end_ms",
                        "causal_lower_bound_ms", "input_candidates_ms", "target_attached_ms",
                        "vocal", "recurrence"],
        "vocal": ["frames", "active", "peak", "threshold", "support_start_ms", "support_end_ms"],
        "alignment_state": ["seq", "capture_end_ms", "rejection", "associated", "complete",
                            "committed", "reading_rank", "ambiguous_spans", "aligned_units",
                            "coverage", "path_margin", "boundary_margin"],
        "stats": ["count", "p50", "p95", "max"],
    }
    output: list[dict[str, Any]] = [header]
    observations: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    alignments: dict[tuple[Any, Any], list[dict[str, Any]]] = {}
    presentation_events: dict[tuple[Any, Any], list[list[Any]]] = {}
    last_progress_state: dict[tuple[Any, Any], str] = {}
    hook_sources: dict[tuple[Any, ...], int] = {}
    hook_source_rows: list[list[Any]] = []
    hook_output_rows: list[list[Any]] = []
    hook_lifecycle_rows: list[list[Any]] = []
    selected_hook_rows: list[list[Any]] = []

    def hook_source_id(hook: dict[str, Any]) -> int | None:
        source = hook.get("source") if isinstance(hook.get("source"), dict) else None
        if source is None:
            return None
        source_key = (
            hook.get("host_generation"), hook.get("connection_generation"),
            hook.get("process_id"), source.get("address"), source.get("context"),
            source.get("context2"), source.get("hook_code"), source.get("hook_name"),
        )
        source_id = hook_sources.get(source_key)
        if source_id is None:
            source_id = len(hook_source_rows) + 1
            hook_sources[source_key] = source_id
            hook_source_rows.append([source_id, *source_key])
        return source_id

    presentation_fields = {
        "utterance": ["source", "hook_sequence", "raw", "body", "speaker", "advance_mode",
                      "use_tts", "uncertain_text_fallback", "reading_units", "text_wait_ms"],
        "utterance_progress": ["state", "evidence_at_ms"],
        "advance_attempt": [],
        "audio_causal_lower_bound": ["at_ms", "after_line"],
        "advance_input": ["presentation_reason"],
        "presentation_complete": ["reason"],
        "uncertainty_fallback_due": ["text_wait_ms"],
        "game_input_candidates_attached": ["text_received_ns", "input_candidates_ms"],
        "game_input_observed": ["at_ms"],
        "tts_result": ["success", "reason"],
        "native_audio_error": ["reason"],
        "hook_group_flushed": [],
    }
    header["presentation_event_fields"] = presentation_fields

    def presentation_event(record: dict[str, Any]) -> list[Any]:
        event = str(record.get("event") or "")
        fields = presentation_fields.get(event)
        detail = {name: value for name, value in record.items()
                  if name not in {"event", "seq", "at_ns", "session", "line"}}
        if fields is None:
            return [event, record.get("seq"), record.get("at_ns"), detail]
        extra = {name: value for name, value in detail.items() if name not in fields}
        return [event, record.get("seq"), record.get("at_ns"),
                *[detail.get(name) for name in fields], extra or None]

    for record in records[1:]:
        event = record.get("event")
        key = (record.get("session"), record.get("line"))
        if event == "native_audio_observation":
            observations.setdefault(key, []).append(record)
            continue
        if event == "alignment_inference":
            alignments.setdefault(key, []).append(record)
            continue
        if event == "utterance_progress":
            state = str(record.get("state") or "unknown")
            prior = last_progress_state.get(key, "unknown")
            last_progress_state[key] = state
            if state == prior:
                continue
            presentation_events.setdefault(key, []).append(presentation_event(record))
            continue
        if event == "hook_output":
            selected_hook_rows.append([
                record.get("seq"), record.get("at_ns"), record.get("session"),
                record.get("generation"), record.get("connection"),
                record.get("hook_sequence"), record.get("received_ns"),
                record.get("pid"), record.get("source"),
            ])
            continue
        if event == "hook_event":
            hook = record.get("hook_event")
            if not isinstance(hook, dict):
                output.append(dict(record))
                continue
            source_id = hook_source_id(hook)
            outcome = record.get("outcome") if isinstance(record.get("outcome"), dict) else {}
            capture = outcome.get("capture") if isinstance(outcome.get("capture"), dict) else {}
            token = outcome.get("token") if isinstance(outcome.get("token"), dict) else {}
            common = [
                record.get("seq"), record.get("at_ns"), source_id, hook.get("sequence"),
                hook.get("received_ns"), outcome.get("connection_transition"),
                capture.get("changed"), capture.get("new_ambiguity"),
                capture.get("active_source_removed"), capture.get("monitor_started"),
                token.get("session"), token.get("line"), outcome.get("flush_delay_ms"),
            ]
            if hook.get("kind") == "output":
                hook_output_rows.append([*common, hook.get("text")])
            else:
                hook_lifecycle_rows.append([
                    *common, hook.get("kind"), hook.get("process_id"),
                    hook.get("text"), hook.get("detail"), outcome.get("note"),
                ])
            continue
        if key[0] is not None and key[1] is not None:
            presentation_events.setdefault(key, []).append(presentation_event(record))
        else:
            output.append(dict(record))

    if hook_output_rows or hook_lifecycle_rows or selected_hook_rows:
        output.append({
            "event": "hook_analysis",
            "source_columns": ["id", "generation", "connection", "pid", "address",
                               "context", "context2", "hook_code", "hook_name"],
            "common_columns": ["seq", "at_ns", "source_id", "hook_sequence", "received_ns",
                               "connection_transition", "capture_changed", "new_ambiguity",
                               "active_source_removed", "monitor_started", "token_session",
                               "token_line", "flush_delay_ms"],
            "output_suffix": ["text"],
            "lifecycle_suffix": ["kind", "pid", "text", "detail", "note"],
            "selected_columns": ["seq", "at_ns", "session", "generation", "connection",
                                 "hook_sequence", "received_ns", "pid", "source"],
            "sources": hook_source_rows, "outputs": hook_output_rows,
            "lifecycle": hook_lifecycle_rows, "selected": selected_hook_rows,
        })

    def stats(rows: list[dict[str, Any]], name: str) -> list[Any] | None:
        values = sorted(float(row[name]) for row in rows
                        if isinstance(row.get(name), (int, float)))
        if not values:
            return None
        pick = lambda q: round(values[min(len(values) - 1, int(round((len(values) - 1) * q)))], 3)
        return [len(values), pick(.5), pick(.95), round(values[-1], 3)]

    for key in dict.fromkeys((*observations.keys(), *alignments.keys(), *presentation_events.keys())):
        obs = observations.get(key, [])
        events = presentation_events.get(key, [])
        prog = [row for row in events if row[0] == "utterance_progress"]
        wanted: set[int] = set()
        if obs and isinstance(obs[-1].get("seq"), int):
            wanted.add(obs[-1]["seq"])
        for transition in prog:
            evidence_at_ms = transition[4]
            candidates = [row for row in obs
                          if row.get("evidence_at_ms") == evidence_at_ms
                          and isinstance(row.get("seq"), int)
                          and row["seq"] < (transition[1] if isinstance(transition[1], int) else -1)]
            if not candidates:
                candidates = [row for row in obs
                              if isinstance(row.get("seq"), int)
                              and row["seq"] < (transition[1] if isinstance(transition[1], int) else -1)]
            if candidates:
                wanted.add(candidates[-1]["seq"])

        sampled = []
        vocal_rollup: dict[str, list[Any]] = {}
        last_active: dict[str, int] = {}
        for row in obs:
            vocal = {}
            for item in row.get("vocal_series", ()) if isinstance(row.get("vocal_series"), list) else ():
                if not isinstance(item, dict) or not isinstance(item.get("observer"), str):
                    continue
                name = item["observer"]
                frames, active = item.get("frame_count"), item.get("active_count")
                prior_active = last_active.get(name, 0)
                if isinstance(active, int) and active > prior_active:
                    last_active[name] = active
                summary = vocal_rollup.setdefault(name, [0, 0, 0, 0.0, None])
                summary[0] += 1
                summary[1], summary[2] = frames, active
                if isinstance(item.get("peak_score"), (int, float)):
                    summary[3] = round(max(summary[3], float(item["peak_score"])), 6)
                if isinstance(active, int) and active > prior_active:
                    summary[4] = row.get("evidence_at_ms")
                vocal[name] = [frames, active, item.get("peak_score"), item.get("active_score_threshold"),
                               item.get("measurement_support_start_ms"), item.get("measurement_support_end_ms")]
            if row.get("seq") in wanted:
                sampled.append([
                    row.get("seq"), row.get("evidence_at_ms"), row.get("aligned_target_units"),
                    row.get("target_associated"), row.get("complete_alignment"),
                    row.get("committed_target_end"), row.get("lexical_target_start_ms"),
                    row.get("lexical_aligned_end_ms"), row.get("lexical_target_end_ms"),
                    row.get("causal_lower_bound_ms"), row.get("input_candidates_ms"),
                    row.get("target_attached_ms"), vocal,
                    [row.get("recurrence_strength"), row.get("recurrence_period_ms"),
                     row.get("recurrence_stability"), row.get("recent_recurrence_strength"),
                     row.get("recurrence_level_consistency")],
                ])

        recurrence_rows = [row for row in obs
                           if isinstance(row.get("recurrence_strength"), (int, float))]
        strongest = (max(recurrence_rows, key=lambda row: float(row.get("recurrence_strength") or 0.0))
                     if recurrence_rows else None)
        recurrence_rollup = ([
            len(recurrence_rows), strongest.get("recurrence_strength"), strongest.get("evidence_at_ms"),
            strongest.get("recurrence_period_ms"),
            max(float(row.get("recurrence_stability") or 0.0) for row in recurrence_rows),
            max(float(row.get("recent_recurrence_strength") or 0.0) for row in recurrence_rows),
            max(float(row.get("recurrence_level_consistency") or 0.0) for row in recurrence_rows),
        ] if strongest is not None else None)

        alignment_rows = alignments.get(key, [])
        state_rows, prior = [], None
        for row in alignment_rows:
            state = (row.get("rejection") or "", bool(row.get("target_associated")),
                     bool(row.get("complete_alignment")), bool(row.get("committed_target_end")),
                     row.get("reading_rank"), bool(row.get("ambiguous_alignment_spans")))
            if state == prior:
                continue
            prior = state
            state_rows.append([
                row.get("seq"), row.get("capture_end_ms"), *state,
                row.get("aligned_target_units"), row.get("target_coverage"),
                row.get("path_margin"), row.get("boundary_margin"),
            ])
        final = alignment_rows[-1] if alignment_rows else {}
        coverage_rows = [row for row in alignment_rows
                         if isinstance(row.get("target_coverage"), (int, float))]
        best_coverage = (max(coverage_rows, key=lambda row: float(row["target_coverage"]))
                         if coverage_rows else None)
        output.append({
            "event": "presentation_analysis", "session": key[0], "line": key[1],
            "events": events, "observation_count": len(obs), "observations": sampled, "vocal_rollup": vocal_rollup,
            "recurrence_rollup": recurrence_rollup,
            "alignment": {
                "count": len(alignment_rows), "queue_ms": stats(alignment_rows, "queue_ms"),
                "align_ms": stats(alignment_rows, "align_ms"),
                "audio_ms": stats(alignment_rows, "audio_ms"), "state_changes": state_rows,
                "max_coverage": [best_coverage.get("target_coverage"),
                                 best_coverage.get("capture_end_ms"),
                                 best_coverage.get("aligned_target_units")]
                if best_coverage is not None else None,
                "final": [final.get("capture_end_ms"), final.get("target_units"),
                          final.get("aligned_target_units"), final.get("target_coverage"),
                          final.get("lexical_target_end_ms"), final.get("path_margin"),
                          final.get("boundary_margin")],
            } if alignment_rows else None,
        })

    return "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for item in output), TRACE_ENCODING

def _shared_representation(path: Path, content: str) -> tuple[str, str]:
    if path.name.endswith(".jsonl") and "fixture-evidence" not in path.name:
        return _compact_product_trace(content)
    if path.name.endswith(".json"):
        try:
            value = json.loads(content)
            case_rows = value.get("case_results") if isinstance(value, dict) else None
            if (isinstance(value, dict)
                    and value.get("schema") == "autoadv-offline-verification-v1"
                    and isinstance(case_rows, list)
                    and all(isinstance(row, dict) for row in case_rows)):
                columns = list(case_rows[0]) if case_rows else []
                if all(list(row) == columns for row in case_rows):
                    value = dict(value)
                    value["case_result_columns"] = columns
                    value["case_results"] = [[row[name] for name in columns] for row in case_rows]
                    representation = "verifier-table-json"
                else:
                    representation = "compact-json"
            else:
                representation = "compact-json"
            return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
                    representation)
        except json.JSONDecodeError:
            pass
    return content, "full"


def _human_vocal_summary(value: dict[str, Any]) -> dict[str, Any]:
    files = value.get("files")
    rows = files if isinstance(files, list) else []
    observer_sources: set[str] = set()
    structurally_empty = not rows
    for row in rows:
        if not isinstance(row, dict):
            structurally_empty = True
            continue
        observers = row.get("observers")
        if not isinstance(observers, dict) or not observers:
            structurally_empty = True
            continue
        observer_sources.update(str(name) for name in observers)
    explicit_error = value.get("error") or value.get("errors") or value.get("diagnostic_error")
    return {
        "schema": value.get("schema"),
        "evidence_layer": value.get("evidence_layer"),
        "product_acceptance": value.get("product_acceptance"),
        "platform": value.get("platform"),
        "model": value.get("model"),
        "threshold_grid": value.get("threshold_grid"),
        "file_count": len(rows),
        "observer_sources": sorted(observer_sources),
        "collection_anomaly": bool(structurally_empty or explicit_error),
    }


def _known_diagnostic_digests(
    comments: Iterable[dict[str, Any]], name: str,
) -> set[str]:
    result: set[str] = set()
    for comment in comments:
        body = str(comment.get("body") or "")
        for match in DIAGNOSTIC_DIGEST_RE.finditer(body):
            if match.group("name") == name:
                result.add(match.group("digest"))
        # Preserve continuity with the previous manifest format, which already
        # published the sanitized SHA-256 but had no dedicated hidden marker.
        for line in body.splitlines():
            if name not in line:
                continue
            result.update(re.findall(r"`([0-9a-f]{64})`", line))
    return result

def _split_text(content: str, char_limit: int, byte_limit: int) -> list[str]:
    """Split text without dropping characters after sanitization.

    GitHub's stable comment-body limit is not part of this contract, so each
    chunk stays below both a character and UTF-8 byte budget.
    """
    if char_limit <= 0 or byte_limit <= 0:
        raise ValueError("invalid evidence chunk limit")
    if not content:
        return [""]
    chunks: list[str] = []
    cursor = 0
    while cursor < len(content):
        high = min(len(content), cursor + char_limit)
        low = cursor + 1
        stop = cursor
        while low <= high:
            middle = (low + high) // 2
            if len(content[cursor:middle].encode("utf-8")) <= byte_limit:
                stop = middle
                low = middle + 1
            else:
                high = middle - 1
        if stop <= cursor:
            raise ValueError("one validation evidence character exceeds the Issue byte budget")
        if stop < len(content):
            newline = content.rfind("\n", cursor, stop)
            if newline >= cursor:
                stop = newline + 1
        chunks.append(content[cursor:stop])
        cursor = stop
    return chunks


def _fits_payload(content: str) -> bool:
    return (
        len(content) <= COMMENT_BODY_TARGET_CHARS - COMMENT_METADATA_RESERVE_CHARS
        and len(content.encode("utf-8"))
        <= COMMENT_BODY_TARGET_BYTES - COMMENT_METADATA_RESERVE_BYTES
    )


def _evidence_blocks(
    root: Path,
    summary: dict[str, Any],
    known_human_vocal_digests: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Build the normal analysis surface plus conditional diagnostics."""
    manifest = [
        "", "**Shared analysis evidence**",
        "", "| file | source chars | shared chars | source sha256 (sanitized UTF-8) | representation |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    blocks: list[str] = []
    # Keep one block well below the physical comment budget so a file chunk can
    # always be wrapped with its label and Markdown delimiters.
    chunk_char_limit = COMMENT_BODY_TARGET_CHARS - COMMENT_METADATA_RESERVE_CHARS - 4_000
    chunk_byte_limit = COMMENT_BODY_TARGET_BYTES - COMMENT_METADATA_RESERVE_BYTES - 4_000
    for path in _evidence_paths(root, summary):
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
        content = sanitize_public_issue(raw)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        shared, representation = _shared_representation(path, content)
        manifest.append(
            f"| `{path.name}` | {len(content)} | {len(shared)} | `{digest}` | `{representation}` |"
        )
        chunks = _split_text(shared, chunk_char_limit, chunk_byte_limit)
        for index, chunk in enumerate(chunks, 1):
            label = path.name if len(chunks) == 1 else f"{path.name} · chunk {index}/{len(chunks)}"
            # Validation evidence is generated by this repository and does not
            # contain Markdown fences. Keep the raw textual payload directly
            # readable instead of base64/ZIP encoding it.
            fence = _markdown_fence(chunk)
            blocks.append("\n".join((
                f"<details open><summary>{label}</summary>", "",
                f"{fence}{_evidence_language(path)}",
                chunk,
                fence, "</details>",
            )))

    diagnostic_path = root / HUMAN_VOCAL_REPORT
    if diagnostic_path.is_file():
        raw = diagnostic_path.read_text(encoding="utf-8-sig", errors="replace")
        content = sanitize_public_issue(raw)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            value = {"error": "invalid diagnostic JSON"}
        if not isinstance(value, dict):
            value = {"error": "diagnostic root is not an object"}
        diagnostic_summary = _human_vocal_summary(value)
        summary_text = json.dumps(
            diagnostic_summary, ensure_ascii=False, indent=2, sort_keys=True,
        ) + "\n"
        known = known_human_vocal_digests or set()
        include_detail = digest not in known or bool(diagnostic_summary["collection_anomaly"])
        representation = "digest+summary+detail" if include_detail else "digest+summary"
        manifest.extend((
            f"<!-- autoadv-validation-diagnostic:{HUMAN_VOCAL_REPORT}:{digest} -->",
            f"| `{HUMAN_VOCAL_REPORT}` | {len(content)} | {len(summary_text) if not include_detail else len(summary_text) + len(content)} | `{digest}` | `{representation}` |",
        ))
        fence = _markdown_fence(summary_text)
        blocks.append("\n".join((
            f"<details open><summary>{HUMAN_VOCAL_REPORT} · summary</summary>", "",
            f"{fence}json", summary_text, fence, "</details>",
        )))
        if include_detail:
            chunks = _split_text(content, chunk_char_limit, chunk_byte_limit)
            for index, chunk in enumerate(chunks, 1):
                label = (f"{HUMAN_VOCAL_REPORT} · detail" if len(chunks) == 1 else
                         f"{HUMAN_VOCAL_REPORT} · detail chunk {index}/{len(chunks)}")
                fence = _markdown_fence(chunk)
                blocks.append("\n".join((
                    f"<details><summary>{label}</summary>", "",
                    f"{fence}json", chunk, fence, "</details>",
                )))
    return manifest, blocks


def _partition_blocks(prefix: list[str], blocks: list[str]) -> tuple[str, ...]:
    """Partition one logical run across comments without omitting evidence."""
    if not _fits_payload(""):
        raise ValueError("invalid Issue comment payload budget")
    parts: list[str] = []
    current = "\n".join(prefix)
    if not _fits_payload(current):
        raise ValueError("validation summary itself exceeds the Issue comment payload budget")
    for block in blocks:
        candidate = block if not current else current + "\n\n" + block
        if _fits_payload(candidate):
            current = candidate
            continue
        if current:
            parts.append(current)
        if not _fits_payload(block):
            raise ValueError("one validation evidence block exceeds the Issue comment payload budget")
        current = block
    if current or not parts:
        parts.append(current)
    return tuple(parts)


def format_snapshot(
    root: Path,
    sha: str,
    *,
    known_human_vocal_digests: set[str] | None = None,
) -> Snapshot:
    summary = _read_json(root / "validation-summary.json")
    if summary is None:
        raise ValueError("No verification summary exists to upload")
    run = root.name
    lines: list[str] = [
        f"### `{run}` · `{sha}`",
        f"Requested: `{summary.get('phase_requested', '-')}` · "
        f"{summary.get('started_utc', '-')} → {summary.get('ended_utc', '-')}",
        "",
        "| phase | fixture/requested | hook outputs | utterances | presentations | inputs | game input | recovery | report | error |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for phase in summary.get("phases", ()):
        facts = phase.get("facts") if isinstance(phase.get("facts"), dict) else {}
        lines.append(
            f"| {_cell(phase.get('phase'))} | "
            f"{_metric(facts.get('fixture_cases'))}/{_metric(facts.get('requested_cases'))} | "
            f"{_metric(facts.get('hook_outputs'))} | {_metric(facts.get('utterances'))} | "
            f"{_metric(facts.get('presentation_completions'))} | {_metric(facts.get('advance_inputs'))} | "
            f"{_metric(facts.get('validation_game_inputs'))} | "
            f"{_metric(facts.get('validation_recovery_inputs'))} | "
            f"{_metric(phase.get('report_present'))} | {_cell(phase.get('runner_error'))} |"
        )
    if summary.get("launcher_error"):
        lines.extend(("", f"Launcher error: `{_cell(summary.get('launcher_error'))}`"))

    report = _read_json(root / "autoadv-accuracy.result.json")
    if report is not None:
        verdict = report.get("verdict")
        if verdict:
            lines.extend(("", f"Offline verifier: `{_cell(verdict)}`"))
        issues = [str(item) for item in report.get("evidence_issues", ()) if str(item).strip()]
        if issues:
            lines.extend(("", "**Evidence issues**"))
            lines.extend(f"- {sanitize_public_issue(item)}" for item in issues[:12])

    lines.extend((
        "",
        "Product trace sharing uses `presentation-analysis-jsonl-v1`: all Presentations are retained, "
        "while cadence-level acoustic samples are summarized by transition evidence.",
    ))
    manifest, blocks = _evidence_blocks(
        root, summary, known_human_vocal_digests=known_human_vocal_digests,
    )
    lines.extend(manifest)
    return Snapshot(
        run=run,
        parts=_partition_blocks(lines, blocks),
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
    if (len(body) > COMMENT_BODY_TARGET_CHARS
            or len(body.encode("utf-8")) > COMMENT_BODY_TARGET_BYTES):
        raise ValueError(
            "Issue comment partition exceeds target size: "
            f"chars={len(body)} bytes={len(body.encode('utf-8'))}"
        )
    return body


def build_upload_plan(root: Path, sha: str,
                      comments: Iterable[dict[str, Any]]) -> dict[str, Any]:
    comments = list(comments)
    snapshot = format_snapshot(
        root,
        sha,
        known_human_vocal_digests=_known_diagnostic_digests(
            comments, HUMAN_VOCAL_REPORT,
        ),
    )
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
    body = str(comment.get("body") or "")
    expected_body = str(operation["body"])
    if body != expected_body:
        raise RuntimeError(
            f"GitHub verification changed or truncated the {operation['role']} comment body")
    role = operation["role"]
    if role == "archive":
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
