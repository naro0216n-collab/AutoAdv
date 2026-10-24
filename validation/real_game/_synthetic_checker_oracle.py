"""Synthetic *verifier-unit-test* observations; NEVER real-session evidence.

This file is imported only by test_*.py. Product and live runner never import it.
Positive unit-test assertions confirm a checker invariant, not automatic game
advancement. Independent negative cases exercise the source-side oracle checks.
"""
from __future__ import annotations
from pathlib import Path
import json

HERE = Path(__file__).parent


def synthetic_game_observations(records, *, phase='mixed', start_line=None):
    from validation.real_game.verify_trace import scenario_for_phase
    scenario = scenario_for_phase(phase)
    utterances = [r for r in records if r['event'] == 'utterance']
    if not utterances:
        return []
    hashes = [r.get('body_hash') for r in utterances]
    from validation.real_game.trace import text_digest
    if start_line is not None:
        candidates = [start_line - 1] if 1 <= start_line <= len(scenario) else []
    else:
        candidates = range(len(scenario) - len(hashes) + 1)
    indexes = [j for j in candidates
               if [text_digest(x['text']) for x in scenario[j:j+len(hashes)]] == hashes]
    if len(indexes) != 1:
        return []  # verifier must reject ambiguous selection, not select one
    product_pid = next((r.get('hook_pid') for r in records if r['event'] == 'monitor_started'), 41)
    events = [dict(event='oracle_started', version=2, pid=product_pid, at_ns=1,
                   endpoint_contract='clean_source_pcm_rms_v1')]
    def add(event, at, **values):
        events.append(dict(event=event, at_ns=at, pid=product_pid, **values))
    for i, u in enumerate(utterances):
        key = (u['session'], u['line'])
        prior = [r for r in records if r.get('session') == key[0] and r.get('line') == key[1]]
        hook = next((r for r in records if r['event'] == 'hook_output' and
                     r.get('session') == key[0] and r.get('hook_sequence') == u.get('hook_sequence')), None)
        if hook is None:
            return []
        sent = next((r for r in prior if r['event'] == 'advance_input'), None)
        case = scenario[indexes[0]+i]
        start = hook['at_ns'] - 100
        add('line_started', start, line=i+1, body_hash=u['body_hash'])
        # Mirror only the authored external mix condition so verifier unit
        # tests can exercise the game-observer contract. This is synthetic
        # checker input and never substitutes for a live Ren'Py execution.
        mix = {channel: f'synthetic/{channel}.ogg'
               for channel in case.get('expected_mix_channels', ())}
        add('mix_snapshot', start + 1, line=i+1, channels=mix)
        if case['foreground_voice']:
            end_at = ((sent['at_ns'] - 100) if sent else (u['at_ns'] + 1000))
            add('voice_started', start+2, line=i+1, file=case.get('foreground_file', 'unknown'),
                channel_position_ms=0.0, source_acoustic_end_at_ns=end_at,
                source_file_end_at_ns=end_at + 100, source_acoustic_end_ms=1.0,
                source_duration_ms=1.1, source_acoustic_uncertainty_ms=0.0,
                source_endpoint_basis='clean_source_pcm_rms_10ms_0.002fs')
            add('voice_channel_inactive', end_at+50, line=i+1,
                file=case.get('foreground_file', 'unknown'),
                last_channel_active_at_ns=end_at+49)
    return sorted(events, key=lambda r: r['at_ns'])
