"""Safe diagnostics only. This CLI NEVER emits game input."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys


def _origin(args: argparse.Namespace) -> int:
    from .luna import observe_origin
    async def run() -> None:
        seen=0
        async for observation in observe_origin(args.url):
            print(json.dumps({"source_seq":observation.sequence,
                              "text":observation.text,
                              "authoritative_for_autoplay":False},ensure_ascii=False),flush=True)
            seen += 1
            if args.count and seen >= args.count: break
    try:
        asyncio.run(run())
    except (OSError, ImportError) as exc:
        print(f"Luna diagnostic connection failed: {exc}",file=sys.stderr)
        return 2
    return 0


def _tts(args: argparse.Namespace) -> int:
    from .irodori import IrodoriConfig, IrodoriClient, play_wav
    from threading import Event
    client=IrodoriClient(IrodoriConfig(url=args.url, speed=args.speed))
    try:
        audio=client.synthesize(args.text,args.voice)
        if args.output:
            Path(args.output).write_bytes(audio)
            print(f"Generated {len(audio)} WAV bytes at {args.output}")
        if args.play:
            if not play_wav(audio,Event()):
                raise RuntimeError("playback interrupted")
            print("Output device finished playing")
        else:
            print("HTTP synthesis completed; playback not requested")
    except Exception as exc:
        print(f"Irodori diagnostic failed: {exc}",file=sys.stderr)
        return 2
    return 0


def _bridge(args: argparse.Namespace) -> int:
    from .luna_bridge import listen
    for observation in listen(port=args.port):
        print(json.dumps({"source_seq":observation.source_seq,
                          "raw_text":observation.raw_text,
                          "text_ready":False,
                          "line_identity_verified":False},ensure_ascii=False),flush=True)
    return 0


def main() -> int:
    parser=argparse.ArgumentParser(description="AutoAdv diagnostics; no automatic game input")
    sub=parser.add_subparsers(dest="command",required=True)
    origin=sub.add_parser("luna-origin",help="inspect original text stream; not line-authoritative")
    origin.add_argument("--url",default="ws://127.0.0.1:2333/api/ws/text/origin")
    origin.add_argument("--count",type=int,default=0,help="0 for unlimited")
    tts=sub.add_parser("tts-smoke",help="request a real WAV from Irodori without game input")
    tts.add_argument("--url",default="http://127.0.0.1:8088/v1/audio/speech")
    tts.add_argument("--voice",required=True)
    tts.add_argument("--text",default="音声テストです。")
    tts.add_argument("--speed",type=float,default=1)
    tts.add_argument("--output",help="save synthesized WAV")
    tts.add_argument("--play",action="store_true",help="play until output device drained (sounddevice required)")
    bridge=sub.add_parser("bridge-monitor",help="inspect opt-in pre-filter candidate packets")
    bridge.add_argument("--port",type=int,default=47916)
    args=parser.parse_args()
    if args.command=="luna-origin":return _origin(args)
    if args.command=="bridge-monitor":return _bridge(args)
    return _tts(args)


if __name__=="__main__":raise SystemExit(main())