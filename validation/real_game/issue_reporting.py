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
TRACE_DELTA_EVENTS = frozenset({"native_audio_observation", "alignment_inference", "hook_event"})
TRACE_ENCODING = "causal-delta-jsonl-v2"


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


def _trace_envelope(item: dict[str, Any]) -> dict[str, Any]:
    return {
        name: item[name]
        for name in ("event", "seq", "at_ns", "session", "line")
        if name in item
    }


def _trace_payload(item: dict[str, Any]) -> dict[str, Any]:
    envelope = {"event", "seq", "at_ns", "session", "line"}
    return {name: value for name, value in item.items() if name not in envelope}


def _normalize_trace_payload(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize repeated measurement collections into provenance-keyed facts."""
    normalized = dict(payload)
    if event != "native_audio_observation":
        return normalized
    series = normalized.pop("vocal_series", None)
    if not isinstance(series, list):
        if series is not None:
            normalized["vocal_series"] = series
        return normalized
    by_observer: dict[str, Any] = {}
    for entry in series:
        if not isinstance(entry, dict) or not isinstance(entry.get("observer"), str):
            normalized["vocal_series"] = series
            return normalized
        observer = entry["observer"]
        by_observer[observer] = {
            name: value for name, value in entry.items() if name != "observer"
        }
    normalized["vocal_by_observer"] = by_observer
    return normalized


def _deep_delta(
    current: dict[str, Any], previous: dict[str, Any], *, prefix: str = "",
) -> tuple[dict[str, Any], list[str]]:
    """Return a recursive merge delta plus explicit removed field paths."""
    changed: dict[str, Any] = {}
    removed: list[str] = []
    for name, value in current.items():
        path = f"{prefix}.{name}" if prefix else name
        if name not in previous:
            changed[name] = value
            continue
        old = previous[name]
        if isinstance(value, dict) and isinstance(old, dict):
            nested, nested_removed = _deep_delta(value, old, prefix=path)
            if nested:
                changed[name] = nested
            removed.extend(nested_removed)
        elif value != old:
            changed[name] = value
    for name in previous:
        if name not in current:
            removed.append(f"{prefix}.{name}" if prefix else name)
    return changed, removed


def _hook_signature_from_event(record: dict[str, Any]) -> tuple[Any, ...] | None:
    value = record.get("hook_event")
    if not isinstance(value, dict) or value.get("kind") != "output":
        return None
    source = value.get("source")
    if not isinstance(source, dict):
        return None
    identity = f"{source.get('hook_code', '')}|{source.get('context')}|{source.get('context2')}"
    return (
        value.get("host_generation"), value.get("connection_generation"),
        value.get("sequence"), value.get("received_ns"), value.get("process_id"),
        source.get("address"), source.get("context"), source.get("context2"),
        identity, value.get("text"),
    )


def _hook_signature_from_selection(record: dict[str, Any]) -> tuple[Any, ...] | None:
    if record.get("event") != "hook_output":
        return None
    return (
        record.get("generation"), record.get("connection"), record.get("hook_sequence"),
        record.get("received_ns"), record.get("pid"), record.get("address"),
        record.get("context"), record.get("context2"), record.get("source"), record.get("raw"),
    )


def _observation_index(records: list[dict[str, Any]]) -> dict[tuple[Any, Any, Any], list[int]]:
    result: dict[tuple[Any, Any, Any], list[int]] = {}
    for record in records:
        if record.get("event") != "native_audio_observation":
            continue
        key = (record.get("session"), record.get("line"), record.get("evidence_at_ms"))
        seq = record.get("seq")
        if isinstance(seq, int):
            result.setdefault(key, []).append(seq)
    return result


def _referenced_observation_seq(
    index: dict[tuple[Any, Any, Any], list[int]], record: dict[str, Any],
    evidence_at_ms: Any, *, before: bool,
) -> int | None:
    seq = record.get("seq")
    if not isinstance(seq, int):
        return None
    values = index.get((record.get("session"), record.get("line"), evidence_at_ms), ())
    candidates = [value for value in values if (value < seq if before else value > seq)]
    if not candidates:
        return None
    return max(candidates) if before else min(candidates)


def _compact_alignment_reference(
    record: dict[str, Any], observation_records: dict[int, dict[str, Any]],
    observation_index: dict[tuple[Any, Any, Any], list[int]],
) -> dict[str, Any]:
    """Keep inference work facts and reference equal product-observation results."""
    compact = dict(record)
    boundary = compact.get("capture_end_ms")
    observation_seq = _referenced_observation_seq(
        observation_index, compact, boundary, before=False)
    if observation_seq is None:
        return compact
    observation = observation_records.get(observation_seq)
    if observation is None:
        return compact
    compact["observation_seq"] = observation_seq
    if observation.get("evidence_at_ms") == boundary:
        compact.pop("capture_end_ms", None)
    for name in (
        "causal_lower_bound_ms", "input_candidates_ms", "target_attached_ms",
        "target_units", "aligned_target_units", "target_associated",
        "complete_alignment", "committed_target_end", "lexical_target_end_ms",
    ):
        if name in compact and observation.get(name) == compact[name]:
            compact.pop(name, None)
    return compact


def _compact_product_trace(content: str) -> tuple[str, str]:
    """Build a causal, analysis-complete Issue representation of product trace.

    The local JSONL remains the durable complete event stream.  The Issue copy
    preserves every upstream observation and one-time action while removing
    state heartbeats and repeated snapshot fields.  High-rate measurement
    records use recursive deltas, selected Hook outputs reference their upstream
    Hook event, and alignment work references the product observation carrying
    equal result facts.
    """
    raw_lines = content.splitlines()
    if not raw_lines:
        return content, "full"
    try:
        records = [json.loads(line) for line in raw_lines if line.strip()]
    except json.JSONDecodeError:
        return content, "full"
    if not records or not all(isinstance(item, dict) for item in records):
        return content, "full"
    if records[0].get("event") != "trace_started":
        return content, "full"

    hook_event_seqs: dict[tuple[Any, ...], list[int]] = {}
    for record in records:
        if record.get("event") != "hook_event":
            continue
        signature = _hook_signature_from_event(record)
        seq = record.get("seq")
        if signature is not None and isinstance(seq, int):
            hook_event_seqs.setdefault(signature, []).append(seq)

    observations = _observation_index(records)
    observation_records = {
        int(record["seq"]): record
        for record in records
        if record.get("event") == "native_audio_observation"
        and isinstance(record.get("seq"), int)
    }

    semantic_records: list[dict[str, Any]] = []
    progress_state: dict[tuple[Any, Any], str] = {}
    for index, source in enumerate(records):
        if index == 0:
            head = dict(source)
            head["issue_encoding"] = TRACE_ENCODING
            head["delta_events"] = sorted(TRACE_DELTA_EVENTS)
            head["normalized_fields"] = {
                "native_audio_observation.vocal_series": "vocal_by_observer"
            }
            head["causal_references"] = ["hook_output", "alignment_inference", "utterance_progress"]
            head["elided_repetition"] = ["utterance_progress_same_state"]
            semantic_records.append(head)
            continue

        record = dict(source)
        event = record.get("event")
        if event == "utterance_progress":
            key = (record.get("session"), record.get("line"))
            state = str(record.get("state") or "unknown")
            prior_state = progress_state.get(key, "unknown")
            progress_state[key] = state
            if state == prior_state:
                continue
            observation_seq = _referenced_observation_seq(
                observations, record, record.get("evidence_at_ms"), before=True)
            if observation_seq is not None:
                record["observation_seq"] = observation_seq
                record.pop("evidence_at_ms", None)
            semantic_records.append(record)
            continue

        if event == "hook_output":
            signature = _hook_signature_from_selection(record)
            matches = hook_event_seqs.get(signature, ()) if signature is not None else ()
            if len(matches) == 1:
                compact = _trace_envelope(record)
                compact["hook_event_seq"] = matches[0]
                semantic_records.append(compact)
                continue

        if event == "alignment_inference":
            record = _compact_alignment_reference(record, observation_records, observations)
        semantic_records.append(record)

    encoded: list[dict[str, Any]] = []
    previous: dict[tuple[Any, ...], tuple[Any, dict[str, Any]]] = {}
    for record in semantic_records:
        event = str(record.get("event"))
        if event not in TRACE_DELTA_EVENTS:
            encoded.append(record)
            continue

        if event == "hook_event":
            key = (event,)
        else:
            key = (event, record.get("session"), record.get("line"))
        payload = _normalize_trace_payload(event, _trace_payload(record))
        prior = previous.get(key)
        if prior is None:
            first = _trace_envelope(record)
            first.update(payload)
            encoded.append(first)
        else:
            prior_seq, prior_payload = prior
            delta, removed = _deep_delta(payload, prior_payload)
            compact = _trace_envelope(record)
            compact["delta_from_seq"] = prior_seq
            if removed:
                compact["delta_remove"] = removed
            compact.update(delta)
            encoded.append(compact)
        previous[key] = (record.get("seq"), payload)

    rendered = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in encoded
    )
    return rendered, TRACE_ENCODING


def _shared_representation(path: Path, content: str) -> tuple[str, str]:
    if path.name.endswith(".jsonl") and "fixture-evidence" not in path.name:
        return _compact_product_trace(content)
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
        "Product trace sharing uses `delta-jsonl-v1` for repeated acoustic snapshots: "
        "omitted payload fields inherit from `delta_from_seq`; `delta_remove` clears fields.",
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
