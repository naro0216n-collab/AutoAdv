"""Independent diagnostic audit of product GUI observations from a *real* game.

No runner, injection, simulated HOOK, audio source or input exists here. A
trace is evidence from one opt-in production run, never proof of execution when
manufactured for unit tests. Scenario expectations are not read by AutoAdv.
"""
from __future__ import annotations

import argparse
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
                      max_voice_hold_ms: int | None = None) -> dict:
    """Compare actual game playback/dismissal with actual AutoAdv inputs.

    The Ren'Py callback/polling stream is independent of the product. It is
    observational, not an alternate source of audio or input for AutoAdv.
    Game audio polling provides an *interval* for playback end, not sample-
    precise Windows device-drain ground truth; uncertainty must remain visible.
    """
    errors: list[str] = []
    diagnostics: list[str] = []
    result = {'errors': errors, 'diagnostics': diagnostics, 'latencies': {}, 'lines': [], 'early_inputs': 0,
              'unverified_voice_ends': 0, 'indeterminate_voice_end': 0,
              'overholds': 0, 'misattributions': 0,
              'manual_or_unadvanced': 0, 'manual_or_external_advances': 0,
              'undismissed_lines': 0, 'confirmed_voice_safe_inputs': 0,
              'unaligned_lines': 0}
    if not oracle_records:
        diagnostics.append('independent game playback evidence unavailable; voice safety unverified')
        return result
    if oracle_records[0].get('event') != 'oracle_started' or oracle_records[0].get('version') != 1:
        errors.append('invalid independent game playback observation header')
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
    # Align *each* game dialogue using only independently observed source text
    # and monotonic timing. A lost/changed HOOK or one faulty parsing rule must
    # not discard observations of every other utterance in the session.
    for offset, key in enumerate(utterance_order):
        utterance = utterances[key]
        stamp = utterance['at_ns']
        matching = [number for position, number in enumerate(ordered)
                    if lines[number]['start'].get('body_hash') == utterance.get('body_hash')
                    and lines[number]['start']['at_ns'] <= stamp
                    and (position + 1 == len(ordered) or
                         stamp < lines[ordered[position + 1]]['start']['at_ns'])]
        if len(matching) != 1:
            result['unaligned_lines'] += 1
            result['lines'].append({
                'scenario': None, 'game_line': None, 'sent': key in sent,
                'voice_observed': None, 'dismissed_after_input': None,
                'outcome': 'game_playback_unaligned',
            })
            diagnostics.append(f'no unique playback observation for HOOK line {key}; voice safety unverified')
            continue
        game_number = matching[0]
        line = lines[game_number]
        # Scenario metadata is optional diagnostic context. Its authored voice
        # classification is never a prerequisite for collecting game playback.
        pos = (reference_start + offset if reference_start is not None else None)
        contract = scenario[pos] if pos is not None and 0 <= pos < len(scenario) else {}
        want_voice = contract.get('foreground_voice')
        if type(want_voice) is not bool:
            want_voice = None
        attempt = attempts.get(key)
        actual_input = sent.get(key)
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
                  'dismissed_after_input': (bool(dismissal and dismissal['at_ns'] >= actual_input)
                                            if actual_input is not None else None),
                  'outcome': 'unadvanced'}
        if actual_input is not None and (dismissal is None or dismissal['at_ns'] < actual_input):
            diagnostics.append(f'confirmed SendInput not followed by observed game dialogue dismissal: {key}')
        if attempt is not None and key not in sent:
            diagnostics.append(f'input attempt has no confirmed SendInput: {key}')
        # The product's SendInput and following LunaHook already verify the
        # advance. Game dismissal and the fixture's chosen sound-file name are
        # not AutoAdv acceptance criteria. Retain playback state solely to
        # compare a native-voice advance with independently observed voice end.
        if want_voice is False and not voice_onsets and reasons.get(key) == 'native':
            result['misattributions'] += 1
            detail['outcome'] = 'background_voice_misattributed'
            diagnostics.append(f'native completion with no foreground voice in independent scenario: {key}')
        elif actual_input is None:
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
        elif strict_native:
            # Each sampled end is an interval (last audible sample, first
            # inactive sample], not an exact source-audio termination timestamp.
            # Preserve the signed latency to the first inactive observation as
            # a diagnostic; do not use it to classify overlap as success/failure.
            end = voice_ends[0] if voice_ends else None
            if voice_ends:
                observed = voice_ends[0]
                ms = _signed_elapsed_ms(observed['at_ns'], actual_input)
                result['latencies'].setdefault('game_voice_end_to_input_ms', []).append(ms)
                detail['game_end_to_input_ms'] = ms
                if (decision := presentation_at.get(key)) is not None:
                    result['latencies'].setdefault('game_voice_end_to_decision_ms', []).append(
                        _signed_elapsed_ms(observed['at_ns'], decision))
                if (estimated := vad_end_estimates.get(key)) is not None:
                    result['latencies'].setdefault('game_voice_end_to_vad_estimate_ms', []).append(
                        _signed_elapsed_ms(observed['at_ns'], estimated))
            active_at_dismiss = bool(dismissal and dismissal.get('voice_active'))
            if active_at_dismiss or (end and actual_input < end['last_active_at_ns']):
                result['early_inputs'] += 1
                detail['outcome'] = 'early_input'
                errors.append(f'input while the game voice was still playing: {key}')
            elif not end:
                result['unverified_voice_ends'] += 1
                detail['outcome'] = 'voice_end_unverified'
                diagnostics.append(f'voice-end not independently observed for input: {key}')
            elif actual_input < end['at_ns']:
                result['indeterminate_voice_end'] += 1
                detail['outcome'] = 'voice_end_indeterminate'
                # An ambiguous interval cannot count as a safe advance, but it
                # must not be called a demonstrated early input either.
                diagnostics.append(f'input within voice-end observer uncertainty interval: {key}')
            else:
                ms = detail['game_end_to_input_ms']
                if (ms is not None and max_voice_hold_ms is not None
                        and ms > max_voice_hold_ms):
                    result['overholds'] += 1
                    detail['outcome'] = 'overheld'
                    diagnostics.append(f'observed voice-end-to-input latency {ms:.0f}ms exceeds diagnostic reference: {key}')
                else:
                    detail['outcome'] = 'voice_end_confirmed_then_input'
                    result['confirmed_voice_safe_inputs'] += 1
        else:
            detail['outcome'] = ('native_voice_not_required_by_text_or_tts_policy'
                                 if want_voice else 'input_without_foreground_voice')
        result['lines'].append(detail)
    return result


