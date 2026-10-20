"""Audit evaluation. Truth-dependent error rates are NEVER inferred from logs.

A labeled trial is required for false-advance and unnecessary-pause rates.
Unlabeled JSONL yields only factual counts/latencies, not invented scores.
"""
from __future__ import annotations
import csv
import json
from collections import Counter
from pathlib import Path
from math import floor,ceil


def _percentile(values:list[float],p:float)->float|None:
    if not values:return None
    vals=sorted(values)
    i=(len(vals)-1)*p
    return round(vals[floor(i)]*(ceil(i)-i)+vals[ceil(i)]*(i-floor(i)),2)


def load_jsonl(path:Path)->list[dict]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def evaluate_audit(events:list[dict],labels:list[dict]|None=None)->dict:
    summaries={(x['session_id'],str(x['line_epoch'])):x
        for x in events if x.get('name')=='line_summary'}
    injected=Counter((x['session_id'],str(x['line_epoch']))
        for x in events if x.get('name')=='input_injected')
    duplicates=sum(max(0,n-1) for n in injected.values())
    # Native-end timestamps come from our own VAD/ASR inference. Without
    # independently labeled real voice offsets, this is decision-to-input
    # latency, NOT the ground-truth native-end latency metric.
    latencies=[float(x['input_at_ms']-x['native_end_ms'])
        for x in summaries.values() if x.get('input_at_ms') is not None and x.get('native_end_ms') is not None]
    negative_latencies=sum(v<0 for v in latencies)
    native_latency=[v for v in latencies if v>=0]
    result={
        'line_summaries':len(summaries),
        'input_injections':sum(injected.values()),
        'duplicate_input_count':duplicates,
        'stale_event_applied_count':None, # Cannot certify merely from logs.
        'stale_events_ignored':sum(x.get('name')=='stale_event_ignored' for x in events),
        'detected_native_end_to_input_latency_ms':{
            'n':len(native_latency),'p50':_percentile(native_latency,.5),
            'p95':_percentile(native_latency,.95),'p99':_percentile(native_latency,.99)},
        'negative_native_end_latency_records':negative_latencies,
        'tts_native_playback_overlap_ms':None, # Needs actual device start/stop observations.
        'capture_health_failures':sum(x.get('kind')=='CAPTURE_HEALTH' and x.get('capture_ok') is False
                                       for x in events),
        'pause_reasons':dict(Counter(x.get('pause_reason') for x in summaries.values() if x.get('pause_reason'))),
        'ground_truth_metrics':None,
    }
    if labels is None:return result
    # Only explicitly labeled trials belong to ground-truth populations.
    # In particular, an omitted CSV row/column must never mean "safe".
    eligible={}
    missing=[]
    fields=('false_advance','silent_eligible','should_advance','manual_required')
    for item in labels:
        key=(item['session_id'],str(item['line_epoch']))
        if key not in summaries:
            missing.append({'session_id':key[0],'line_epoch':key[1]})
            continue
        observations={}
        for field in fields:
            value=str(item.get(field,'')).strip().lower()
            if not value:
                continue
            if value not in ('true','false'):
                raise ValueError(f'invalid ground truth flag {field}: {value}')
            observations[field]=(value=='true')
        eligible[key]=observations
    def rate(predicate,success):
        population=[(key,summaries[key]) for key,label in eligible.items() if predicate(label)]
        if not population:return {'n':0,'count':0,'rate':None}
        count=sum(bool(success(summaries[key],eligible[key])) for key,_ in population)
        return {'n':len(population),'count':count,'rate':round(count/len(population),6)}
    # Labels explicitly mark an advance that was incorrect; no attempt is
    # made to reconstruct truth from model outputs or current screen state.
    input_trials=[(k,summaries[k],eligible[k]) for k in eligible
                  if summaries[k].get('input_at_ms') is not None and 'false_advance' in eligible[k]]
    false_count=sum(l['false_advance'] for _,_,l in input_trials)
    result['ground_truth_metrics']={
        'false_advance_rate':{
            'n':len(input_trials),'count':false_count,
            'rate':round(false_count/len(input_trials),6) if input_trials else None},
        'silent_completion_rate':rate(lambda x:x.get('silent_eligible',False),
            lambda s,l:s.get('presentation')=='SILENT' and s.get('final_status')=='advanced_and_acknowledged'),
        'unnecessary_pause_rate':rate(lambda x:x.get('should_advance',False),
            lambda s,l:s.get('final_status')=='paused'),
        'manual_required_stop_recall':rate(lambda x:x.get('manual_required',False),
            lambda s,l:s.get('input_at_ms') is None),
        'unmatched_labels':missing,
        'native_end_latency_ms':None, # Requires independently labeled actual voice end times.
    }
    return result


def main()->int:
    import argparse
    p=argparse.ArgumentParser(description='Evaluate AutoAdv structured per-line audit')
    p.add_argument('audit',type=Path)
    p.add_argument('--labels',type=Path,help='optional manually verified ground-truth CSV')
    args=p.parse_args()
    with args.labels.open(encoding='utf-8-sig',newline='') if args.labels else __import__('contextlib').nullcontext() as f:
        labels=list(csv.DictReader(f)) if f else None
    print(json.dumps(evaluate_audit(load_jsonl(args.audit),labels),ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':raise SystemExit(main())
