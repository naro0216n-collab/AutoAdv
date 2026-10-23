"""Opt-in real-model ASR investigation for nonlexical and competing voices.

Uses production JapaneseASR, real recordings, deterministic mixtures. This is
not a source-separation or a second AutoAdv audio controller: it records raw
recognizer and G2P-alignment measurements only.  Advancement behavior still
requires the live product path.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import platform
import time

from autoadv.audio_buffer import PCMChunk
from autoadv.audio_match import TargetMatcher, surface_key
from autoadv.native_audio import JapaneseASR
from .benchmark_asr import _edit_distance, _load_pcm
from .verify_trace import _summary
HERE = Path(__file__).resolve().parent
STRESS = HERE / 'ASR_STRESS.json'
VOICE = HERE / 'renpy' / 'game' / 'audio' / 'voice'
from autoadv.native_runtime import managed_paths


def _load_cases(*, selection: str = 'all'):
    data = json.loads(STRESS.read_text(encoding='utf-8'))
    manifest = json.loads((HERE / 'VOICE_MANIFEST.json').read_text(encoding='utf-8'))
    known = {s['id']: s for s in manifest['samples']}
    external = {s['id']: s for s in data['external_assets']}
    if selection not in ('all', 'background', 'nonlexical'):
        raise ValueError(f'Unknown selection: {selection}')
    cases = [case for case in data['cases'] if selection == 'all' or
             (selection == 'background' and case['kind'] not in ('nonlexical', 'nonlexical_overlap')) or
             (selection == 'nonlexical' and case['kind'] in ('nonlexical', 'nonlexical_overlap'))]
    if not cases:
        raise ValueError('No cases selected')
    audio = {}
    ids = {item for c in cases for item in (c['foreground'], c['background']) if item}
    for source_id in sorted(ids):
        if source_id in known:
            sample = known[source_id]
            file = HERE / 'renpy' / 'game' / sample['path']
            audio[source_id] = _load_pcm(file, sample['sha256'])
        elif source_id in external:
            file = VOICE / external[source_id]['filename']
            if not file.is_file():
                raise FileNotFoundError(f'Actual nonverbal audio required: {file}. '
                                        'The pinned CC0 voice fixture is missing')
            audio[source_id] = _load_pcm(file, external[source_id]['prepared_sha256'])
        else:
            raise ValueError(f'Unknown voice recording in ASR stress scenario: {source_id}')
    return cases, audio


def _mix(case: dict, audio: dict):
    import numpy as np
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
            # Repeated *human voice* with its recorded silence. Avoid a
            # synthesized noise stand-in and avoid extending a huge WAV.
            for pos in range(offset, offset + loop_frames, len(background)):
                end_frame = min(pos + len(background), offset + loop_frames)
                out[pos:end_frame] += background[:end_frame - pos] * case['background_gain']
        else:
            out[offset:offset + len(background)] += background * case['background_gain']
    return np.clip(out, -1, 1)



def run(model: Path, tokens: Path, *, selection: str = 'all', rounds: int = 1) -> dict:
    import numpy as np
    if rounds < 1:
        raise ValueError('rounds must be >= 1')
    cases, audio = _load_cases(selection=selection)  # fail, never skip missing vocalizations
    start = time.perf_counter_ns()
    recognizer = JapaneseASR(model, tokens)
    loaded_ms = round((time.perf_counter_ns() - start) / 1e6, 3)
    observations = []
    for case in cases:
        mono = _mix(case, audio)
        frames = np.column_stack((mono, mono)).astype('<f4').tobytes()
        chunk = PCMChunk(0, frames, 48000, 2, 4)
        for repetition in range(rounds):
            started = time.perf_counter_ns()
            hypothesis = recognizer.recognize((chunk,))
            duration = (time.perf_counter_ns() - started) / 1e6
            transcript, target = surface_key(hypothesis.text), surface_key(case['display'])
            alignment = TargetMatcher(case['display']).align(
                hypothesis.text, tokens=hypothesis.tokens)
            lexical = case['kind'] == 'lexical'
            observations.append({
                'case': case['id'], 'kind': case['kind'], 'attribution': case['attribution'],
                'round': repetition + 1, 'audio_ms': round(chunk.duration_ms, 3),
                'decode_ms': round(duration, 3), 'rtf': round(duration / chunk.duration_ms, 4),
                'transcript_hash': hashlib.sha256(transcript.encode('utf-8')).hexdigest(),
                'transcript': hypothesis.text,  # real recognizer output, not inferred from its hash
                'recognized_characters': len(transcript),
                'phonetic_coverage': round(alignment.phonetic_coverage, 4),
                'phonetic_matches': alignment.phonetic_matches,
                'phonetic_target_units': alignment.phonetic_target_units,
                'phonetic_edit_count': alignment.phonetic_edit_count,
                'reading_rank': alignment.reading_rank,
                'cer': round(_edit_distance(target, transcript) / max(1, len(target)), 4)
                        if lexical else None,
            })
    return {
        'device': platform.platform(), 'python': platform.python_version(),
        'model_sha256': hashlib.sha256(model.read_bytes()).hexdigest(),
        'tokens_sha256': hashlib.sha256(tokens.read_bytes()).hexdigest(),
        'model_load_ms': loaded_ms, 'selection': selection,
        'recordings_sha256': {name: hashlib.sha256(mono.astype('<f4').tobytes()).hexdigest()
                              for name, mono in audio.items()},
        'cases': [{**case, 'decode': _summary([o['decode_ms'] for o in observations if o['case'] == case['id']]),
                   'rtf': _summary([o['rtf'] for o in observations if o['case'] == case['id']])}
                  for case in cases],
        'observations': observations,
        # Benchmark metrics are evidence, not a fabricated PASS for real game input.
        'real_game_verified': False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Real JapaneseASR stress evaluation; explicit, non-CI')
    parser.add_argument('--model', type=Path, default=Path(managed_paths().asr_model))
    parser.add_argument('--tokens', type=Path, default=Path(managed_paths().asr_tokens))
    parser.add_argument('--selection', choices=('all', 'background', 'nonlexical'), default='all')
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run(args.model, args.tokens, selection=args.selection, rounds=args.rounds)
    print(json.dumps({k: v for k, v in result.items() if k != 'observations'}, ensure_ascii=False, indent=2))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
