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
PHASES = ('hook', 'reading', 'native', 'tts', 'mixed', 'stress')
# The independent voice-channel observer is needed to judge *native* early
# advances, not to prove that LunaHook received text or TTS drained.
VOICE_ORACLE_PHASES = frozenset(('native', 'mixed', 'stress'))


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


def _signed_elapsed_ms(first: int | None, last: int | None) -> float | None:
    if first is None or last is None:
        return None
    return (last - first) / 1_000_000


def _key(record: dict) -> tuple[object, object]:
    return record.get('session'), record.get('line')


def audit_game_oracle(oracle_records: Sequence[dict] | None,
                      utterance_order: Sequence[tuple], utterances: dict,
                      attempts: dict, sent: dict, reasons: dict,
                      reference_start: int | None, scenario: Sequence[dict], *,
                      game_pid_by_session: dict, presentation_at: dict,
                      vad_end_estimates: dict, source_outputs: dict,
                      max_voice_hold_ms: int = 2000) -> dict:
    """Compare actual game playback/dismissal with actual AutoAdv inputs.

    The Ren'Py callback/polling stream is independent of the product. It is
    observational, not an alternate source of audio or input for AutoAdv.
    Game audio polling provides an *interval* for playback end, not sample-
    precise Windows device-drain ground truth; uncertainty must remain visible.
    """
    errors: list[str] = []
    result = {'errors': errors, 'latencies': {}, 'lines': [], 'early_inputs': 0,
              'unverified_voice_ends': 0, 'overholds': 0, 'misattributions': 0,
              'manual_or_unadvanced': 0, 'manual_or_external_advances': 0,
              'undismissed_lines': 0, 'confirmed_voice_safe_inputs': 0}
    if not oracle_records or oracle_records[0].get('event') != 'oracle_started' or oracle_records[0].get('version') != 1:
        errors.append('missing independent game-side playback/dismissal evidence')
        return result
    pid = oracle_records[0].get('pid')
    if type(pid) is not int or pid <= 0 or any(
            game_pid_by_session.get(key[0]) != pid for key in utterance_order):
        errors.append('game oracle PID does not match the selected input/window process')
        return result
    last_at = -1
    lines: dict[int, dict] = {}
    ordered = []
    for record in oracle_records:
        at = record.get('at_ns')
        if (type(at) is not int or at < last_at or record.get('pid') != pid):
            errors.append('inconsistent independent game clock or process')
            return result
        last_at = at
        event, number = record.get('event'), record.get('line')
        if event == 'line_started':
            if type(number) is not int or number <= 0 or number in lines:
                errors.append('invalid independent game line sequence')
                return result
            lines[number] = {'start': record, 'voice_onsets': [], 'voice_ends': [],
                             'dismissed': None, 'interrupted': False}
            ordered.append(number)
        elif event in ('voice_started', 'voice_ended', 'voice_interrupted', 'line_dismissed'):
            if number not in lines:
                errors.append(f'game event without a line: {number}')
                return result
            line = lines[number]
            if at < line['start']['at_ns']:
                errors.append(f'game event predates line: {number}')
            if event == 'voice_started':
                line['voice_onsets'].append(record)
            elif event == 'voice_ended':
                if (type(record.get('last_active_at_ns')) is not int or
                        record['last_active_at_ns'] > at):
                    errors.append(f'bad game playback endpoint interval: {number}')
                line['voice_ends'].append(record)
            elif event == 'voice_interrupted':
                line['interrupted'] = True
            else:
                if line['dismissed'] is not None:
                    errors.append(f'duplicate game line dismissal: {number}')
                line['dismissed'] = record
    if reference_start is None or not utterance_order:
        errors.append('cannot align game oracle without contiguous HOOK dialogue')
        return result
    if len(utterance_order) > len(ordered):
        errors.append('fewer game dialogues than product utterances')
        return result
    candidates = []
    for first in range(len(ordered) - len(utterance_order) + 1):
        segment = ordered[first:first + len(utterance_order)]
        if all(lines[n]['start'].get('body_hash') == utterances[key].get('body_hash')
               and lines[n]['start']['at_ns'] <= utterances[key]['at_ns']
               and (j + first + 1 == len(ordered) or
                    utterances[key]['at_ns'] < lines[ordered[first + j + 1]]['start']['at_ns'])
               for j, (n, key) in enumerate(zip(segment, utterance_order))):
            candidates.append(segment)
    if len(candidates) != 1:
        errors.append(f'no unique time-aligned game playback trace ({len(candidates)} candidates)')
        return result
    matched = candidates[0]
    for offset, (game_number, key) in enumerate(zip(matched, utterance_order)):
        line = lines[game_number]
        contract = scenario[reference_start + offset]
        want_voice = contract.get('foreground_voice')
        if type(want_voice) is not bool:
            errors.append(f'foreground_voice not specified in independent scenario: {contract.get("id")}')
            continue
        attempt = attempts.get(key)
        strict_native = (utterances[key].get('advance_mode') == 'audio' and
                         not utterances[key].get('use_tts'))
        dismissal = line['dismissed']
        voice_onsets = line['voice_onsets']
        voice_ends = line['voice_ends']
        origin = (key[0], utterances[key].get('source'),
                  utterances[key].get('hook_sequence'), utterances[key].get('raw_hash'))
        if voice_onsets:
            if (ms := _signed_elapsed_ms(voice_onsets[0]['at_ns'],
                                         source_outputs.get(origin))) is not None:
                result['latencies'].setdefault('game_voice_onset_to_hook_ms', []).append(ms)
        detail = {'scenario': contract.get('id'), 'game_line': game_number,
                  'completion_reason': reasons.get(key), 'sent': key in sent,
                  'voice_expected': want_voice, 'voice_observed': bool(voice_onsets),
                  'outcome': 'unadvanced'}
        if attempt is not None and key not in sent:
            errors.append(f'game progressed without confirmed AutoAdv SendInput: {key}')
        if attempt is not None and not dismissal:
            errors.append(f'AutoAdv sent but game did not dismiss dialogue: {key}')
        elif attempt is not None and dismissal:
            if not (attempt <= dismissal['at_ns'] and
                    (offset + 1 == len(matched) or dismissal['at_ns'] <= lines[matched[offset + 1]]['start']['at_ns'])):
                errors.append(f'game dismissal not causally after input attempt: {key}')
            ms = _signed_elapsed_ms(attempt, dismissal['at_ns'])
            if ms is not None:
                result['latencies'].setdefault('input_to_game_dismiss_ms', []).append(ms)
        expected_file = contract.get('foreground_file')
        if want_voice and (not isinstance(expected_file, str) or not expected_file):
            errors.append(f'expected foreground audio file unspecified: {key}')
        if want_voice and any(not isinstance(e.get('file'), str) or
                              not e['file'].replace('\\', '/').endswith(expected_file or '!!')
                              for e in voice_onsets):
            errors.append(f'wrong game voice channel source: {key}')
        if want_voice and not voice_onsets:
            errors.append(f'foreground game voice was never observed: {key}')
        if not want_voice and voice_onsets:
            errors.append(f'unexpected foreground voice in game: {key}')
        if line['interrupted']:
            errors.append(f'game voice replaced before verified end: {key}')
        if not want_voice and reasons.get(key) == 'native':
            result['misattributions'] += 1
            detail['outcome'] = 'background_voice_misattributed'
            errors.append(f'native completion without foreground game voice: {key}')
        elif attempt is None:
            result['manual_or_unadvanced'] += 1
            if dismissal is not None:
                # The game moved without AutoAdv input. That can be an intended
                # manual choice or another external input, but never an AutoAdv
                # success and never equivalent to leaving the dialogue open.
                result['manual_or_external_advances'] += 1
                detail['outcome'] = 'game_dismissed_without_autoadv_input'
            else:
                result['undismissed_lines'] += 1
                detail['outcome'] = 'still_presented_without_autoadv_input'
        elif want_voice and strict_native:
            # A decoder match cannot stand in for observing real voice end.
            # A playback end sampled only after sending the input is too late.
            # Preserve signed negative latency when an input beats playback
            # end; never discard these observations as invalid durations.
            end = next((e for e in voice_ends if e['at_ns'] <= attempt), None)
            if voice_ends:
                observed = voice_ends[0]
                ms = _signed_elapsed_ms(observed['at_ns'], attempt)
                result['latencies'].setdefault('game_voice_end_to_input_ms', []).append(ms)
                detail['game_end_to_input_ms'] = ms
                if (decision := presentation_at.get(key)) is not None:
                    result['latencies'].setdefault('game_voice_end_to_decision_ms', []).append(
                        _signed_elapsed_ms(observed['at_ns'], decision))
                if (estimated := vad_end_estimates.get(key)) is not None:
                    result['latencies'].setdefault('game_voice_end_to_vad_estimate_ms', []).append(
                        _signed_elapsed_ms(observed['at_ns'], estimated))
            active_at_dismiss = bool(dismissal and dismissal.get('voice_active'))
            if active_at_dismiss or (voice_ends and attempt < voice_ends[0]['last_active_at_ns']):
                result['early_inputs'] += 1
                detail['outcome'] = 'early_input'
                errors.append(f'input while the game voice was still playing: {key}')
            elif not end:
                result['unverified_voice_ends'] += 1
                detail['outcome'] = 'voice_end_unverified'
                errors.append(f'no independent voice-end observation before input: {key}')
            else:
                ms = detail['game_end_to_input_ms']
                if ms is not None and ms > max_voice_hold_ms:
                    result['overholds'] += 1
                    detail['outcome'] = 'overheld'
                    errors.append(f'input held {ms:.0f}ms after game voice end: {key}')
                else:
                    detail['outcome'] = 'voice_end_confirmed_then_input'
                    result['confirmed_voice_safe_inputs'] += 1
        else:
            detail['outcome'] = ('native_voice_not_required_by_text_or_tts_policy'
                                 if want_voice else 'input_without_foreground_voice')
        result['lines'].append(detail)
    return result


