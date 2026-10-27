"""Offline comparison of validation inputs against fixture-authored time facts.

The Windows runner and product trace are evidence sources only.  This module is
the sole validation-side owner of behavioral classification.  Its case verdicts
use only fixture case/voice timestamps and actual input timestamps; product
alignment, UtteranceProgress, pause reasons, completion reasons and other
runtime inference are retained as evidence but never reproduced here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

HERE = Path(__file__).resolve().parent
PHASES = ('hook', 'reading', 'accuracy', 'tts', 'mixed', 'stress')

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


def _sequence_issues(records: Sequence[dict], *, started_event: str, version: int) -> list[str]:
    if not records:
        return [f'no {started_event} evidence']
    first = records[0]
    issues = []
    if first.get('event') != started_event or first.get('version') != version:
        issues.append(f'invalid {started_event} header')
    previous = -1
    for record in records[1:]:
        seq = record.get('seq')
        if type(seq) is not int or seq <= previous:
            issues.append(f'non-monotonic {started_event} sequence')
            break
        previous = seq
        if type(record.get('at_ns')) is not int:
            issues.append(f'{started_event} record without monotonic timestamp')
            break
    return issues


def _endpoint_oracle_by_path() -> dict[str, dict]:
    """Map isolated authored voice paths to independent acoustic-end facts."""
    manifest = json.loads((HERE / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
    oracle = json.loads((HERE / 'ALIGNMENT_ENDPOINT_ORACLE.json').read_text(encoding='utf-8'))
    samples = oracle.get('samples', {})
    result: dict[str, dict] = {}
    for sample in manifest.get('samples', ()):
        sample_id = sample.get('id')
        path = sample.get('path')
        endpoint = samples.get(sample_id)
        if isinstance(path, str) and isinstance(endpoint, dict):
            result[path] = endpoint
    for path, endpoint in oracle.get('paths', {}).items():
        if isinstance(path, str) and isinstance(endpoint, dict):
            result[path] = endpoint
    return result


def _target_options(records: Sequence[dict]) -> dict:
    target = next((r for r in records if r.get('event') == 'validation_collection_target'), None)
    return target if isinstance(target, dict) else {}


def _successful_product_inputs(records: Sequence[dict], issues: list[str]) -> list[dict]:
    """Return successful send facts timestamped at their causal attempt edge."""
    pending: dict[tuple[object, object], list[dict]] = {}
    successful: list[dict] = []
    for record in records:
        event = record.get('event')
        key = (record.get('session'), record.get('line'))
        if event == 'advance_attempt' and type(record.get('at_ns')) is int:
            pending.setdefault(key, []).append(record)
        elif event == 'advance_input' and type(record.get('at_ns')) is int:
            attempts = pending.get(key) or []
            attempt = attempts.pop(0) if attempts else None
            if attempt is None:
                issues.append(f'advance_input without matching advance_attempt: session={key[0]} line={key[1]}')
            successful.append({
                'at_ns': attempt.get('at_ns') if attempt is not None else record.get('at_ns'),
                'confirmed_at_ns': record.get('at_ns'),
                'session': key[0],
                'line': key[1],
            })
    return successful


def _accuracy_case_results(records: Sequence[dict], fixture_records: Sequence[dict],
                           expected: Sequence[dict], issues: list[str]) -> list[dict]:
    cases = [r for r in fixture_records if r.get('event') == 'fixture_case']
    expected_ids = [str(line.get('id') or '') for line in expected]
    first_pass = cases[:len(expected_ids)]
    observed_ids = [str(row.get('case') or '') for row in first_pass]
    if observed_ids != expected_ids[:len(observed_ids)]:
        issues.append('fixture case evidence does not follow authored accuracy sequence')
    if len(cases) > len(expected_ids):
        issues.append(f'fixture emitted additional case records after authored pass: {len(cases) - len(expected_ids)}')

    voice_rows = [r for r in fixture_records if r.get('event') == 'fixture_voice_commanded']
    product_inputs = _successful_product_inputs(records, issues)
    authored_inputs = [r for r in records if r.get('event') == 'validation_game_input' and type(r.get('at_ns')) is int]
    recoveries = [r for r in records if r.get('event') == 'validation_recovery_input' and type(r.get('at_ns')) is int]
    oracle = _endpoint_oracle_by_path()
    trace_times = [r.get('at_ns') for r in records if type(r.get('at_ns')) is int]
    trace_end = max(trace_times) if trace_times else None
    options = _target_options(records)
    final_observation_seconds = options.get('final_observation_seconds')
    if not isinstance(final_observation_seconds, (int, float)):
        final_observation_seconds = None

    results: list[dict] = []
    for index, case in enumerate(first_pass):
        case_id = str(case.get('case') or '')
        authored = expected[index] if index < len(expected) else {}
        authored_voice_expected = bool(authored.get('voice_file'))
        audio_facts = authored.get('audio_facts') if isinstance(authored.get('audio_facts'), dict) else {}
        start_ns = case.get('at_ns')
        if type(start_ns) is not int:
            issues.append(f'fixture case without timestamp: {case_id}')
            continue
        next_ns = None
        if index + 1 < len(first_pass):
            candidate = first_pass[index + 1].get('at_ns')
            next_ns = candidate if type(candidate) is int else None
        elif len(cases) > len(first_pass):
            # A restarted fixture is not part of the authored pass, but its first
            # case timestamp is still a factual upper bound for the final case.
            candidate = cases[len(first_pass)].get('at_ns')
            next_ns = candidate if type(candidate) is int else None

        def in_case_window(row: dict) -> bool:
            at_ns = row.get('at_ns')
            # Windows timers can quantize the successful send and the fixture's
            # reaction to the same tick. A boundary-equal input belongs to the
            # earlier case because that input causally exposed the next case.
            return type(at_ns) is int and at_ns >= start_ns and (next_ns is None or at_ns <= next_ns)

        # Assign each successful product input once, to the earliest authored case
        # whose causal time window contains it. This uses no product state.
        case_inputs = []
        for row in product_inputs:
            if row.get('_assigned'):
                continue
            if in_case_window(row):
                row['_assigned'] = True
                case_inputs.append(row)

        case_authored_inputs = [
            row for row in authored_inputs
            if row.get('case') == case_id and in_case_window(row)
        ]
        case_recoveries = [
            row for row in recoveries
            if row.get('case') == case_id and in_case_window(row)
        ]
        voice = next((row for row in voice_rows
                      if row.get('case') == case_id and in_case_window(row)), None)

        advance_ns = case_inputs[0].get('at_ns') if case_inputs else None
        authored_input_ns = case_authored_inputs[0].get('at_ns') if case_authored_inputs else None
        endpoint = oracle.get(str(case.get('voice_file') or ''))
        earliest_end_ns = latest_end_ns = None
        early_input = None
        endpoint_status = 'not_applicable' if not authored_voice_expected else 'unmeasured'
        if voice is not None and endpoint is not None and type(voice.get('at_ns')) is int:
            speech_end_ms = float(endpoint['speech_end_ms'])
            uncertainty_ms = float(endpoint['uncertainty_ms'])
            earliest_end_ns = int(voice['at_ns'] + max(0.0, speech_end_ms - uncertainty_ms) * 1_000_000)
            latest_end_ns = int(voice['at_ns'] + (speech_end_ms + uncertainty_ms) * 1_000_000)
            endpoint_status = 'measured'
            if advance_ns is not None:
                early_input = advance_ns < earliest_end_ns

        stopped = bool(case_recoveries)
        if (not stopped and advance_ns is None and next_ns is None and trace_end is not None
                and final_observation_seconds is not None):
            stopped = trace_end - start_ns >= int(final_observation_seconds * 1_000_000_000)

        if stopped:
            outcome = 'stopped'
        elif len(case_inputs) > 1:
            outcome = 'multiple_inputs'
        elif (advance_ns is not None and authored_input_ns is not None
              and advance_ns >= authored_input_ns):
            outcome = 'competing_product_input'
        elif early_input is True:
            outcome = 'early_input'
        elif advance_ns is not None and endpoint_status == 'unmeasured':
            outcome = 'unmeasured_endpoint'
        elif advance_ns is not None:
            outcome = 'advanced'
        elif authored_input_ns is not None and next_ns is not None:
            outcome = 'authored_input'
        elif next_ns is not None:
            outcome = 'unattributed_advance'
        else:
            outcome = 'unresolved'

        results.append({
            'case': case_id,
            'fixture_started_at_ns': start_ns,
            'voice_commanded_at_ns': voice.get('at_ns') if voice is not None else None,
            'voice_duration_ms': voice.get('duration_ms') if voice is not None else None,
            'product_input_at_ns': advance_ns,
            'product_input_confirmed_at_ns': case_inputs[0].get('confirmed_at_ns') if case_inputs else None,
            'product_input_count': len(case_inputs),
            'authored_game_input_at_ns': authored_input_ns,
            'authored_game_input_count': len(case_authored_inputs),
            'recovery_input_at_ns': case_recoveries[0].get('at_ns') if case_recoveries else None,
            'audio_facts': dict(audio_facts),
            'authored_voice_endpoint_status': endpoint_status,
            'known_authored_voice_end_earliest_ns': earliest_end_ns,
            'known_authored_voice_end_latest_ns': latest_end_ns,
            'early_input': early_input,
            'stopped': stopped,
            'outcome': outcome,
        })
    return results


def verify(records: Sequence[dict], *, phase: str = 'mixed',
           fixture_records: Sequence[dict] | None = None) -> dict:
    if phase not in PHASES:
        raise ValueError(f'invalid phase: {phase}')

    issues = _sequence_issues(records, started_event='trace_started', version=3)
    if fixture_records is not None:
        issues.extend(_sequence_issues(fixture_records, started_event='fixture_started', version=1))

    target = _target_options(records)
    requested_cases = target.get('dialogues') if type(target.get('dialogues')) is int else None
    fixture_cases = [r for r in (fixture_records or ()) if r.get('event') == 'fixture_case']
    if (fixture_records is not None and requested_cases is not None
            and len(fixture_cases) < requested_cases):
        issues.append(
            f'fixture cases not observed: observed={len(fixture_cases)} requested={requested_cases}')

    facts = {
        'requested_cases': requested_cases,
        'fixture_cases': len(fixture_cases) if fixture_records is not None else None,
        'fixture_voice_commands': sum(r.get('event') == 'fixture_voice_commanded' for r in (fixture_records or ())),
        'hook_outputs': sum(r.get('event') == 'hook_output' for r in records),
        'utterances': sum(r.get('event') == 'utterance' for r in records),
        'presentation_completions': sum(r.get('event') == 'presentation_complete' for r in records),
        'advance_inputs': sum(r.get('event') == 'advance_input' for r in records),
        'validation_game_inputs': sum(r.get('event') == 'validation_game_input' for r in records),
        'game_input_observations': sum(
            r.get('event') == 'game_input_observed' for r in records),
        'external_input_candidate_attachments': sum(
            r.get('event') == 'game_input_candidates_attached' for r in records),
        'external_input_candidates': sum(
            len(r.get('input_candidates_ms', ()))
            for r in records
            if r.get('event') == 'game_input_candidates_attached'
            and isinstance(r.get('input_candidates_ms'), list)),
        'auto_input_causal_bounds': sum(
            r.get('event') == 'audio_causal_lower_bound' for r in records),
        'validation_recovery_inputs': sum(r.get('event') == 'validation_recovery_input' for r in records),
        'driver_failures': sum(r.get('event') == 'validation_driver_failure' for r in records),
    }

    case_results: list[dict] = []
    if phase == 'accuracy':
        if facts['game_input_observations'] < facts['validation_game_inputs']:
            issues.append(
                'authored validation input was not observed through the product input observer: '
                f"stimuli={facts['validation_game_inputs']} observed={facts['game_input_observations']}")
        if fixture_records is None:
            issues.append('accuracy verification requires fixture evidence')
        else:
            expected = scenario_for_phase('accuracy')
            if requested_cases is None:
                requested_cases = len(expected)
                facts['requested_cases'] = requested_cases
            case_results = _accuracy_case_results(
                records, fixture_records, expected, issues)

    failures = [row for row in case_results
                if row.get('outcome') in {
                    'early_input', 'stopped', 'multiple_inputs',
                    'competing_product_input', 'unattributed_advance'}]
    unresolved = [row for row in case_results
                  if row.get('outcome') in {'unresolved', 'unmeasured_endpoint'}]
    if phase == 'accuracy':
        facts['authored_voice_endpoint_measured'] = sum(
            row.get('authored_voice_endpoint_status') == 'measured' for row in case_results)
        facts['authored_voice_endpoint_unmeasured'] = sum(
            row.get('authored_voice_endpoint_status') == 'unmeasured' for row in case_results)
    verdict = 'failure_observed' if failures else ('incomplete' if unresolved or issues else 'no_failure_observed')

    return {
        'schema': 'autoadv-offline-verification-v2',
        'phase': phase,
        'verdict': verdict,
        'facts': facts,
        'case_results': case_results,
        'evidence_issues': issues,
    }


# Kept as the public Python entry used by local contract tests and helper tools.
validate = verify


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
    parser = argparse.ArgumentParser(description='Compare AutoAdv inputs with fixture time facts')
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
        result = verify(records, phase=args.phase, fixture_records=fixture_records)
        if args.report is not None:
            with args.report.open('x', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        parser.error(f'invalid real-session evidence: {exc}')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Behavioral failures are report data, not runner failures.  Non-zero exit
    # is reserved for malformed/unreadable evidence handled above by argparse.
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
