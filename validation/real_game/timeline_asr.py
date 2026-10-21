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
from queue import Empty, SimpleQueue
from types import SimpleNamespace

from autoadv.audio_buffer import PCMChunk
from autoadv.conversation import Controller, Target
from autoadv.model import AdvanceMode, HookSelection, Profile
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
           mono, kind: str, chunk_ms: int, tail_ms: int, hook_delay_ms: int = 600,
           fallback: bool = False) -> dict:
    """Time-ordered recorded PCM through production NativeAudio and Controller.

    Only the recording, arrival schedule, focus and simulated input transport
    are fixtures. Native attribution, completion, fallback and one-input
    authority are decided exclusively by the product implementations.
    """
    import numpy as np
    if chunk_ms <= 0 or chunk_ms % 20 or tail_ms < 0 or tail_ms % 20 or hook_delay_ms < 0:
        raise ValueError('timing requires positive 20ms-aligned chunks and nonnegative tail/hook delay')
    observations: list[dict] = []
    inference: list[dict] = []
    inputs: list[dict] = []
    q: SimpleQueue = SimpleQueue()
    t0 = time.monotonic()
    source = SimpleNamespace(hook_code='VALIDATION', context=1, context2=2)
    profile = Profile('recording', 'Recording', hooks=[HookSelection('VALIDATION', 1, 2)],
                      advance_mode=AdvanceMode.AUDIO, uncertain_text_fallback=fallback)
    def deliver_test_input(*_):
        # Fixed transport replaces SendInput only. Keep the production send
        # boundary effect used by GUI.send_input, never fabricate a decision.
        native.boundary()
        inputs.append({'at_ms': elapsed(), 'reason': controller.presentation_reason})
    controller = Controller(deliver_test_input, lambda *_: True)
    controller.begin(profile, Target(native.pid, 1), 'recording-host', 1)
    controller.permit(True)
    token = None
    deadline = None
    next_deadline_check = None

    def elapsed():
        return round((time.monotonic() - t0) * 1000, 2)

    def record_event(t, event, detail):
        q.put((t, event, detail, elapsed()))

    def record_inference(t, metrics):
        if t == token:
            inference.append({'at_ms': elapsed(), **metrics})

    native.boundary()
    native.notify = record_event
    native.on_inference = record_inference
    native.on_voice_end = lambda t, m: observations.append({
        'event': 'voice_end_estimate', 'at_ms': elapsed(),
        'estimated_ms': round(m['estimated_end_ms'] - t0 * 1000, 2),
        'source': m['source']}) if t == token else None

    def accept_hook():
        nonlocal token, deadline, next_deadline_check
        token = controller.observe(generation='recording-host', connection=1,
                                   sequence=index, source=source, text=display,
                                   pid=native.pid)
        if token is None:
            raise RuntimeError('production Controller rejected fixed HOOK')
        native.begin(token, controller.utterance.match_text)
        deadline = time.monotonic() + controller.utterance.text_wait_ms / 1000
        next_deadline_check = deadline

    def pump():
        nonlocal next_deadline_check
        while True:
            try:
                ticket, event, detail, at_ms = q.get_nowait()
            except Empty:
                break
            if ticket != token or token is None:
                continue
            observations.append({'event': event, 'at_ms': at_ms,
                                 'detail': detail if event == 'error' else ''})
            if event == 'started':
                controller.presentation_event(token, 'native_started')
            elif event == 'quiet':
                controller.presentation_event(token, 'native_quiet')
            elif event == 'completed':
                controller.presentation_event(token, 'native_finished')
            elif event == 'error' and not fallback:
                controller.pause('native_audio_failure')
            if controller.completed and not controller.sent:
                observations.append({'event': 'presentation_decision',
                                     'at_ms': elapsed(), 'reason': controller.presentation_reason})
                controller.try_advance()
        if (token is not None and next_deadline_check is not None
                and time.monotonic() >= next_deadline_check
                and not controller.completed and not controller.sent
                and controller.authorized):
            if native.holds_deadline(token):
                next_deadline_check = time.monotonic() + .2
            else:
                if controller.native_playing:
                    controller.presentation_event(token, 'native_evidence_stale')
                controller.presentation_event(token, 'fallback_elapsed')
                next_deadline_check = None
                if controller.completed:
                    observations.append({'event': 'presentation_decision',
                                         'at_ms': elapsed(), 'reason': controller.presentation_reason})
                    controller.try_advance()
                elif not controller.authorized:
                    observations.append({'event': 'manual_handoff', 'at_ms': elapsed(),
                                         'reason': controller.pause_reason})

    hook_begun = False
    frame_count = int(48000 * chunk_ms / 1000)
    all_frames = np.concatenate((mono, np.zeros(int(48000 * tail_ms / 1000), dtype='float32')))
    for frame in range(0, len(all_frames), frame_count):
        remaining = t0 + frame / 48000 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        if not hook_begun and frame / 48 >= hook_delay_ms:
            accept_hook()
            hook_begun = True
        portion = all_frames[frame:frame + frame_count]
        stereo = np.column_stack((portion, portion)).astype('<f4').tobytes()
        pcm = PCMChunk(native.clock(), stereo, 48000, 2, 4)
        native.history.push(pcm, target_pid=native.pid)
        native.feed(pcm)
        pump()
    if not hook_begun:
        remaining = t0 + hook_delay_ms / 1000 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        accept_hook()
    # Drain the ASR actually submitted by the native implementation. Do not
    # synthesize completion or treat a model timeout as positive evidence.
    drain_start = time.monotonic()
    while time.monotonic() - drain_start < 2.5:
        pump()
        with native._lock:
            busy = native._inflight
        if not busy and (next_deadline_check is None or not controller.authorized):
            break
        if not busy and time.monotonic() > (deadline or 0) + .25:
            break
        time.sleep(.01)
    pump()
    with native._lock:
        pending = native._inflight
    native.cancel()
    return {
        'id': name, 'kind': kind, 'display_hash': hashlib.sha256(display.encode('utf-8')).hexdigest(),
        'recorded_audio_ms': round(len(mono) / 48, 2),
        'tail_pcm_ms': tail_ms, 'chunk_ms': chunk_ms, 'hook_delay_ms': hook_delay_ms,
        'fallback': fallback, 'events': observations, 'inferences': inference,
        'input_attempts': inputs, 'presentation_reason': controller.presentation_reason,
        'pause_reason': controller.pause_reason, 'controller_authorized': controller.authorized,
        'decode_pending_after_drain': pending,
        'native_completions': sum(e['event'] == 'completed' for e in observations),
        'possible_early_completion_before_recording_end': any(
            e['event'] == 'completed' and e['at_ms'] < len(mono) / 48 for e in observations),
        'recording_end_to_input_ms': round(inputs[0]['at_ms'] - len(mono) / 48, 2) if inputs else None,
        'recording_end_to_decision_ms': next((round(e['at_ms'] - len(mono) / 48, 2)
            for e in observations if e['event'] == 'presentation_decision'), None),
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
        rows = list(recorded_cases(selection))
        for i, (name, display, pcm, kind) in enumerate(rows, 1):
            records.append(replay(native, index=i, name=name, display=display, mono=pcm,
                                  kind=kind, chunk_ms=chunk_ms, tail_ms=tail_ms,
                                  hook_delay_ms=hook_delay_ms))
        # Actual model, same native+controller decisions after all PCM has
        # already arrived. Not merely a FakeRecognizer unit assertion.
        for name, display, pcm, kind in rows:
            if name in ('normal-001', 'whisper-001'):
                records.append(replay(native, index=len(records) + 1,
                    name=name + '-hook-after-voice', display=display,
                    mono=pcm, kind=kind, chunk_ms=chunk_ms,
                    tail_ms=tail_ms, hook_delay_ms=6000))
        # Exercise fallback ON on difficult real speech, not a parallel timing
        # algorithm. Both paths use the same Controller and NativeAudio.
        for i, (name, display, pcm, kind) in enumerate(rows, 1):
            if kind != 'recorded_voice':
                records.append(replay(native, index=len(records) + 1,
                    name=name + '-fallback-on', display=display, mono=pcm,
                    kind=kind, chunk_ms=chunk_ms, tail_ms=tail_ms,
                    hook_delay_ms=hook_delay_ms, fallback=True))
    finally:
        native.close()
    return {
        'scope': 'production NativeAudio and Controller time-ordered recording replay; synthetic Hook/input transport, not live game validation',
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
