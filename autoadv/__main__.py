"""Default GUI entrypoint; diagnostics are safe and never authorize game input."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import sys


def main() -> int:
    parser=argparse.ArgumentParser(description='AutoAdv GUI and input-free diagnostics')
    sub=parser.add_subparsers(dest='command')
    tts=sub.add_parser('tts-smoke',help='test Irodori synthesis without game input')
    tts.add_argument('--url',default='http://127.0.0.1:8088/v1/audio/speech')
    tts.add_argument('--voice',required=True)
    tts.add_argument('--text',default='音声テストです。')
    tts.add_argument('--speed',type=float,default=1)
    tts.add_argument('--output')
    tts.add_argument('--play',action='store_true')
    report=sub.add_parser('audit-evaluate',help='evaluate structured diagnostics')
    report.add_argument('audit',type=Path)
    report.add_argument('--labels',type=Path)
    live=sub.add_parser('run-windows',help='input-disabled diagnostics through the shared Application path')
    live.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    if not args.command:
        try:
            from .gui import run_gui
            return run_gui()
        except ImportError as exc:
            print(f'PySide6 GUI is unavailable: {exc}; install AutoAdv with .[gui]',file=sys.stderr)
            return 2
    if args.command=='run-windows':
        from .live import LiveConfig,run_windows
        try:
            from .preferences import Preferences
            return run_windows(LiveConfig.load(args.config),preferences=Preferences.load(),enable_input=False)
        except (OSError,ValueError,FileNotFoundError) as exc:
            print(f'AutoAdv runtime error: {exc}',file=sys.stderr)
            return 2
    if args.command=='audit-evaluate':
        from .evaluation import load_jsonl,evaluate_audit
        try:
            labels=None
            if args.labels:
                with args.labels.open(encoding='utf-8-sig',newline='') as f:labels=list(csv.DictReader(f))
            print(json.dumps(evaluate_audit(load_jsonl(args.audit),labels),ensure_ascii=False,indent=2))
            return 0
        except (OSError,ValueError,KeyError,json.JSONDecodeError) as exc:
            print(f'audit evaluation failed: {exc}',file=sys.stderr);return 2
    from .irodori import IrodoriClient,IrodoriConfig,play_wav
    from threading import Event
    try:
        audio=IrodoriClient(IrodoriConfig(url=args.url,speed=args.speed)).synthesize(args.text,args.voice)
        if args.output:Path(args.output).write_bytes(audio)
        if args.play and not play_wav(audio,Event()):raise RuntimeError('audio playback interrupted')
        print('Synthesis and playback' if args.play else 'HTTP synthesis','completed')
        return 0
    except Exception as exc:
        print(f'Irodori diagnostic failed: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
