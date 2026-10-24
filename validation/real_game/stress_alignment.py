"""Opt-in target-conditioned alignment stress measurement.

Real foreground/background/nonlexical recordings are mixed deterministically and
passed through the same production phone-CTC alignment contract.  The report
never claims speaker ownership from lexical equality and never drives game input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np

from autoadv.audio_buffer import PCMChunk
from autoadv.forced_alignment import OnnxPhoneCTCModel, TargetConditionedAligner
from autoadv.g2p import default_g2p
from autoadv.native_runtime import managed_paths
from .benchmark_alignment import _load_pcm
from .verify_trace import _summary

HERE = Path(__file__).resolve().parent
STRESS = HERE / 'ALIGNMENT_STRESS.json'
VOICE = HERE / 'renpy' / 'game' / 'audio' / 'voice'


def _load_cases(*, selection: str = 'all'):
    data = json.loads(STRESS.read_text(encoding='utf-8'))
    manifest = json.loads((HERE / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
    known = {sample['id']: sample for sample in manifest['samples']}
    external = {sample['id']: sample for sample in data['external_assets']}
    if selection not in ('all', 'background', 'nonlexical'):
        raise ValueError(f'Unknown selection: {selection}')
    cases = [case for case in data['cases'] if selection == 'all' or
             (selection == 'background' and case['kind'] not in ('nonlexical', 'nonlexical_overlap')) or
             (selection == 'nonlexical' and case['kind'] in ('nonlexical', 'nonlexical_overlap'))]
    if not cases:
        raise ValueError('No cases selected')
    audio = {}
    ids = {source for case in cases for source in (case['foreground'], case['background']) if source}
    for source_id in sorted(ids):
        if source_id in known:
            sample = known[source_id]
            audio[source_id] = _load_pcm(HERE / 'renpy' / 'game' / sample['path'], sample['sha256'])
        elif source_id in external:
            asset = external[source_id]
            file = VOICE / asset['filename']
            if not file.is_file():
                raise FileNotFoundError(
                    f'Actual nonverbal audio required: {file}. The pinned CC0 voice fixture is missing')
            audio[source_id] = _load_pcm(file, asset['prepared_sha256'])
        else:
            raise ValueError(f'Unknown voice recording in alignment stress scenario: {source_id}')
    return cases, audio


def _mix(case: dict, audio: dict) -> np.ndarray:
    foreground = audio.get(case['foreground'])
    background = audio.get(case['background'])
    if foreground is None and background is None:
        raise ValueError('Case has no actual audio source')
    offset = round(case['background_offset_ms'] * 48)
    loop_frames = round(case.get('background_loop_ms', 0) * 48)
    if loop_frames and background is None:
        raise ValueError('background loop requires a recording')
    length = loop_frames if loop_frames else (len(background) if background is not None else 0)
    end = max(len(foreground) if foreground is not None else 0, offset + length)
    out = np.zeros(end, dtype='float32')
    if foreground is not None:
        out[:len(foreground)] += foreground
    if background is not None:
        if loop_frames:
            for pos in range(offset, offset + loop_frames, len(background)):
                end_frame = min(pos + len(background), offset + loop_frames)
                out[pos:end_frame] += background[:end_frame - pos] * case['background_gain']
        else:
            out[offset:offset + len(background)] += background * case['background_gain']
    return np.clip(out, -1, 1)


def run(model: Path, vocab: Path, *, selection: str = 'all', rounds: int = 1,
        model_backend=None, g2p=None) -> dict:
    if rounds < 1:
        raise ValueError('rounds must be >= 1')
    cases, audio = _load_cases(selection=selection)
    started = time.perf_counter_ns()
    backend = model_backend if model_backend is not None else OnnxPhoneCTCModel(model, vocab)
    loaded_ms = round((time.perf_counter_ns() - started) / 1e6, 3)
    pronunciation = g2p if g2p is not None else default_g2p()
    observations = []
    for case in cases:
        mono = _mix(case, audio)
        frames = np.column_stack((mono, mono)).astype('<f4').tobytes()
        chunk = PCMChunk(round(len(mono) / 48), frames, 48000, 2, 4)
        aligner = TargetConditionedAligner(backend, g2p=pronunciation)
        try:
            aligner.set_target(case['display'])
        except ValueError as exc:
            for repetition in range(1, rounds + 1):
                observations.append({
                    'case': case['id'], 'kind': case['kind'], 'attribution': case['attribution'],
                    'round': repetition, 'audio_ms': round(chunk.duration_ms, 3),
                    'alignment_ms': 0.0, 'rtf': 0.0,
                    'target_associated': False, 'complete_alignment': False,
                    'committed_end': False, 'target_coverage': 0.0,
                    'rejection': f'target_unrepresentable:{exc}',
                })
            continue
        for repetition in range(1, rounds + 1):
            begin = time.perf_counter_ns()
            alignment = aligner.align((chunk,))
            duration = (time.perf_counter_ns() - begin) / 1e6
            observations.append({
                'case': case['id'], 'kind': case['kind'], 'attribution': case['attribution'],
                'round': repetition, 'audio_ms': round(chunk.duration_ms, 3),
                'alignment_ms': round(duration, 3),
                'rtf': round(duration / chunk.duration_ms, 4),
                'target_associated': alignment.target_associated,
                'complete_alignment': alignment.complete_alignment,
                'committed_end': alignment.committed_end,
                'target_coverage': round(alignment.coverage, 4),
                'target_units': alignment.target_units,
                'aligned_target_units': alignment.aligned_target_units,
                'reading_rank': alignment.reading_rank,
                'path_margin': round(alignment.fit.path_margin, 4),
                'boundary_margin': round(alignment.fit.boundary_margin, 4),
                'rejection': alignment.rejection,
            })
    return {
        'schema': 'autoadv-alignment-stress-v1',
        'device': platform.platform(), 'python': platform.python_version(),
        'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest() if model.is_file() else None,
        'vocab_sha256': hashlib.sha256(vocab.read_bytes()).hexdigest() if vocab.is_file() else None,
        'model_load_ms': loaded_ms, 'selection': selection,
        'recordings_sha256': {
            name: hashlib.sha256(mono.astype('<f4').tobytes()).hexdigest()
            for name, mono in audio.items()
        },
        'cases': [
            {**case,
             'alignment': _summary([o['alignment_ms'] for o in observations if o['case'] == case['id']]),
             'rtf': _summary([o['rtf'] for o in observations if o['case'] == case['id']]),
             'associated': sum(bool(o['target_associated']) for o in observations if o['case'] == case['id']),
             'committed': sum(bool(o['committed_end']) for o in observations if o['case'] == case['id'])}
            for case in cases
        ],
        'observations': observations,
        'real_game_verified': False,
    }


def contract_failures(result: dict) -> list[str]:
    """Return only product-critical real-model failures with an unambiguous oracle.

    Overlapping or same-text background speech is intentionally diagnostic: the
    process mix does not contain speaker ownership evidence.  The two cases
    below do have a valid oracle for this backend contract: a clean target must
    be alignable, while unrelated background-only speech must not be promoted
    into target association or a committed endpoint.
    """
    failures: list[str] = []
    cases = {case['id']: case for case in result.get('cases', [])}
    clean = cases.get('clean_control')
    mismatch = cases.get('background_only_mismatch')
    # ``run`` stores aggregate success counts rather than the requested round
    # count. Recover the actual count from observations to avoid inferring a
    # passing denominator from success itself.
    observations = result.get('observations', [])
    clean_runs = sum(row.get('case') == 'clean_control' for row in observations)
    mismatch_runs = sum(row.get('case') == 'background_only_mismatch' for row in observations)
    if clean is None or clean_runs == 0:
        failures.append('clean_control missing')
    else:
        if int(clean.get('associated', 0)) != clean_runs:
            failures.append('clean target was not associated on every run')
        if int(clean.get('committed', 0)) != clean_runs:
            failures.append('clean target end was not committed on every run')
    if mismatch is None or mismatch_runs == 0:
        failures.append('background_only_mismatch missing')
    else:
        if int(mismatch.get('associated', 0)) != 0:
            failures.append('unrelated background speech was associated with target')
        if int(mismatch.get('committed', 0)) != 0:
            failures.append('unrelated background speech committed a target end')
    return failures


def main() -> int:
    paths = managed_paths()
    parser = argparse.ArgumentParser(description='Target-conditioned real-audio stress evaluation')
    parser.add_argument('--model', type=Path, default=Path(paths.alignment_model))
    parser.add_argument('--vocab', type=Path, default=Path(paths.alignment_vocab))
    parser.add_argument('--selection', choices=('all', 'background', 'nonlexical'), default='all')
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--assert-contract', action='store_true')
    args = parser.parse_args()
    result = run(args.model, args.vocab, selection=args.selection, rounds=args.rounds)
    print(json.dumps({k: v for k, v in result.items() if k != 'observations'},
                     ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.assert_contract:
        failures = contract_failures(result)
        if failures:
            raise SystemExit('Alignment contract failed: ' + '; '.join(failures))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