def scenario_for_phase(phase: str, scenario_path: Path = HERE / 'SCENARIO.json') -> list[dict]:
    # These are test-side expectations, never a source of AutoAdv decisions.
    if phase == 'stress' and scenario_path == HERE / 'SCENARIO.json':
        scenario_path = HERE / 'ASR_STRESS_SCENARIO.json'
    scenario = json.loads(scenario_path.read_text(encoding='utf-8'))['lines']
    if phase == 'mixed' and scenario_path == HERE / 'SCENARIO.json':
        # The same Ren'Py game terminates before its interactive menu when
        # launched with AUTOADV_VALIDATION_SCENARIO=mixed.
        handoff = next((i for i, line in enumerate(scenario) if line.get('manual_handoff')), None)
        if handoff is None:
            raise ValueError('mixed game has no menu boundary')
        scenario = scenario[:handoff] + [{
            'id': 'auto-test-end', 'text': '通常会話の検証を終了します。',
            'ending': True, 'advance_mode': 'text', 'use_tts': False,
            'foreground_voice': False,
        }]
    return scenario


def validate(records: Sequence[dict], *, phase: str = 'mixed',
             scenario_path: Path = HERE / 'SCENARIO.json',
             start_line: int | None = None,
             oracle_records: Sequence[dict] | None = None,
             max_voice_hold_ms: int | None = None) -> dict:
    if phase not in PHASES:
        raise ValueError(f'unknown validation phase: {phase}')
    scenario = scenario_for_phase(phase, scenario_path)
    expected_hashes = [text_digest(x['text']) for x in scenario]
    errors: list[str] = []
    diagnostics: list[str] = []
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
    audio_methods: dict[object, str] = {}
    audio_targets: dict[object, int] = {}
    authorized: set[object] = set()
    timeouts: set[tuple] = set()
    handoffs: dict[tuple, str] = {}
    capture_summaries: dict[tuple, dict] = {}
    methods: dict[tuple, str] = {}
    reasons: dict[tuple, str] = {}
    presentation_at: dict[tuple, int] = {}
    audio_started_at: dict[tuple, int] = {}
    audio_completed_at: dict[tuple, int] = {}
    vad_end_estimates: dict[tuple, int] = {}
    vad_onset_estimates: dict[tuple, int] = {}
    asr_counts: dict[tuple, int] = {}
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
            if 'vad_only' in record:
                errors.append(f'removed VAD-only audio configuration in product trace: {session}')
            if session in monitoring and record.get('audio_pid') == monitoring[session].get('audio_pid'):
                readiness.add(session)
                audio_methods[session] = 'asr'
                audio_targets[session] = record.get('audio_pid')
        elif event == 'native_pcm':
            if (key not in utterances or type(record.get('frames')) is not int or record['frames'] <= 0
                    or record.get('audio_pid') != audio_targets.get(session)
                    or record.get('audio_pid') != monitoring.get(session, {}).get('audio_pid')):
                errors.append(f'PCM is not valid captured frames for {key}')
            pcm.add(key)
        elif event == 'asr_inference':
            if key in utterances:
                asr_counts[key] = asr_counts.get(key, 0) + 1
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
            if record.get('method') != 'asr':
                errors.append(f'native event without ASR judgement method: {key}')
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
                    record.get('evidence') != 'asr_vad'):
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
            authorized.add(session)
        elif event == 'pause_requested':
            authorized.discard(session)
        elif event == 'advance_attempt':
            if key in attempts:
                errors.append(f'duplicate SendInput attempt: {key}')
            attempts[key] = at
        elif event == 'advance_input':
            if key not in attempts or attempts[key] > at:
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
                        errors.append(f'native completion lacks observed audio judgement end: {key}')
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
        elif event == 'controller_handoff':
            if key not in utterances or not isinstance(record.get('reason'), str) or not record['reason']:
                errors.append(f'invalid Controller handoff: {key}')
            else:
                handoffs[key] = record['reason']
        elif event == 'native_capture_summary':
            # Capture transport health and ASR/VAD judgement are independent.
            # No count or PCM threshold is a test pass condition.
            if key in utterances:
                capture_summaries[key] = {
                    name: record.get(name) for name in
                    ('scope', 'pcm_packets', 'pcm_frames', 'heartbeats',
                     'vad_positive_packets', 'vad_input_peak', 'failure')
                }

    for key in native:
        if (key[0] not in readiness or key not in pcm or
                methods.get(key) != audio_methods.get(key[0])):
            errors.append(f'native completion lacks matching real PCM and ASR/VAD evidence: {key}')

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
        # The fixture text can reveal a parsing regression, but the product's
        # received output is still usable to diagnose the controller/input path.
        diagnostics.append('HOOK text cannot be uniquely aligned to fixture: '
                           'report unaligned evidence rather than reject observed product events')
    start = matches[0] if len(matches) == 1 else None
    links: set[tuple] = set()
    # Source provenance, the product's input, and the subsequent Hook event
    # establish the actual continuation, independent of fixture text identity.
    for offset, key in enumerate(utterance_order):
        if key not in sent or offset + 1 >= len(utterance_order):
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
    # These are measurements of the product path, not fixture-authored
    # instructions to AutoAdv. Unobserved continuation is always reported,
    # regardless of what line number or expected scene was selected.
    for offset, key in enumerate(utterance_order):
        if key in sent and key not in links:
            diagnostics.append(f'input sent but following HOOK not observed: {key}')
        if key in timeouts:
            diagnostics.append(f'controller handed off after next-line timeout: {key}')
        if offset:
            previous = utterance_order[offset - 1]
            if previous not in sent and phase != 'hook':
                diagnostics.append(f'dialogue observed without preceding AutoAdv SendInput: {previous}')
    confirmed_native = [k for k in links if k in native and k in pcm
                        and methods.get(k) == 'asr']
    confirmed_tts = [k for k in links if k in tts and utterances[k].get('use_tts') is True]
    if not utterance_order:
        errors.append('no live dialogue observed')
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
        diagnostics.extend(game_evidence['diagnostics'])
        for name, values in game_evidence['latencies'].items():
            durations.setdefault(name, []).extend(values)
        game_evidence = {k: v for k, v in game_evidence.items() if k != 'latencies'}
        if phase == 'stress' and game_evidence['manual_or_external_advances']:
            # Manual progression through unresolved lines may help inspect
            # subsequent cases, but it is not a successful unattended
            # auto-advance run. Report observations without upgrading them.
            diagnostics.append('stress coverage contains game progression without AutoAdv input')
    else:
        game_evidence = None
    performance = {name: stats for name, samples in durations.items()
                   if (stats := _summary(samples)) is not None}
    # Preserve each acquired line, including a terminal observation without an
    # input. A two-line success count must never hide an unadvanced third line.
    line_results = []
    for offset, key in enumerate(utterance_order):
        utterance = utterances[key]
        if key not in attempts:
            outcome = 'no_autoadv_input'
        elif key not in sent:
            outcome = 'input_attempt_unconfirmed'
        elif key in links:
            outcome = 'next_hook_observed'
        else:
            outcome = 'next_hook_not_observed'
        scenario_id = (scenario[start + offset].get('id')
                       if start is not None and start + offset < len(scenario) else None)
        line_result = {
            'session': key[0], 'line': key[1], 'scenario': scenario_id,
            'advance_mode': utterance.get('advance_mode'),
            'use_tts': utterance.get('use_tts'),
            'completion_reason': reasons.get(key),
            'stop_reason': handoffs.get(key),
            'fallback_enabled': utterance.get('uncertain_text_fallback'),
            'source': utterance.get('source'),
            'body_hash': utterance.get('body_hash'),
            'text_wait_ms': utterance.get('text_wait_ms'),
            'characters': utterance.get('characters'),
            'native_setup_ready': key[0] in readiness,
            'audio_judgement_method': audio_methods.get(key[0]),
            'pcm_observed': key in pcm,
            'asr_inference_count': asr_counts.get(key, 0),
            'capture_transport': capture_summaries.get(key),
            'native_judgement_started': key in audio_started_at,
            'native_judgement_completed': key in native,
            'vad_end_estimate_ns': vad_end_estimates.get(key),
            'input_attempted': key in attempts,
            'input_sent': key in sent,
            'next_hook_observed': key in links,
            'next_hook_timeout': key in timeouts,
            'outcome': outcome,
        }
        if game_evidence is not None and offset < len(game_evidence['lines']):
            line_result['game_outcome'] = game_evidence['lines'][offset]['outcome']
            line_result['game_dismissed_after_input'] = (
                game_evidence['lines'][offset].get('dismissed_after_input'))
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
        'collection_valid': not errors,
        'advancement_observed': bool(links),
        'voice_safety_verified': (game_evidence is not None and
            game_evidence['confirmed_voice_safe_inputs'] > 0 and
            not game_evidence['early_inputs'] and
            not game_evidence['unverified_voice_ends'] and
            not game_evidence['indeterminate_voice_end'] and
            not game_evidence['misattributions'] and
            not game_evidence['unaligned_lines'] and
            len(game_evidence['lines']) == len(utterance_order)) if needs_oracle else None,
        'reference_start_line': start + 1 if start is not None else None,
        'observed_lines': len(utterance_order), 'confirmed_advance_to_next_hook': len(links),
        'confirmed_native_asr': len(confirmed_native),
        'confirmed_tts': len(confirmed_tts),
        'errors': errors, 'diagnostics': diagnostics, 'game_evidence': game_evidence, 'line_results': line_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Collect and audit real AutoAdv GUI-to-game HOOK observations')
    parser.add_argument('--trace', type=Path, required=True, help='fresh AUTOADV_VALIDATION_TRACE JSONL')
    parser.add_argument('--phase', choices=PHASES, required=True,
                        help='hook / reading / native / tts / mixed / stress branch')
    parser.add_argument('--oracle', type=Path,
                        help='independent RenPy JSONL; required for native, mixed and stress')
    parser.add_argument('--report', type=Path,
                        help='write measured per-line outcomes and timing as JSON, including failures')
    parser.add_argument('--max-voice-hold-ms', type=int,
                        help='optional diagnostic delay reference; no default acceptance threshold')
    parser.add_argument('--start-line', type=int,
                        help='1-based first SCENARIO.json line, to disambiguate repeated text')
    args = parser.parse_args()
    if not args.trace.is_file():
        parser.error('real game trace not found')
    # Missing external playback evidence must not prevent product data capture.
    # The resulting report explicitly leaves voice safety unverified.
    if args.report is not None and args.report.exists():
        parser.error('refusing to overwrite an existing report')
    if args.max_voice_hold_ms is not None and args.max_voice_hold_ms <= 0:
        parser.error('--max-voice-hold-ms must be positive')
    try:
        records = [json.loads(x) for x in args.trace.read_text(encoding='utf-8').splitlines() if x.strip()]
        oracle_records = ([json.loads(x) for x in args.oracle.read_text(encoding='utf-8').splitlines() if x.strip()]
                          if args.oracle is not None and args.oracle.is_file() else None)
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
