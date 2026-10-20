"""Validate observations from an actual AutoAdv GUI session with a running game.

No game build/start or internal game assertions; no fake hook/text/audio events
are generated. All input rights and outputs come from the production GUI path.
This check must fail closed if native/TTS/HOOK/input evidence is absent.
"""
from __future__ import annotations
import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Sequence
from autoadv.validation_trace import text_digest

HERE = Path(__file__).resolve().parent

def validate(records: Sequence[dict], manifest_path: Path = HERE / "VOICE_MANIFEST.json") -> dict:
    errors = []
    if not records or records[0].get("event") != "trace_started":
        errors.append("missing real-session trace header")
    for left, right in zip(records, records[1:]):
        if int(right.get("at_ns", -1)) < int(left.get("at_ns", 0)):
            errors.append("non-monotonic event timestamps")
            break
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    voice_hashes = {text_digest(item["text"]) for item in manifest["samples"]}
    expected_repeat = text_digest("ヒューズが飛んだ。")
    active_sessions = set()
    sources = set()
    utterances = {}
    completes = set()
    native_completed = set()
    tts_completed = set()
    native_ready = set()
    sent = set()
    corpus_seen = []
    permits = 0
    for record in records:
        event = record.get("event")
        session = record.get("session")
        if event == "monitor_started" and record.get("source") == "hook":
            active_sessions.add(session)
        if event == "monitor_ended":
            active_sessions.discard(session)
        if event == "hook_output":
            sources.add((session, record.get("source"),
                         record.get("hook_sequence"), record.get("raw_hash")))
        if event == "native_ready":
            native_ready.add(session)
        if event == "permit_requested":
            permits += 1
        if event == "utterance":
            key = (session, record.get("line"))
            if key in utterances:
                errors.append(f"duplicate utterance identity {key}")
            utterances[key] = record
            if session not in active_sessions:
                errors.append(f"utterance outside HOOK monitoring: {key}")
            provenance = (session, record.get("source"),
                          record.get("hook_sequence"), record.get("raw_hash"))
            if provenance not in sources:
                errors.append(f"missing selected Luna output provenance: {key}")
            if record.get("body_hash") in voice_hashes:
                corpus_seen.append((key, record))
        if event == "native_event" and record.get("status") == "completed":
            native_completed.add((session, record.get("line")))
            if session not in native_ready:
                errors.append(f"native speech completed without capture ready: {session}")
        if event == "tts_result" and record.get("success"):
            tts_completed.add((session, record.get("line")))
        if event == "presentation_complete":
            if record.get("success"):
                completes.add((session, record.get("line")))
        if event == "advance_input":
            key = (session, record.get("line"))
            if key in sent:
                errors.append(f"double input for one line: {key}")
            sent.add(key)
            if key not in completes:
                errors.append(f"input before presentation completion: {key}")
            if session not in active_sessions:
                errors.append(f"input outside active game monitor: {key}")
            u = utterances.get(key)
            if u is None:
                errors.append(f"input without observed utterance: {key}")
                continue
            route = u.get("mode")
            if route == "native" and key not in native_completed:
                errors.append(f"native input without speech completion: {key}")
            if route == "tts" and key not in tts_completed:
                errors.append(f"TTS input without confirmed playback drain: {key}")
            if route == "auto" and key not in native_completed | tts_completed:
                errors.append(f"AUTO input without native or TTS completion: {key}")
    if not permits:
        errors.append("no explicit user input authorization")
    if len(corpus_seen) < 6:
        errors.append(f"insufficient real voice-line observations ({len(corpus_seen)}/6)")
    if not any(x.get("mode") == "native" and key in native_completed and key in sent
               for key,x in corpus_seen):
        errors.append("no native dialogue through audio completion and real input")
    if not any(x.get("mode") == "tts" and k in tts_completed and k in sent
               for k,x in utterances.items()):
        errors.append("no Irodori TTS dialogue through playback completion and real input")
    repeats = [(k,v) for k,v in corpus_seen if v.get("body_hash") == expected_repeat]
    if len(repeats) != 2 or any(k not in sent for k,_ in repeats):
        errors.append("repeated identical voiced line did not advance twice")
    # Check sample order against the independently known game scenario.
    # The scenario may contain non-voiced dialogue; those are not constrained here.
    expected = [
        "えっ嘘でしょ。", "あっベルが鳴ってる。", "猫はにゃーにゃーと鳴く。",
        "私はいつもミネラルウォーターを持ち歩いています。",
        "彼女はスタッフをまとめていけると思いますか？",
        "えっ嘘でしょ。", "えっ嘘でしょ。", "あっベルが鳴ってる。",
        "猫はにゃーにゃーと鳴く。", "ヒューズが飛んだ。", "ヒューズが飛んだ。",
    ]
    all_expected = list(map(text_digest,expected))
    actual = [x.get("body_hash") for _,x in corpus_seen]
    if actual[:len(all_expected)] != all_expected:
        errors.append("real HOOK dialogue order differs from voice scene reference")
    continuation = [
        "彼のあだ名は言い得て妙だよね。",
        "ゼロという概念は、ヒンドゥー文化に由来している。",
        "私はポピュラー音楽を聞きたい。",
        "あなたは流感になりかけているか、もっと重い病気かもしれません。",
        "彼女は息子に家で行儀よくするように言った。",
    ]
    tail = actual[len(all_expected):]
    if tail and tail != [text_digest(x) for x in continuation]:
        errors.append("unexpected or incomplete continuation dialogue sequence")

    result = {"ok": not errors, "observed_voice_lines":len(corpus_seen),
              "actual_inputs":len(sent),"native_completions":len(native_completed),
              "tts_completions":len(tts_completed),"errors":errors}
    return result

def main() -> int:
    parser = argparse.ArgumentParser(description="Verify production AutoAdv event evidence")
    parser.add_argument("--trace", type=Path, required=True,
                        help="fresh JSONL from AUTOADV_VALIDATION_TRACE")
    args = parser.parse_args()
    if not args.trace.is_file():
        parser.error("live trace not found: real game connection was not observed")
    records = [json.loads(line) for line in args.trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = validate(records)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result["ok"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