def validate(records: Sequence[dict], *, phase: str = 'mixed',
             scenario_path: Path = HERE / 'SCENARIO.json',
             start_line: int | None = None,
             oracle_records: Sequence[dict] | None = None,
             max_voice_hold_ms: int = 2000) -> dict:
    if phase not in PHASES:
        raise ValueError(f'unknown validation phase: {phase}')
    if phase == 'stress' and scenario_path == HERE / 'SCENARIO.json':
        scenario_path = HERE / 'ASR_STRESS_SCENARIO.json'
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
    game_pid_by_session: dict[object, int] = {}
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
    reasons: dict[tuple, str] = {}
    presentation_at: dict[tuple, int] = {}
    audio_started_at: dict[tuple, int] = {}
    audio_completed_at: dict[tuple, int] = {}
    vad_end_estimates: dict[tuple, int] = {}
    vad_onset_estimates: dict[tuple, int] = {}
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
            # Hook, audio and UI PIDs may legitimately differ for a game.
            # The independent game observer identifies the input/window PID;
            # capture evidence is checked against its separately selected PID.
            game_pid_by_session[session] = record.get('input_pid')
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
        elif event == 'native_voice_end_estimate':
            estimate, onset = record.get('estimated_at_ns'), record.get('onset_estimated_at_ns')
            # These are process-loopback/VAD estimates, not known game source
            # playback endpoints. Bounds are checked, but not against HOOK:
            # the voice may have started and ended before the HOOK arrived.
            if (key not in utterances or type(estimate) is not int or
                    type(onset) is not int or onset > estimate or
                    estimate > at or estimate < 0 or onset < 0 or
                    record.get('evidence') not in ('asr_vad', 'vad_only')):
                errors.append(f'invalid native VAD endpoint evidence: {key}')
            elif key in vad_end_estimates:
                errors.append(f'duplicate native VAD endpoint: {key}')
            else:
                vad_end_estimates[key] = estimate
                vad_onset_estimates[key] = onset
        elif event == 'tts_result':
            if record.get('success') is True:
                tts.add(key)
                audio_completed_at[key] = at
        elif event == 'presentation_complete':
            if record.get('success') is True:
                complete.add(key)
                presentation_at[key] = at
                reasons[key] = record.get('reason', '')
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
                mode, uses_tts = u.get('advance_mode'), u.get('use_tts')
                completed = next((r for r in records[1:] if r.get('event') == 'presentation_complete' and _key(r) == key), None)
                completion_reason = completed.get('reason') if completed else None
                if mode not in ('text', 'audio') or type(uses_tts) is not bool:
                    errors.append(f'invalid two-mode Utterance policy: {key}')
                elif mode == 'text':
                    expected_reason = 'tts_and_text' if uses_tts else 'text'
                    if completion_reason != expected_reason:
                        errors.append(f'text-mode completion contract not observed: {key}')
                    if uses_tts and key not in tts:
                        errors.append(f'TTS SendInput before actual playback completion: {key}')
                    wait = u.get('text_wait_ms')
                    if (type(wait) is not int or wait <= 0 or not completed or
                            completed['at_ns'] - timestamps[key] < wait * 1_000_000):
                        errors.append(f'text SendInput before computed reading time: {key}')
                elif uses_tts:
                    if key not in tts or completion_reason != 'tts':
                        errors.append(f'audio-judgment TTS requires playback drain: {key}')
                elif completion_reason == 'native':
                    if key not in native:
                        errors.append(f'native completion lacks observed ASR end: {key}')
                elif completion_reason == 'uncertain_text_fallback':
                    wait = u.get('text_wait_ms')
                    if (u.get('uncertain_text_fallback') is not True or
                            type(wait) is not int or wait <= 0 or not completed or
                            completed['at_ns'] - timestamps[key] < wait * 1_000_000):
                        errors.append(f'unpermitted or early uncertain fallback: {key}')
                else:
                    errors.append(f'unknown native completion reason: {key}')
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
                errors.append(f'SendInput at manual handoff instead of stopping: {key}')
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
            if (phase not in ('hook', 'stress') and previous not in sent and
                    not scenario[start + offset - 1].get('manual_handoff')):
                errors.append(f'next dialogue lacks preceding AutoAdv SendInput: {previous}')

    if phase == 'stress':
        # The real challenge run is observational: a fixed 'hold' expected for
        # nonlexical speech/background is not a valid universal oracle.
        if start != 0 or len(utterance_order) != len(scenario):
            errors.append('incomplete ASR stress sequence; observe entire challenge branch')
        for index, key in enumerate(utterance_order):
            if start is None or start + index >= len(scenario):
                break
            case = scenario[start + index]
            if case.get('assessment') is None:
                continue
            if (utterances[key].get('advance_mode') != 'audio' or utterances[key].get('use_tts')):
                errors.append(f'stress must exercise non-TTS audio judgement: {key}')
            # Expect source observations, not a manufactured yes/no 'should hold'.
            # Missing PCM/inference is a coverage gap, not evidence of no voice.
        controls = [key for i, key in enumerate(utterance_order)
                    if start is not None and start + i < len(scenario)
                    and scenario[start + i]['id'] == 'clean_control']
        if not controls or controls[0] not in links or controls[0] not in native:
            errors.append('positive foreground ASR control did not advance')

    linked_modes = Counter((utterances[k].get('advance_mode'), utterances[k].get('use_tts')) for k in links)
    confirmed_native = [k for k in links if k in native and k in pcm]
    confirmed_tts = [k for k in links if k in tts and utterances[k].get('use_tts') is True]
    bgm_native = bool(start is not None and any(
        scenario[start + i].get('bgm') and k in confirmed_native
        for i, k in enumerate(utterance_order)))
    repeats = [k for k in links if utterances[k].get('body_hash') == text_digest('ヒューズが飛んだ。')]
    if not utterance_order:
        errors.append('no live dialogue observed')
    if phase in ('reading', 'mixed') and linked_modes[('text', False)] < (1 if phase == 'mixed' else 2):
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
        if key in vad_end_estimates:
            if (ms := _signed_elapsed_ms(vad_end_estimates[key], presentation_at.get(key))) is not None:
                durations.setdefault('vad_estimated_end_to_decision_ms', []).append(ms)
            if (ms := _signed_elapsed_ms(vad_end_estimates[key], attempts.get(key))) is not None:
                durations.setdefault('vad_estimated_end_to_input_ms', []).append(ms)
            if (onset := vad_onset_estimates.get(key)) is not None:
                # Signed delta: negative means observed voice began before the HOOK.
                source_time = source_outputs.get(origin)
                if source_time is not None:
                    durations.setdefault('hook_to_vad_onset_ms', []).append(
                        round((onset - source_time) / 1_000_000, 3))
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
    # The product trace establishes the Luna->Controller->input->next-HOOK
    # path. The independent game observer is additionally required wherever
    # an actual native-voice advance is being credited, even if the caller
    # labels the run 'reading' or 'tts'. Never silently certify an incidental
    # native input without the evidence necessary to judge premature sending.
    needs_oracle = (phase in VOICE_ORACLE_PHASES or any(
        key in sent and u.get('advance_mode') == 'audio' and not u.get('use_tts')
        for key, u in utterances.items()))
    if oracle_records is not None or needs_oracle:
        game_evidence = audit_game_oracle(
            oracle_records, utterance_order, utterances, attempts, sent, reasons,
            start, scenario, game_pid_by_session=game_pid_by_session,
            presentation_at=presentation_at, vad_end_estimates=vad_end_estimates,
            source_outputs=source_outputs, max_voice_hold_ms=max_voice_hold_ms,
        )
        errors.extend(game_evidence['errors'])
        for name, values in game_evidence['latencies'].items():
            durations.setdefault(name, []).extend(values)
        game_evidence = {k: v for k, v in game_evidence.items() if k != 'latencies'}
    else:
        game_evidence = None
    performance = {name: stats for name, samples in durations.items()
                   if (stats := _summary(samples)) is not None}
    # Preserve each acquired line, including a terminal observation without an
    # input. A two-line success count must never hide an unadvanced third line.
    line_results = []
    for offset, key in enumerate(utterance_order):
        utterance = utterances[key]
        is_terminal = (start is not None and start + offset < len(scenario)
                       and (start + offset == len(scenario) - 1
                            or scenario[start + offset].get('ending')))
        if key not in attempts:
            outcome = 'no_autoadv_input'
        elif key not in sent:
            outcome = 'input_attempt_unconfirmed'
        elif key in links:
            outcome = 'next_hook_observed'
        elif is_terminal:
            outcome = 'terminal_line_input'
        else:
            outcome = 'next_hook_not_observed'
        scenario_id = (scenario[start + offset].get('id')
                       if start is not None and start + offset < len(scenario) else None)
        line_result = {
            'session': key[0], 'line': key[1], 'scenario': scenario_id,
            'advance_mode': utterance.get('advance_mode'),
            'use_tts': utterance.get('use_tts'),
            'completion_reason': reasons.get(key),
            'outcome': outcome,
        }
        if game_evidence is not None and offset < len(game_evidence['lines']):
            line_result['game_outcome'] = game_evidence['lines'][offset]['outcome']
            if 'game_end_to_input_ms' in game_evidence['lines'][offset]:
                line_result['game_end_to_input_ms'] = game_evidence['lines'][offset]['game_end_to_input_ms']
        line_results.append(line_result)
    return {
        'performance': performance,
        'evaluation_scope': ('stress_observation_not_automatic_success' if phase == 'stress'
                             else 'native_voice_timing_and_advancement' if needs_oracle
                             else 'product_hook_source_only' if phase == 'hook'
                             else 'product_presentation_input_and_next_hook'),
        'game_observer': ('required' if needs_oracle else 'optional'),
        'ok': not errors, 'phase': phase,
        'reference_start_line': start + 1 if start is not None else None,
        'observed_lines': len(utterance_order), 'confirmed_advance_to_next_hook': len(links),
        'confirmed_native_asr': len(confirmed_native), 'confirmed_tts': len(confirmed_tts),
        'errors': errors, 'game_evidence': game_evidence, 'line_results': line_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Verify a real AutoAdv GUI-to-game HOOK trace')
    parser.add_argument('--trace', type=Path, required=True, help='fresh AUTOADV_VALIDATION_TRACE JSONL')
    parser.add_argument('--phase', choices=PHASES, required=True,
                        help='hook / reading / native / tts / mixed / stress branch')
    parser.add_argument('--oracle', type=Path,
                        help='independent RenPy JSONL; required for native, mixed and stress')
    parser.add_argument('--report', type=Path,
                        help='write measured per-line outcomes and timing as JSON, including failures')
    parser.add_argument('--max-voice-hold-ms', type=int, default=2000,
                        help='quality threshold for delay after observed voice end')
    parser.add_argument('--start-line', type=int,
                        help='1-based first SCENARIO.json line, to disambiguate repeated text')
    args = parser.parse_args()
    if not args.trace.is_file():
        parser.error('real game trace not found')
    if args.phase in VOICE_ORACLE_PHASES and args.oracle is None:
        parser.error(f'--oracle required for {args.phase} voice-end verification')
    if args.oracle is not None and not args.oracle.is_file():
        parser.error('independent game oracle not found')
    if args.report is not None and args.report.exists():
        parser.error('refusing to overwrite an existing report')
    if args.max_voice_hold_ms <= 0:
        parser.error('--max-voice-hold-ms must be positive')
    try:
        records = [json.loads(x) for x in args.trace.read_text(encoding='utf-8').splitlines() if x.strip()]
        oracle_records = ([json.loads(x) for x in args.oracle.read_text(encoding='utf-8').splitlines() if x.strip()]
                          if args.oracle is not None else None)
        result = validate(records, phase=args.phase, start_line=args.start_line,
                          oracle_records=oracle_records, max_voice_hold_ms=args.max_voice_hold_ms)
        if args.report is not None:
            with args.report.open('x', encoding='utf-8') as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.error(f'invalid real-session trace: {exc}')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
