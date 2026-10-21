"""Synthetic *verifier-unit-test* observations; NEVER real-session evidence.

This file is imported only by test_*.py. Product and live runner never import it.
Positive unit-test assertions confirm a checker invariant, not automatic game
advancement. Independent negative cases exercise the source-side oracle checks.
"""
from __future__ import annotations
from pathlib import Path
import json

HERE = Path(__file__).parent


def synthetic_game_observations(records, *, phase='mixed'):
    from validation.real_game.verify_trace import scenario_for_phase
    scenario = scenario_for_phase(phase)
    utterances = [r for r in records if r['event'] == 'utterance']
    if not utterances:
        return []
    hashes = [r.get('body_hash') for r in utterances]
    from autoadv.validation_trace import text_digest
    indexes = [j for j in range(len(scenario) - len(hashes) + 1)
               if [text_digest(x['text']) for x in scenario[j:j+len(hashes)]] == hashes]
    if len(indexes) != 1:
        return []  # verifier must reject ambiguous selection, not select one
    product_pid = next((r.get('hook_pid') for r in records if r['event'] == 'monitor_started'), 41)
    events = [dict(event='oracle_started', version=1, pid=product_pid, at_ns=1)]
    def add(event, at, **values):
        events.append(dict(event=event, at_ns=at, pid=product_pid, **values))
    for i, u in enumerate(utterances):
        key = (u['session'], u['line'])
        prior = [r for r in records if r.get('session') == key[0] and r.get('line') == key[1]]
        hook = next((r for r in records if r['event'] == 'hook_output' and
                     r.get('session') == key[0] and r.get('hook_sequence') == u.get('hook_sequence')), None)
        if hook is None:
            return []
        attempt = next((r for r in prior if r['event'] == 'advance_attempt'), None)
        case = scenario[indexes[0]+i]
        start = hook['at_ns'] - 100
        add('line_started', start, line=i+1, body_hash=u['body_hash'])
        if case['foreground_voice']:
            add('voice_started', start+1, line=i+1, file=case.get('foreground_file', 'unknown'))
            end_at = ((attempt['at_ns'] - 100) if attempt else (u['at_ns'] + 1000))
            add('voice_ended', end_at, line=i+1, file=case.get('foreground_file', 'unknown'),
                last_active_at_ns=end_at-2)
        if attempt:
            add('line_dismissed', attempt['at_ns']+1, line=i+1, voice_active=False)
    return sorted(events, key=lambda r: r['at_ns'])
