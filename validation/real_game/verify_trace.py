"""Independent, fail-closed verification of product GUI observations from a *real* game.

No runner, injection, simulated HOOK, audio source or input exists here. A
trace is evidence from one opt-in production run, never proof of execution when
manufactured for unit tests. Scenario expectations are not read by AutoAdv.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from typing import Sequence
from autoadv.validation_trace import text_digest

HERE = Path(__file__).resolve().parent
PHASES = ('hook', 'reading', 'native', 'tts', 'mixed')


def _summary(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        'count': len(ordered),
        'p50': round(ordered[(len(ordered) - 1) // 2], 3),
        'p95': round(ordered[math.ceil(len(ordered) * .95) - 1], 3),
        'max': round(ordered[-1], 3),
    }


def _elapsed_ms(first: int | None, last: int | None) -> float | None:
    if first is None or last is None or first > last:
        return None
    return (last - first) / 1_000_000


def _key(record: dict) -> tuple[object, object]:
    return record.get('session'), record.get('line')


def validate(records: Sequence[dict], *, phase: str = 'mixed',
             scenario_path: Path = HERE / 'SCENARIO.json',
             start_line: int | None = None) -> dict:
    if phase not in PHASES:
        raise ValueError(f'unknown validation phase: {phase}')
    scenario = json.loads(scenario_path.read_text(encoding='utf-8'))['lines']
    expected_hashes = [text_digest(x['text']) for x in scenario]
    errors: list[str] = []
    if (not records or records[0].get('event') != 'trace_started'
            or records[0].get('version') != 2):
        errors.append('missing version 2 real-session trace header')
    for index, (previous, current) in enumerate(zip(records, records[1:]), 1):
        if (type(current.get('seq')) is not int or current['seq'] != index or
                type(current.get('at_ns')) is not int or
                current['at_ns'] < previous.get('at_ns', 0)):
            errors.append(f'invalid event sequence or timestamp at record {index}')
            break

    # The active-session and provenance checks reject old-session, OCR and
    # unmatched candidate-HOOK events as evidence of a live Luna dialogue.
    connections: set[tuple] = set()
    monitoring: dict[object, dict] = {}
    source_outputs: dict[tuple, int] = {}
    utterances: dict[tuple, dict] = {}
    utterance_order: list[tuple] = []
    complete: set[tuple] = set()
    pcm: set[tuple] = set()
    native: set[tuple] = set()
    tts: set[tuple] = set()
    attempts: dict[tuple, int] = {}
    sent: dict[tuple, int] = {}
    timestamps: dict[tuple, int] = {}
    readiness: set[object] = set()
    audio_targets: dict[object, int] = {}
    permits: dict[object, int] = {}
    pauses: dict[object, int] = {}
    authorized: set[object] = set()
    resumed: set[object] = set()
    advanced_after_resumption: set[object] = set()
    pause_after_advance: set[object] = set()
    timeouts: set[tuple] = set()
    methods: dict[tuple, str] = {}
    presentation_at: dict[tuple, int] = {}
    audio_started_at: dict[tuple, int] = {}
    audio_completed_at: dict[tuple, int] = {}
    measured: dict[str, list[float]] = {
        name: [] for name in (
            'asr_decode_ms', 'asr_queue_ms', 'asr_rtf',
            'tts_synthesis_ms', 'tts_synthesis_rtf', 'tts_playback_ms',
        )
    }

    for record in records[1:]:
        event, session = record.get('event'), record.get('session')
        key = _key(record)
        at = record.get('at_ns', -1)
        if event == 'hook_connected':
            connections.add((record.get('pid'), record.get('generation'), record.get('connection')))
        elif event == 'monitor_started':
            if record.get('source') != 'hook':
                errors.append('real LunaHook acceptance cannot use OCR or other sources')
            connection = (record.get('hook_pid'), record.get('generation'), record.get('connection'))
            if connection not in connections:
                errors.append(f'monitor {session} lacks actual Host connected notification')
            if session in monitoring:
                errors.append(f'duplicate monitor session {session}')
            monitoring[session] = record
            authorized.discard(session)
        elif event == 'monitor_ended':
            monitoring.pop(session, None)
            authorized.discard(session)
        elif event == 'hook_output':
            monitor = monitoring.get(session)
            if (not monitor or record.get('pid') != monitor.get('hook_pid') or
                    record.get('generation') != monitor.get('generation') or
                    record.get('connection') != monitor.get('connection') or
                    not isinstance(record.get('source'), str) or
                    not isinstance(record.get('address'), int) or
                    record['address'] <= 0 or
                    not isinstance(record.get('hook_sequence'), int) or
                    not isinstance(record.get('received_ns'), int) or
                    record['received_ns'] <= 0 or record['received_ns'] > at):
                errors.append(f'invalid live Luna output source for session {session}')
            provenance = (session, record.get('source'), record.get('hook_sequence'), record.get('raw_hash'))
            if provenance in source_outputs:
                errors.append(f'duplicate HOOK output provenance: {provenance}')
            source_outputs[provenance] = record.get('received_ns', 0)
        elif event == 'utterance':
            if key in utterances:
                errors.append(f'duplicate utterance identity {key}')
            utterances[key] = record
            utterance_order.append(key)
            timestamps[key] = at
            if session not in monitoring:
                errors.append(f'utterance outside HOOK monitoring: {key}')
            source = (session, record.get('source'), record.get('hook_sequence'), record.get('raw_hash'))
            if source not in source_outputs:
                errors.append(f'utterance without actual selected Luna output: {key}')
        elif event == 'native_ready':
            if record.get('vad_only') is False and session in monitoring:
                readiness.add(session)
                audio_targets[session] = record.get('audio_pid')
        elif event == 'native_pcm':
            if (key not in utterances or type(record.get('frames')) is not int or record['frames'] <= 0
                    or record.get('audio_pid') != audio_targets.get(session)
                    or record.get('audio_pid') != monitoring.get(session, {}).get('audio_pid')):
                errors.append(f'PCM is not valid captured frames for {key}')
            pcm.add(key)
        elif event == 'asr_inference':
            if key in utterances and record.get('audio_ms', 0) > 0:
                duration = record.get('decode_ms')
                queue = record.get('queue_ms')
                if type(duration) in (int, float) and duration >= 0:
                    measured['asr_decode_ms'].append(duration)
                    measured['asr_rtf'].append(duration / record['audio_ms'])
                if type(queue) in (int, float) and queue >= 0:
                    measured['asr_queue_ms'].append(queue)
        elif event == 'tts_timing':
            if key in utterances and type(record.get('duration_ms')) in (int, float):
                duration = record['duration_ms']
                if duration >= 0:
                    if record.get('stage') == 'synthesis':
                        measured['tts_synthesis_ms'].append(duration)
                        if record.get('audio_ms', 0) > 0:
                            measured['tts_synthesis_rtf'].append(duration / record['audio_ms'])
                    elif record.get('stage') == 'playback':
                        measured['tts_playback_ms'].append(duration)
        elif event == 'native_event':
            if record.get('status') == 'started':
                audio_started_at.setdefault(key, at)
            if record.get('status') == 'completed':
                audio_completed_at[key] = at
                methods[key] = record.get('method', '')
                native.add(key)
        elif event == 'tts_result':
            if record.get('success') is True:
                tts.add(key)
                audio_completed_at[key] = at
        elif event == 'presentation_complete':
            if record.get('success') is True:
                complete.add(key)
                presentation_at[key] = at
        elif event == 'permit_requested':
            permits[session] = permits.get(session, 0) + 1
            if session in pause_after_advance:
                resumed.add(session)
            authorized.add(session)
        elif event == 'pause_requested':
            pauses[session] = pauses.get(session, 0) + 1
            if any(k[0] == session for k in sent):
                pause_after_advance.add(session)
            authorized.discard(session)
        elif event == 'advance_attempt':
            if key in attempts:
                errors.append(f'duplicate SendInput attempt: {key}')
            attempts[key] = at
        elif event == 'advance_input':
            if key not in attempts or attempts[key] >= at:
                errors.append(f'SendInput lacks a preceding real attempt: {key}')
            if key in sent:
                errors.append(f'double SendInput for one line: {key}')
            sent[key] = at
            if key not in utterances:
                errors.append(f'input without observed utterance: {key}')
            if key not in complete:
                errors.append(f'input before presentation completion: {key}')
            if session not in monitoring or session not in authorized:
                errors.append(f'input without active authorized game session: {key}')
            if session in resumed:
                advanced_after_resumption.add(session)
            u = utterances.get(key)
            if u:
                mode = u.get('mode')
                if mode == 'native' and key not in native:
                    errors.append(f'native SendInput before ASR speech completion: {key}')
                if mode == 'tts' and key not in tts:
                    errors.append(f'TTS SendInput before actual playback completion: {key}')
                if mode == 'auto' and key not in native | tts:
                    errors.append(f'AUTO SendInput before matched audio or TTS completion: {key}')
                if mode not in ('reading', 'native', 'tts', 'auto'):
                    errors.append(f'unknown dialogue presentation mode: {key}')
        elif event == 'next_line_timeout':
            timeouts.add(key)

    for key in native:
        if key[0] not in readiness or key not in pcm or methods.get(key) != 'asr':
            errors.append(f'native completion lacks real PCM and ASR evidence: {key}')

    hashes = [utterances[key].get('body_hash') for key in utterance_order]
    # A run may start from a known line in a larger game. It must not skip
    # intervening dialogues; no arbitrary subsequence matching is permitted.
    if start_line is not None:
        starts = [start_line - 1] if 1 <= start_line <= len(scenario) else []
    else:
        starts = [i for i in range(len(scenario)) if
                  hashes and expected_hashes[i:i + len(hashes)] == hashes]
    matches = [i for i in starts if expected_hashes[i:i + len(hashes)] == hashes]
    if len(matches) != 1:
        errors.append('HOOK dialogue does not match one unambiguous contiguous reference; specify --start-line for repeated text')
    start = matches[0] if len(matches) == 1 else None
    links: set[tuple] = set()
    if start is not None:
        for offset, key in enumerate(utterance_order):
            pos = start + offset
            if pos >= len(scenario):
                errors.append('dialogue exceeds reference scenario')
                break
            line = scenario[pos]
            if key not in sent:
                continue
            if line.get('manual_handoff'):
                errors.append(f'SendInput at manual-choice handoff instead of stopping: {key}')
                continue
            if pos == len(scenario) - 1 or line.get('ending'):
                continue
            if offset + 1 >= len(utterance_order):
                errors.append(f'SendInput did not produce a following HOOK utterance: {key}')
                continue
            subsequent = utterance_order[offset + 1]
            next_u = utterances[subsequent]
            next_origin = (subsequent[0], next_u.get('source'),
                           next_u.get('hook_sequence'), next_u.get('raw_hash'))
            if (subsequent[0] != key[0] or timestamps[subsequent] <= sent[key]
                    or source_outputs.get(next_origin, 0) <= attempts.get(key, sent[key])):
                errors.append(f'next HOOK utterance not after SendInput in same session: {key}')
            else:
                links.add(key)
            if key in timeouts:
                errors.append(f'next-line timeout despite expected continuation: {key}')
        # A new dialogue that appeared with no preceding AutoAdv input is not
        # credited as an automatic advance (except after the menu handoff).
        for offset in range(1, len(utterance_order)):
            previous = utterance_order[offset - 1]
            if (phase != 'hook' and previous not in sent and
                    not scenario[start + offset - 1].get('manual_handoff')):
                errors.append(f'next dialogue lacks preceding AutoAdv SendInput: {previous}')

    linked_modes = Counter(utterances[k].get('mode') for k in links)
    confirmed_native = [k for k in links if k in native and k in pcm]
    confirmed_tts = [k for k in links if k in tts and utterances[k].get('mode') in ('tts', 'auto')]
    bgm_native = bool(start is not None and any(
        scenario[start + i].get('bgm') and k in confirmed_native
        for i, k in enumerate(utterance_order)))
    repeats = [k for k in links if utterances[k].get('body_hash') == text_digest('ヒューズが飛んだ。')]
    if not utterance_order:
        errors.append('no live dialogue observed')
    if phase in ('reading', 'mixed') and linked_modes['reading'] < (1 if phase == 'mixed' else 2):
        errors.append('insufficient reading-time advances confirmed by next HOOK')
    if phase in ('native', 'mixed') and (len(confirmed_native) < (3 if phase == 'mixed' else 2) or not bgm_native):
        errors.append('insufficient ASR-confirmed native advances including BGM case')
    if phase in ('tts', 'mixed') and len(confirmed_tts) < 2:
        errors.append('insufficient Irodori playback-and-next-HOOK confirmations')
    if phase == 'mixed':
        if len(repeats) != 2:
            errors.append('two repeated-identical dialogues were not independently advanced')
        if not any(n >= 2 and s in advanced_after_resumption
                   for s, n in permits.items()):
            errors.append('manual pause, reauthorization and further real input not observed')
    # Measurements are observational, never an additional pass criterion.
    # The same monotonic clock times delivery, policy and actual advancement.
    durations = {name: values for name, values in measured.items()}
    for index, key in enumerate(utterance_order):
        u = utterances[key]
        origin = (key[0], u.get('source'), u.get('hook_sequence'), u.get('raw_hash'))
        if (ms := _elapsed_ms(source_outputs.get(origin), timestamps.get(key))) is not None:
            durations.setdefault('hook_to_utterance_ms', []).append(ms)
        if (ms := _elapsed_ms(timestamps.get(key), audio_started_at.get(key))) is not None:
            durations.setdefault('utterance_to_native_onset_ms', []).append(ms)
        if (ms := _elapsed_ms(timestamps.get(key), audio_completed_at.get(key))) is not None:
            durations.setdefault('utterance_to_audio_done_ms', []).append(ms)
        if (ms := _elapsed_ms(timestamps.get(key), presentation_at.get(key))) is not None:
            durations.setdefault('utterance_to_presentation_ms', []).append(ms)
        if (ms := _elapsed_ms(presentation_at.get(key), attempts.get(key))) is not None:
            durations.setdefault('presentation_to_input_ms', []).append(ms)
        if key in links:
            next_u = utterances[utterance_order[index + 1]]
            next_origin = (utterance_order[index + 1][0], next_u.get('source'),
                           next_u.get('hook_sequence'), next_u.get('raw_hash'))
            if (ms := _elapsed_ms(attempts.get(key), source_outputs.get(next_origin))) is not None:
                durations.setdefault('input_to_next_hook_ms', []).append(ms)
    performance = {name: stats for name, samples in durations.items()
                   if (stats := _summary(samples)) is not None}
    return {
        'performance': performance,
        'ok': not errors, 'phase': phase,
        'reference_start_line': start + 1 if start is not None else None,
        'observed_lines': len(utterance_order), 'confirmed_advance_to_next_hook': len(links),
        'confirmed_native_asr': len(confirmed_native), 'confirmed_tts': len(confirmed_tts),
        'errors': errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Verify a real AutoAdv GUI-to-game HOOK trace')
    parser.add_argument('--trace', type=Path, required=True, help='fresh AUTOADV_VALIDATION_TRACE JSONL')
    parser.add_argument('--phase', choices=PHASES, required=True,
                        help='hook / reading / native / tts / full mixed conversation')
    parser.add_argument('--start-line', type=int,
                        help='1-based first SCENARIO.json line, to disambiguate repeated text')
    args = parser.parse_args()
    if not args.trace.is_file():
        parser.error('real game trace not found')
    try:
        records = [json.loads(x) for x in args.trace.read_text(encoding='utf-8').splitlines() if x.strip()]
        result = validate(records, phase=args.phase, start_line=args.start_line)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(f'invalid real-session trace: {exc}')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
