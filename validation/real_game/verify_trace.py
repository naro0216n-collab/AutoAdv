"""Validate evidence integrity without explaining product behavior.

The real-game runner is an evidence collector, not a second AutoAdv state
machine.  This module checks that the product trace and fixture stimulus record
are structurally readable and that the requested fixture sequence was reached.
It deliberately does not classify early input, overhold, misattribution,
playback state, safety, or product success.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
PHASES = ('hook', 'reading', 'accuracy', 'tts', 'mixed', 'stress')
CORE_PRODUCT_EVENTS = frozenset({
    'hook_output', 'utterance', 'utterance_progress', 'presentation_complete',
    'advance_attempt', 'advance_input', 'validation_recovery_input',
    'controller_handoff', 'native_audio_error', 'validation_driver_failure',
})


def _summary(values: list[float]) -> dict | None:
    """Small numeric summary shared by standalone alignment benchmarks."""
    if not values:
        return None
    ordered = sorted(values)
    def percentile(q: float) -> float:
        return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * q)))]
    return {
        'count': len(ordered),
        'p50': round(percentile(.50), 3),
        'p95': round(percentile(.95), 3),
        'max': round(ordered[-1], 3),
    }


def scenario_for_phase(phase: str, scenario_path: Path = HERE / 'SCENARIO.json') -> list[dict]:
    if phase == 'accuracy':
        scenario_path = HERE / 'ADVANCEMENT_ACCURACY_SCENARIO.json'
    elif phase == 'stress' and scenario_path == HERE / 'SCENARIO.json':
        scenario_path = HERE / 'ALIGNMENT_STRESS_SCENARIO.json'
    value = json.loads(scenario_path.read_text(encoding='utf-8'))
    lines = value.get('lines')
    if not isinstance(lines, list) or not all(isinstance(line, dict) for line in lines):
        raise ValueError(f'invalid scenario lines: {scenario_path}')
    return lines


def _sequence_errors(records: Sequence[dict], *, started_event: str, version: int) -> list[str]:
    if not records:
        return [f'no {started_event} evidence']
    first = records[0]
    errors = []
    if first.get('event') != started_event or first.get('version') != version:
        errors.append(f'invalid {started_event} header')
    previous = -1
    for record in records[1:]:
        seq = record.get('seq')
        if type(seq) is not int or seq <= previous:
            errors.append(f'non-monotonic {started_event} sequence')
            break
        previous = seq
        if type(record.get('at_ns')) is not int:
            errors.append(f'{started_event} record without monotonic timestamp')
            break
    return errors


def _core_timeline(records: Sequence[dict], fixture_records: Sequence[dict] | None) -> list[dict]:
    timeline: list[dict] = []
    for record in fixture_records or ():
        if record.get('event') != 'fixture_case':
            continue
        timeline.append({
            'stream': 'fixture', 'event': 'fixture_case', 'at_ns': record.get('at_ns'),
            'case': record.get('case'), 'text': record.get('text'),
            'voice_file': record.get('voice_file'),
        })
    progress_state: dict[tuple[object, object], tuple[object, ...]] = {}
    for record in records:
        event = record.get('event')
        if event not in CORE_PRODUCT_EVENTS:
            continue
        if event == 'utterance_progress':
            key = (record.get('session'), record.get('line'))
            signature = (
                record.get('state'), record.get('ended_at_ms'),
                record.get('lexical_target_associated'), record.get('lexical_target_ended'),
                record.get('finite_vocal_observed'), record.get('persistent_vocal_observed'),
                record.get('boundary_certainty'),
            )
            if progress_state.get(key) == signature:
                continue
            progress_state[key] = signature
        item = {'stream': 'product'}
        for key, value in record.items():
            if key in ('seq',):
                continue
            item[key] = value
        timeline.append(item)
    timeline.sort(key=lambda item: (item.get('at_ns') if type(item.get('at_ns')) is int else 0,
                                    0 if item.get('stream') == 'fixture' else 1))
    return timeline


def validate(records: Sequence[dict], *, phase: str = 'mixed',
             fixture_records: Sequence[dict] | None = None,
             requested_cases: int | None = None) -> dict:
    if phase not in PHASES:
        raise ValueError(f'invalid phase: {phase}')
    errors = _sequence_errors(records, started_event='trace_started', version=3)
    fixture_errors: list[str] = []
    if fixture_records is not None:
        fixture_errors = _sequence_errors(
            fixture_records, started_event='fixture_started', version=1)
        errors.extend(fixture_errors)

    target_record = next((r for r in records if r.get('event') == 'validation_collection_target'), None)
    if requested_cases is None and target_record is not None:
        value = target_record.get('dialogues')
        if type(value) is int and value >= 0:
            requested_cases = value

    fixture_cases = [r for r in (fixture_records or ()) if r.get('event') == 'fixture_case']
    observed_case_ids = [str(r.get('case') or '') for r in fixture_cases]
    expected_case_ids: list[str] = []
    if phase == 'accuracy':
        expected_case_ids = [str(line.get('id') or '') for line in scenario_for_phase('accuracy')]
        if requested_cases is None:
            requested_cases = len(expected_case_ids)
        prefix = observed_case_ids[:len(expected_case_ids)]
        if prefix and prefix != expected_case_ids[:len(prefix)]:
            errors.append('fixture case evidence does not follow authored accuracy sequence')

    if requested_cases is None:
        requested_cases = 0
    if fixture_records is not None:
        collection_complete = len(fixture_cases) >= requested_cases
    else:
        utterances = [r for r in records if r.get('event') == 'utterance']
        collection_complete = requested_cases == 0 or len(utterances) >= requested_cases

    structural_errors = list(errors)
    if not collection_complete:
        structural_errors.append(
            f'fixture collection incomplete: observed={len(fixture_cases) if fixture_records is not None else len([r for r in records if r.get("event") == "utterance"])} requested={requested_cases}')

    facts = {
        'requested_cases': requested_cases,
        'fixture_cases': len(fixture_cases) if fixture_records is not None else None,
        'hook_outputs': sum(r.get('event') == 'hook_output' for r in records),
        'utterances': sum(r.get('event') == 'utterance' for r in records),
        'presentation_completions': sum(r.get('event') == 'presentation_complete' for r in records),
        'advance_inputs': sum(r.get('event') == 'advance_input' for r in records),
        'validation_recovery_inputs': sum(r.get('event') == 'validation_recovery_input' for r in records),
        'driver_failures': sum(r.get('event') == 'validation_driver_failure' for r in records),
    }
    collection_valid = not errors
    return {
        'schema': 'autoadv-validation-evidence-v1',
        'ok': collection_valid and collection_complete,
        'phase': phase,
        'collection_valid': collection_valid,
        'measurement_valid': collection_valid,
        'collection_complete': collection_complete,
        'facts': facts,
        'fixture_case_ids': observed_case_ids,
        'errors': structural_errors,
        'timeline': _core_timeline(records, fixture_records),
    }


def _load_jsonl(path: Path) -> list[dict]:
    records = []
    for line_number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f'non-object JSONL record at {path}:{line_number}')
        records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description='Validate AutoAdv real-game evidence integrity')
    parser.add_argument('--trace', type=Path, required=True)
    parser.add_argument('--phase', choices=PHASES, required=True)
    parser.add_argument('--evidence', type=Path, help='fixture-authored stimulus evidence JSONL')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    if not args.trace.is_file():
        parser.error('real game trace not found')
    if args.report is not None and args.report.exists():
        parser.error('refusing to overwrite an existing report')
    try:
        records = _load_jsonl(args.trace)
        fixture_records = (_load_jsonl(args.evidence)
                           if args.evidence is not None and args.evidence.is_file() else None)
        result = validate(records, phase=args.phase, fixture_records=fixture_records)
        if args.report is not None:
            with args.report.open('x', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(f'invalid real-session evidence: {exc}')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
