"""Explicit real-model measurement for target-conditioned audio alignment.

This benchmark measures the product aligner itself, not free-transcription CER.
It never certifies live game advancement: independent game playback timing and
Windows input remain the responsibility of the live accuracy verifier.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import platform
import time
import wave

import numpy as np

from autoadv.audio_buffer import PCMChunk
from autoadv.forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from autoadv.g2p import default_g2p
from autoadv.native_audio import NativeAudio
from autoadv.native_runtime import managed_paths
from .verify_trace import _summary

HERE = Path(__file__).resolve().parent
ENDPOINT_ORACLE = HERE / 'ALIGNMENT_ENDPOINT_ORACLE.json'
CONDITIONS = ('clean', 'quiet', 'noise', 'clipped', 'truncated', 'bgm')
IDLE_HEARTBEAT_MS = 100.0
PERSISTENT_CONTEXT_MS = 1000.0


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


def _snapshot_sample_ends(sample_count: int) -> tuple[int, ...]:
    """Return nominal cumulative capture points, without inventing file-end work.

    ``NativeAudio`` submits first at 300 ms and then at least 500 ms after the
    previous submission.  A file ending between those points does not itself
    trigger an inference; the transport heartbeat may only drain PCM that was
    actually captured but not yet analyzed.
    """
    if sample_count <= 0:
        return ()
    first = round(NativeAudio.MIN_AUDIO_MS * 48)
    interval = round(NativeAudio.INFERENCE_INTERVAL_MS * 48)
    if sample_count < first:
        return ()
    return tuple(range(first, sample_count + 1, interval))


def _stream_align(aligner: TargetConditionedAligner, mono: np.ndarray, *,
                  idle_drain: bool = False) -> tuple[dict, ...]:
    """Replay the product's cumulative scheduler, including inference backpressure.

    ``observed_at_ms`` is when the scheduler can submit the snapshot in the
    capture clock; ``completed_at_ms`` is when the resulting knowledge can be
    available after measured inference.  When PCM becomes idle, one final
    snapshot may analyze a previously unprocessed tail.  The heartbeat itself
    is not part of the acoustic evidence.
    """
    sample_count = len(mono)
    if sample_count <= 0:
        return ()
    audio_ms = sample_count / 48.0
    snapshots = []
    next_trigger_ms = float(NativeAudio.MIN_AUDIO_MS)
    previous_complete_ms = 0.0
    last_sample_end = 0

    def analyze(sample_end: int, trigger_ms: float, trigger: str) -> dict:
        chunk = _chunk(mono[:sample_end])
        started = time.perf_counter_ns()
        alignment = aligner.align((chunk,))
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        return {
            'capture_end_ms': sample_end / 48.0,
            'observed_at_ms': trigger_ms,
            'completed_at_ms': trigger_ms + elapsed_ms,
            'trigger': trigger,
            'alignment_ms': elapsed_ms,
            'alignment': alignment,
        }

    while next_trigger_ms <= audio_ms + 1e-9:
        trigger_ms = max(next_trigger_ms, previous_complete_ms)
        if trigger_ms > audio_ms + 1e-9:
            break
        sample_end = min(sample_count, max(1, round(trigger_ms * 48)))
        if sample_end <= last_sample_end:
            break
        snapshot = analyze(sample_end, trigger_ms, 'cadence')
        snapshots.append(snapshot)
        last_sample_end = sample_end
        previous_complete_ms = snapshot['completed_at_ms']
        # NativeAudio owns cadence from the actual submission time, not from a
        # theoretical grid that elapsed while an earlier inference was busy.
        next_trigger_ms = trigger_ms + float(NativeAudio.INFERENCE_INTERVAL_MS)

    if idle_drain and last_sample_end < sample_count:
        # The helper emits health heartbeats at roughly 100 ms intervals after
        # PCM becomes idle.  Model the first heartbeat at which the single
        # inference worker can accept the unprocessed tail.
        behind_ms = max(0.0, previous_complete_ms - audio_ms)
        heartbeat_ticks = max(1, int(math.ceil(behind_ms / IDLE_HEARTBEAT_MS)))
        trigger_ms = audio_ms + heartbeat_ticks * IDLE_HEARTBEAT_MS
        snapshot = analyze(sample_count, trigger_ms, 'idle_drain')
        snapshots.append(snapshot)

    return tuple(snapshots)


def _result_row(sample: dict, condition: str, repetition: int,
                snapshots: tuple[dict, ...], source_audio_ms: float,
                observation_horizon_ms: float, oracle: dict) -> dict:
    oracle_end_ms = float(oracle['speech_end_ms'])
    oracle_uncertainty_ms = float(oracle['uncertainty_ms'])
    if snapshots:
        final_snapshot = snapshots[-1]
        final_alignment = final_snapshot['alignment']
        first_association = next((item for item in snapshots
                                  if item['alignment'].target_associated), None)
        first_commit = next((item for item in snapshots
                             if item['alignment'].committed_end), None)
    else:
        final_snapshot = None
        final_alignment = None
        first_association = None
        first_commit = None

    evidence = first_commit['alignment'] if first_commit is not None else final_alignment
    fit = evidence.fit if evidence is not None else None
    end_ms = None
    if first_commit is not None and evidence.target_end_frame is not None:
        end_ms = evidence.target_end_frame * evidence.frame_ms
    endpoint_error_ms = end_ms - oracle_end_ms if end_ms is not None else None
    association_capture_ms = (first_association['capture_end_ms']
                              if first_association is not None else None)
    association_trigger_ms = (first_association['observed_at_ms']
                              if first_association is not None else None)
    association_at_ms = (first_association['completed_at_ms']
                         if first_association is not None else None)
    commit_capture_ms = first_commit['capture_end_ms'] if first_commit is not None else None
    commit_trigger_ms = first_commit['observed_at_ms'] if first_commit is not None else None
    commit_at_ms = first_commit['completed_at_ms'] if first_commit is not None else None
    commit_delay_ms = commit_at_ms - oracle_end_ms if commit_at_ms is not None else None
    capture_commit_delay_ms = (commit_capture_ms - oracle_end_ms
                               if commit_capture_ms is not None else None)
    cumulative_alignment_ms = sum(item['alignment_ms'] for item in snapshots)
    final_alignment_ms = final_snapshot['alignment_ms'] if final_snapshot is not None else None
    max_alignment_ms = max((item['alignment_ms'] for item in snapshots), default=None)

    return {
        'id': sample['id'],
        'condition': condition,
        'round': repetition,
        'audio_ms': round(source_audio_ms, 3),
        'observation_horizon_ms': round(observation_horizon_ms, 3),
        'snapshot_count': len(snapshots),
        'final_alignment_ms': round(final_alignment_ms, 3) if final_alignment_ms is not None else None,
        'max_alignment_ms': round(max_alignment_ms, 3) if max_alignment_ms is not None else None,
        'cumulative_alignment_ms': round(cumulative_alignment_ms, 3),
        'final_rtf': (round(final_alignment_ms / final_snapshot['capture_end_ms'], 4)
                      if final_alignment_ms is not None and final_snapshot
                      and final_snapshot['capture_end_ms'] else None),
        'cumulative_compute_ratio': (round(cumulative_alignment_ms / observation_horizon_ms, 4)
                                     if observation_horizon_ms else None),
        'target_units': final_alignment.target_units if final_alignment is not None else 0,
        'aligned_target_units': final_alignment.aligned_target_units if final_alignment is not None else 0,
        'target_coverage': round(final_alignment.coverage, 4) if final_alignment is not None else 0.0,
        'target_associated': first_association is not None,
        'association_capture_end_ms': (round(association_capture_ms, 3)
                                       if association_capture_ms is not None else None),
        'association_trigger_at_ms': (round(association_trigger_ms, 3)
                                      if association_trigger_ms is not None else None),
        'association_observed_at_ms': (round(association_at_ms, 3)
                                       if association_at_ms is not None else None),
        'complete_alignment': bool(final_alignment and final_alignment.complete_alignment),
        'committed_end': first_commit is not None,
        'commit_capture_end_ms': (round(commit_capture_ms, 3)
                                  if commit_capture_ms is not None else None),
        'commit_trigger_at_ms': (round(commit_trigger_ms, 3)
                                 if commit_trigger_ms is not None else None),
        'commit_observed_at_ms': round(commit_at_ms, 3) if commit_at_ms is not None else None,
        'commit_alignment_ms': (round(first_commit['alignment_ms'], 3)
                                if first_commit is not None else None),
        'commit_delay_ms': round(commit_delay_ms, 3) if commit_delay_ms is not None else None,
        'capture_commit_delay_ms': (round(capture_commit_delay_ms, 3)
                                    if capture_commit_delay_ms is not None else None),
        'target_end_ms': round(end_ms, 3) if end_ms is not None else None,
        'oracle_end_ms': round(oracle_end_ms, 3),
        'oracle_uncertainty_ms': round(oracle_uncertainty_ms, 3),
        'endpoint_error_ms': round(endpoint_error_ms, 3) if endpoint_error_ms is not None else None,
        'early_commit': bool(commit_capture_ms is not None
                             and commit_capture_ms < oracle_end_ms - oracle_uncertainty_ms),
        'early_decision': bool(commit_at_ms is not None
                               and commit_at_ms < oracle_end_ms - oracle_uncertainty_ms),
        'late_commit': bool(commit_at_ms is not None
                            and commit_at_ms > oracle_end_ms + oracle_uncertainty_ms),
        'early_boundary': bool(endpoint_error_ms is not None
                               and endpoint_error_ms < -oracle_uncertainty_ms),
        'late_boundary': bool(endpoint_error_ms is not None
                              and endpoint_error_ms > oracle_uncertainty_ms),
        'endpoint_within_oracle': bool(endpoint_error_ms is not None
                                       and abs(endpoint_error_ms) <= oracle_uncertainty_ms),
        'post_target_free_frames': evidence.post_target_free_frames if evidence is not None else 0,
        'reading_rank': evidence.reading_rank if evidence is not None else -1,
        'path_margin': round(fit.path_margin, 4) if fit is not None else None,
        'mean_unit_peak_margin': round(fit.mean_unit_peak_margin, 4) if fit is not None else None,
        'min_unit_peak_margin': round(fit.min_unit_peak_margin, 4) if fit is not None else None,
        'weak_units': fit.weak_units if fit is not None else 0,
        'boundary_margin': round(fit.boundary_margin, 4) if fit is not None else None,
        'rejection': evidence.rejection if evidence is not None else 'insufficient_audio_for_first_snapshot',
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
            source_audio_ms = len(mono) / 48.0
            idle_drain = condition != 'bgm'
            if condition == 'bgm':
                tail = np.zeros(round(PERSISTENT_CONTEXT_MS * 48), dtype='float32')
                degraded = _degrade(np.concatenate((mono, tail)), condition, seed + index, bgm)
            else:
                degraded = _degrade(mono, condition, seed + index, bgm)
            observation_horizon_ms = len(degraded) / 48.0
            aligner = TargetConditionedAligner(backend, g2p=pronunciation)
            aligner.set_target(sample['text'])
            for repetition in range(1, rounds + 1):
                snapshots = _stream_align(aligner, degraded, idle_drain=idle_drain)
                results.append(_result_row(
                    sample, condition, repetition, snapshots, source_audio_ms,
                    observation_horizon_ms, endpoint_oracle[sample['id']]))

    by_condition = {}
    for condition in conditions:
        rows = [row for row in results if row['condition'] == condition]
        by_condition[condition] = {
            'final_alignment_ms': _summary([row['final_alignment_ms'] for row in rows
                                             if row['final_alignment_ms'] is not None]),
            'max_alignment_ms': _summary([row['max_alignment_ms'] for row in rows
                                           if row['max_alignment_ms'] is not None]),
            'cumulative_alignment_ms': _summary([row['cumulative_alignment_ms'] for row in rows]),
            'final_rtf': _summary([row['final_rtf'] for row in rows if row['final_rtf'] is not None]),
            'cumulative_compute_ratio': _summary([row['cumulative_compute_ratio'] for row in rows
                                                  if row['cumulative_compute_ratio'] is not None]),
            'target_coverage': _summary([row['target_coverage'] for row in rows]),
            'associated': sum(row['target_associated'] for row in rows),
            'complete': sum(row['complete_alignment'] for row in rows),
            'committed': sum(row['committed_end'] for row in rows),
            'unconfirmed': sum(not row['committed_end'] for row in rows),
            'early_commits': sum(row['early_commit'] for row in rows),
            'early_decisions': sum(row['early_decision'] for row in rows),
            'late_commits': sum(row['late_commit'] for row in rows),
            'early_boundaries': sum(row['early_boundary'] for row in rows),
            'late_boundaries': sum(row['late_boundary'] for row in rows),
            'endpoint_within_oracle': sum(row['endpoint_within_oracle'] for row in rows),
            'endpoint_error_ms': _summary([row['endpoint_error_ms'] for row in rows
                                           if row['committed_end'] and row['endpoint_error_ms'] is not None]),
            'commit_delay_ms': _summary([row['commit_delay_ms'] for row in rows
                                         if row['commit_delay_ms'] is not None]),
            'capture_commit_delay_ms': _summary([row['capture_commit_delay_ms'] for row in rows
                                                 if row['capture_commit_delay_ms'] is not None]),
            'runs': len(rows),
        }
    return {
        'schema': 'autoadv-alignment-benchmark-v3',
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
