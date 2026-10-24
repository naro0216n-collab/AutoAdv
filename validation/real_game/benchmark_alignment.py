"""Explicit real-model measurement for target-conditioned audio alignment.

This benchmark measures the product aligner itself, not free-transcription CER.
It never certifies live game advancement: independent game playback timing and
Windows input remain the responsibility of the live accuracy verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import time
import wave

import numpy as np

from autoadv.audio_buffer import PCMChunk
from autoadv.forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from autoadv.g2p import default_g2p
from autoadv.native_runtime import managed_paths
from .verify_trace import _summary

HERE = Path(__file__).resolve().parent
ENDPOINT_ORACLE = HERE / 'ALIGNMENT_ENDPOINT_ORACLE.json'
CONDITIONS = ('clean', 'quiet', 'noise', 'clipped', 'truncated', 'bgm')


def _load_pcm(file: Path, sha256: str) -> np.ndarray:
    data = file.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f'voice integrity mismatch: {file.name}')
    with wave.open(str(file), 'rb') as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (48000, 1, 2):
            raise ValueError(f'unsupported voice PCM format: {file.name}')
        return (np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
                .astype('float32') / 32768)


def _degrade(mono: np.ndarray, condition: str, seed: int,
             bgm: np.ndarray | None = None) -> np.ndarray:
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


def _load_bgm() -> np.ndarray:
    bgm_path = HERE / 'renpy' / 'game' / 'audio' / 'music' / 'original_synth_loop.wav'
    with wave.open(str(bgm_path), 'rb') as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (24000, 1, 2):
            raise ValueError('unsupported BGM reference format')
        source = (np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2')
                  .astype('float32') / 32768)
    return np.repeat(source, 2)


def _chunk(mono: np.ndarray) -> PCMChunk:
    stereo = np.column_stack((mono, mono)).astype('<f4')
    duration_ms = round(len(mono) / 48)
    return PCMChunk(duration_ms, stereo.tobytes(), 48000, 2, 4)


def _result_row(sample: dict, condition: str, repetition: int,
                alignment, elapsed_ms: float, audio_ms: float, oracle: dict) -> dict:
    fit = alignment.fit
    end_ms = (alignment.target_end_frame * alignment.frame_ms
              if alignment.target_end_frame is not None else None)
    oracle_end_ms = float(oracle['speech_end_ms'])
    oracle_uncertainty_ms = float(oracle['uncertainty_ms'])
    endpoint_error_ms = (end_ms - oracle_end_ms) if end_ms is not None else None
    committed_error = endpoint_error_ms if alignment.committed_end else None
    return {
        'id': sample['id'],
        'condition': condition,
        'round': repetition,
        'audio_ms': round(audio_ms, 3),
        'alignment_ms': round(elapsed_ms, 3),
        'rtf': round(elapsed_ms / audio_ms, 4) if audio_ms else None,
        'target_units': alignment.target_units,
        'aligned_target_units': alignment.aligned_target_units,
        'target_coverage': round(alignment.coverage, 4),
        'target_associated': alignment.target_associated,
        'complete_alignment': alignment.complete_alignment,
        'committed_end': alignment.committed_end,
        'target_end_ms': round(end_ms, 3) if end_ms is not None else None,
        'oracle_end_ms': round(oracle_end_ms, 3),
        'oracle_uncertainty_ms': round(oracle_uncertainty_ms, 3),
        'endpoint_error_ms': round(endpoint_error_ms, 3) if endpoint_error_ms is not None else None,
        'early_commit': bool(alignment.committed_end and committed_error is not None
                             and committed_error < -oracle_uncertainty_ms),
        'late_commit': bool(alignment.committed_end and committed_error is not None
                            and committed_error > oracle_uncertainty_ms),
        'endpoint_within_oracle': bool(alignment.committed_end and committed_error is not None
                                       and abs(committed_error) <= oracle_uncertainty_ms),
        'post_target_free_frames': alignment.post_target_free_frames,
        'reading_rank': alignment.reading_rank,
        'path_margin': round(fit.path_margin, 4),
        'mean_unit_peak_margin': round(fit.mean_unit_peak_margin, 4),
        'min_unit_peak_margin': round(fit.min_unit_peak_margin, 4),
        'weak_units': fit.weak_units,
        'boundary_margin': round(fit.boundary_margin, 4),
        'rejection': alignment.rejection,
    }


def _load_endpoint_oracle(manifest: dict) -> tuple[dict, dict[str, dict]]:
    data = json.loads(ENDPOINT_ORACLE.read_text(encoding='utf-8'))
    samples = data.get('samples', {})
    expected = {sample['id'] for sample in manifest['samples']}
    if set(samples) != expected:
        missing = sorted(expected - set(samples))
        extra = sorted(set(samples) - expected)
        raise ValueError(f'alignment endpoint oracle mismatch: missing={missing}, extra={extra}')
    for sample in manifest['samples']:
        item = samples[sample['id']]
        end_ms = float(item['speech_end_ms'])
        uncertainty_ms = float(item['uncertainty_ms'])
        duration_ms = float(sample['duration_s']) * 1000.0
        if not (0 < end_ms <= duration_ms):
            raise ValueError(f'invalid speech endpoint for {sample["id"]}')
        if not (0 <= uncertainty_ms < duration_ms):
            raise ValueError(f'invalid endpoint uncertainty for {sample["id"]}')
    return {k: v for k, v in data.items() if k != 'samples'}, samples


def run(model: Path, vocab: Path, *, conditions: tuple[str, ...], rounds: int, seed: int,
        model_backend=None, g2p=None) -> dict:
    if rounds < 1:
        raise ValueError('rounds must be positive')
    manifest = json.loads((HERE / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
    oracle_meta, endpoint_oracle = _load_endpoint_oracle(manifest)
    clips = [(sample, _load_pcm(HERE / 'renpy' / 'game' / sample['path'], sample['sha256']))
             for sample in manifest['samples']]
    bgm = _load_bgm() if 'bgm' in conditions else None
    started = time.perf_counter_ns()
    backend = model_backend if model_backend is not None else OnnxPhoneCTCModel(model, vocab)
    load_ms = (time.perf_counter_ns() - started) / 1_000_000
    pronunciation = g2p if g2p is not None else default_g2p()

    results = []
    for condition in conditions:
        for index, (sample, mono) in enumerate(clips):
            degraded = _degrade(mono, condition, seed + index, bgm)
            chunk = _chunk(degraded)
            aligner = TargetConditionedAligner(backend, g2p=pronunciation)
            aligner.set_target(sample['text'])
            for repetition in range(1, rounds + 1):
                start = time.perf_counter_ns()
                alignment = aligner.align((chunk,))
                elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
                results.append(_result_row(
                    sample, condition, repetition, alignment, elapsed_ms, chunk.duration_ms,
                    endpoint_oracle[sample['id']]))

    by_condition = {}
    for condition in conditions:
        rows = [row for row in results if row['condition'] == condition]
        by_condition[condition] = {
            'alignment_ms': _summary([row['alignment_ms'] for row in rows]),
            'rtf': _summary([row['rtf'] for row in rows if row['rtf'] is not None]),
            'target_coverage': _summary([row['target_coverage'] for row in rows]),
            'associated': sum(row['target_associated'] for row in rows),
            'complete': sum(row['complete_alignment'] for row in rows),
            'committed': sum(row['committed_end'] for row in rows),
            'unconfirmed': sum(not row['committed_end'] for row in rows),
            'early_commits': sum(row['early_commit'] for row in rows),
            'late_commits': sum(row['late_commit'] for row in rows),
            'endpoint_within_oracle': sum(row['endpoint_within_oracle'] for row in rows),
            'endpoint_error_ms': _summary([row['endpoint_error_ms'] for row in rows
                                           if row['committed_end'] and row['endpoint_error_ms'] is not None]),
            'runs': len(rows),
        }
    return {
        'schema': 'autoadv-alignment-benchmark-v2',
        'device': platform.platform(),
        'python': platform.python_version(),
        'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest() if model.is_file() else None,
        'vocab_sha256': hashlib.sha256(vocab.read_bytes()).hexdigest() if vocab.is_file() else None,
        'model_load_ms': round(load_ms, 3),
        'rounds': rounds,
        'seed': seed,
        'conditions': by_condition,
        'samples': results,
        'real_game_verified': False,
        'endpoint_oracle': oracle_meta,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description='On-demand target-conditioned alignment quality/speed measurement')
    paths = managed_paths()
    parser.add_argument('--model', type=Path, default=Path(paths.alignment_model))
    parser.add_argument('--vocab', type=Path, default=Path(paths.alignment_vocab))
    parser.add_argument('--conditions', nargs='+', choices=CONDITIONS,
                        default=['clean', 'quiet', 'noise', 'bgm'])
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--seed', type=int, default=20261024)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run(args.model, args.vocab,
                 conditions=tuple(dict.fromkeys(args.conditions)),
                 rounds=args.rounds, seed=args.seed)
    print(json.dumps({k: v for k, v in result.items() if k != 'samples'},
                     ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                               encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
