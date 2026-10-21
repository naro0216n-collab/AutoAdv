"""Explicit real-time PCM replay through production NativeAudio + SenseVoice.

This is a recording/ASR timing experiment, not a game, a loopback adapter, or
an independent advancement algorithm. It never asserts real-game acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import time

from autoadv.audio_buffer import PCMChunk
from autoadv.conversation import Token
from autoadv.native_audio import JapaneseASR, NativeAudio
from .benchmark_asr import HERE, _load_pcm
from .prepare_asr_model import MODEL_DIR
from .stress_asr import _load_cases, _mix


def recorded_cases(selection: str):
    if selection not in ('corpus', 'stress', 'all'):
        raise ValueError('invalid recording selection')
    if selection in ('corpus', 'all'):
        manifest = json.loads((HERE / 'VOICE_MANIFEST.json').read_text('utf-8'))
        for sample in manifest['samples']:
            yield sample['id'], sample['text'], _load_pcm(
                HERE / 'renpy' / 'game' / sample['path'], sample['sha256']), 'recorded_voice'
    if selection in ('stress', 'all'):
        scenarios, source_audio = _load_cases()
        for case in scenarios:
            yield case['id'], case['display'], _mix(case, source_audio), case['kind']


def replay(native: NativeAudio, *, index: int, name: str, display: str,
           mono, kind: str, chunk_ms: int, tail_ms: int, hook_delay_ms: int = 600) -> dict:
    import numpy as np
    if chunk_ms <= 0 or chunk_ms % 20 or tail_ms < 0 or tail_ms % 20 or hook_delay_ms < 0:
        raise ValueError('timing requires positive 20ms-aligned chunks and nonnegative tail/hook delay')
    token = Token(1, index)
    observations: list[dict] = []
    inference: list[dict] = []
    t0 = time.monotonic()
    # Simulate a real send followed by a delayed Luna flush. Audio packets
    # can arrive and even finish before the Utterance exists.
    native.boundary()
    hook_begun = False

    def record_event(t, event, detail):
        if t == token:
            observations.append({'event': event, 'at_ms': round((time.monotonic() - t0) * 1000, 2),
                                 'detail': detail if event == 'error' else ''})

    def record_inference(t, metrics):
        if t == token:
            inference.append({'at_ms': round((time.monotonic() - t0) * 1000, 2), **metrics})

    # No capture or SendInput is faked: explicitly feed verified recording PCM
    # through the same NativeAudio.feed/VoiceActivity/TargetMatcher callbacks.
    native.notify = record_event
    native.on_inference = record_inference
    native.on_voice_end = lambda t, m: observations.append({
        'event': 'voice_end_estimate', 'at_ms': round((time.monotonic() - t0) * 1000, 2),
        'estimated_ms': round(m['estimated_end_ms'] - t0 * 1000, 2),
        'source': m['source']}) if t == token else None
    frame_count = int(48000 * chunk_ms / 1000)
    total_frames = len(mono) + int(48000 * tail_ms / 1000)
    all_frames = np.concatenate((mono, np.zeros(int(48000 * tail_ms / 1000), dtype='float32')))
    for frame in range(0, total_frames, frame_count):
        target_s = frame / 48000
        remaining = t0 + target_s - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        portion = all_frames[frame:frame + frame_count]
        stereo = np.column_stack((portion, portion)).astype('<f4').tobytes()
        if not hook_begun and target_s * 1000 >= hook_delay_ms:
            native.begin(token, display)
            hook_begun = True
        pcm = PCMChunk(native.clock(), stereo, 48000, 2, 4)
        native.history.push(pcm, target_pid=native.pid)
        native.feed(pcm)
    if not hook_begun:
        # Test Hook arrival even after the audio file and its tail.
        remaining = t0 + hook_delay_ms / 1000 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        native.begin(token, display)
    # Allow already-submitted inference to finish, but a late result never
    # becomes a synthetic successful completion. Report outstanding decode.
    drain_start = time.monotonic()
    while time.monotonic() - drain_start < 2.0:
        with native._lock:
            busy = native._inflight
        if not busy:
            break
        time.sleep(.01)
    with native._lock:
        pending = native._inflight
    native.cancel()
    return {
        'id': name, 'kind': kind, 'display_hash': hashlib.sha256(display.encode('utf-8')).hexdigest(),
        'recorded_audio_ms': round(len(mono) / 48, 2),
        'tail_pcm_ms': tail_ms, 'chunk_ms': chunk_ms,
        'hook_delay_ms': hook_delay_ms,
        'events': observations, 'inferences': inference,
        'decode_pending_after_drain': pending,
        'native_completions': sum(e['event'] == 'completed' for e in observations),
        'possible_early_completion_before_recording_end': any(
            e['event'] == 'completed' and e['at_ms'] < len(mono) / 48 for e in observations),
    }


def run(model: Path, tokens: Path, *, selection: str, chunk_ms: int = 40,
        tail_ms: int = 600, hook_delay_ms: int = 600) -> dict:
    if chunk_ms <= 0 or chunk_ms % 20 or tail_ms < 0 or tail_ms % 20 or hook_delay_ms < 0:
        raise ValueError('invalid timeline chunk/tail/hook delay length')
    started = time.monotonic()
    recognizer = JapaneseASR(model, tokens)
    load_ms = (time.monotonic() - started) * 1000
    native = NativeAudio(42, Path('not-started-in-recording-replay.dll'), model, tokens,
                         lambda *_: None, recognizer=recognizer)
    records = []
    try:
        native.history.start(native.pid)
        for i, (name, display, pcm, kind) in enumerate(recorded_cases(selection), 1):
            records.append(replay(native, index=i, name=name, display=display, mono=pcm,
                                  kind=kind, chunk_ms=chunk_ms, tail_ms=tail_ms,
                                  hook_delay_ms=hook_delay_ms))
    finally:
        native.close()
    return {
        'scope': 'production NativeAudio time-ordered recording replay; not live game validation',
        'device': platform.platform(), 'python': platform.python_version(),
        'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
        'tokens_sha256': hashlib.sha256(tokens.read_bytes()).hexdigest(),
        'model_load_ms': round(load_ms, 2),
        'cases': records, 'real_game_verified': False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Timed real recording PCM -> product NativeAudio with real SenseVoice')
    parser.add_argument('--model', type=Path, default=MODEL_DIR / 'model.int8.onnx')
    parser.add_argument('--tokens', type=Path, default=MODEL_DIR / 'tokens.txt')
    parser.add_argument('--selection', choices=['corpus', 'stress', 'all'], default='all')
    parser.add_argument('--chunk-ms', type=int, default=40)
    parser.add_argument('--hook-delay-ms', type=int, default=600,
                        help='Delay HOOK binding relative to the initial PCM, in milliseconds')
    parser.add_argument('--tail-ms', type=int, default=600)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run(args.model, args.tokens, selection=args.selection,
                 chunk_ms=args.chunk_ms, tail_ms=args.tail_ms,
                 hook_delay_ms=args.hook_delay_ms)
    brief = [{key: row[key] for key in ('id', 'native_completions',
              'possible_early_completion_before_recording_end', 'decode_pending_after_drain')}
             for row in result['cases']]
    print(json.dumps({'scope': result['scope'], 'cases': brief, 'real_game_verified': False},
                     ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
