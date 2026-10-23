"""Explicit, non-CI measurement using the production JapaneseASR recognizer.

All degradation happens in memory; original recorded voice files stay unchanged.
Numbers describe this machine/model/session, not a cross-machine CI gate.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import platform
import time
import wave

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_match import TargetMatcher, surface_key
from autoadv.native_audio import JapaneseASR
from .verify_trace import _summary
from autoadv.native_runtime import managed_paths

HERE = Path(__file__).resolve().parent
CONDITIONS = ('clean', 'quiet', 'noise', 'clipped', 'truncated', 'bgm')


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, first in enumerate(left, 1):
        current = [i]
        for j, second in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (first != second)))
        previous = current
    return previous[-1]


def _load_pcm(file: Path, sha256: str):
    import numpy as np
    data = file.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f'voice integrity mismatch: {file.name}')
    with wave.open(str(file), 'rb') as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (48000, 1, 2):
            raise ValueError(f'unsupported voice PCM format: {file.name}')
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').astype('float32') / 32768


def _degrade(mono, condition: str, seed: int, bgm=None):
    import numpy as np
    signal = mono.copy()
    if condition == 'quiet':
        signal *= .125
    elif condition == 'noise':
        signal += np.random.default_rng(seed).normal(0, .075, signal.shape).astype('float32')
    elif condition == 'clipped':
        signal = np.clip(signal * 4, -.45, .45)
    elif condition == 'truncated':
        signal[:round(len(signal) * .25)] = 0
    elif condition == 'bgm':
        if bgm is None or len(bgm) == 0:
            raise ValueError('BGM reference audio required')
        repeated = np.tile(bgm, (len(signal) + len(bgm) - 1) // len(bgm))[:len(signal)]
        signal += repeated * .4
    elif condition != 'clean':
        raise ValueError(f'unknown condition: {condition}')
    return np.clip(signal, -1, 1).astype('<f4')


def _load_bgm():
    import numpy as np
    bgm_path = HERE / 'renpy' / 'game' / 'audio' / 'music' / 'original_synth_loop.wav'
    with wave.open(str(bgm_path), 'rb') as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (24000, 1, 2):
            raise ValueError('unsupported BGM reference format')
        source = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').astype('float32') / 32768
    return np.repeat(source, 2)  # 24kHz => 48kHz, deterministic condition, no output files


def run(model: Path, tokens: Path, *, conditions: tuple[str, ...], rounds: int, seed: int) -> dict:
    if rounds < 1:
        raise ValueError('rounds must be positive')
    manifest = json.loads((HERE / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
    clips = [(sample, _load_pcm(HERE / 'renpy' / 'game' / sample['path'], sample['sha256']))
             for sample in manifest['samples']]
    bgm = _load_bgm() if 'bgm' in conditions else None
    started = time.perf_counter_ns()
    recognizer = JapaneseASR(model, tokens)
    load_ms = (time.perf_counter_ns() - started) / 1_000_000
    results = []
    for condition in conditions:
        for index, (sample, mono) in enumerate(clips):
            degraded = _degrade(mono, condition, seed + index, bgm)
            # Call production PCM preparation and inference, without replacing
            # it by an independent resampler or a simulated recognizer.
            stereo = __import__('numpy').column_stack((degraded, degraded)).astype('<f4')
            chunk = PCMChunk(0, stereo.tobytes(), 48000, 2, 4)
            audio_ms = chunk.duration_ms
            for repetition in range(rounds):
                start = time.perf_counter_ns()
                recognized = recognizer.recognize((chunk,))
                elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
                expected = surface_key(sample['text'])
                actual = surface_key(recognized.text)
                alignment = TargetMatcher(sample['text']).align(
                    recognized.text, tokens=recognized.tokens)
                results.append({
                    'id': sample['id'], 'condition': condition, 'round': repetition + 1,
                    'audio_ms': round(audio_ms, 3), 'decode_ms': round(elapsed_ms, 3),
                    'rtf': round(elapsed_ms / audio_ms, 4),
                    'cer': round(_edit_distance(expected, actual) / max(1, len(expected)), 4),
                    'phonetic_coverage': round(alignment.phonetic_coverage, 4),
                    'phonetic_matches': alignment.phonetic_matches,
                    'phonetic_target_units': alignment.phonetic_target_units,
                    'phonetic_edit_count': alignment.phonetic_edits,
                    'reading_rank': alignment.reading_rank,
                    'recognized_hash': hashlib.sha256(actual.encode('utf-8')).hexdigest(),
                })
    by_condition = {}
    for condition in conditions:
        rows = [row for row in results if row['condition'] == condition]
        by_condition[condition] = {
            'decode_ms': _summary([row['decode_ms'] for row in rows]),
            'rtf': _summary([row['rtf'] for row in rows]),
            'cer': _summary([row['cer'] for row in rows]),
            'phonetic_coverage': _summary([row['phonetic_coverage'] for row in rows]),
            'normalized_exact': sum(row['cer'] == 0 for row in rows),
            'runs': len(rows),
        }
    return {
        'device': platform.platform(), 'python': platform.python_version(),
        'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
        'tokens_sha256': hashlib.sha256(tokens.read_bytes()).hexdigest(),
        'model_load_ms': round(load_ms, 3), 'rounds': rounds, 'seed': seed,
        'conditions': by_condition, 'samples': results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='On-demand real-model ASR quality AND speed measurement (not Fast CI)')
    parser.add_argument('--model', type=Path, default=Path(managed_paths().asr_model))
    parser.add_argument('--tokens', type=Path, default=Path(managed_paths().asr_tokens))
    parser.add_argument('--conditions', nargs='+', choices=CONDITIONS, default=['clean', 'quiet', 'noise', 'bgm'])
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20261021)
    parser.add_argument('--output', type=Path, help='optional machine-readable JSON report')
    args = parser.parse_args()
    result = run(args.model, args.tokens, conditions=tuple(dict.fromkeys(args.conditions)),
                 rounds=args.rounds, seed=args.seed)
    print(json.dumps({key: value for key, value in result.items() if key != 'samples'},
                     ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
