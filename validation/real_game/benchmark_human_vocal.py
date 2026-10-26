"""Measure the managed human-vocal observers on isolated fixture waveforms.

This is a diagnostic evidence layer, not a product acceptance gate.  It answers
whether the observers themselves expose useful score support for representative
human-vocal material before integration, game mixing, causal association, or
Controller policy are involved.  The live accuracy suite remains the authority
for end-to-end advancement behaviour.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import wave

import numpy as np

from autoadv.acoustic_observers import (
    AUDIOSET_HUMAN_VOCAL_CLASSES, HumanVocalActivityObserver, VocalActivityFrame,
)
from autoadv.audio_buffer import PCMChunk
from autoadv.native_runtime import managed_paths

THRESHOLDS = (0.05, 0.10, 0.20, 0.30, 0.45, 0.60)


def _load_chunk(path: Path) -> PCMChunk:
    with wave.open(str(path), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (48000, 1, 2):
            raise ValueError(f"unsupported diagnostic WAV format: {path.name}")
        mono = (np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2")
                .astype(np.float32) / 32768.0)
    stereo = np.column_stack((mono, mono)).astype("<f4", copy=False)
    return PCMChunk(
        at_ms=int(round(len(mono) * 1000 / 48000)),
        frames=stereo.tobytes(), sample_rate=48000, channels=2, sample_width_bytes=4,
    )


def _intervals(frames: list[VocalActivityFrame], threshold: float) -> list[list[int]]:
    selected = [frame for frame in frames if frame.score >= threshold]
    if not selected:
        return []
    times = [frame.at_ms for frame in frames]
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    nominal_gap = float(np.median(gaps)) if gaps else 16.0
    join_gap = max(32.0, nominal_gap * 2.0)
    result: list[list[int]] = []
    for frame in selected:
        start = int(frame.support_start_ms if frame.support_start_ms is not None else frame.at_ms)
        end = int(frame.at_ms)
        if result and start - result[-1][1] <= join_gap:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def summarize_frames(frames: tuple[VocalActivityFrame, ...]) -> dict:
    by_observer: dict[str, list[VocalActivityFrame]] = {}
    for frame in frames:
        by_observer.setdefault(frame.observer, []).append(frame)
    result = {}
    for observer, items in sorted(by_observer.items()):
        scores = np.asarray([frame.score for frame in items], dtype=np.float64)
        thresholds = {}
        for threshold in THRESHOLDS:
            active = scores >= threshold
            thresholds[f"{threshold:.2f}"] = {
                "active_fraction": round(float(np.mean(active)), 6),
                "active_count": int(np.sum(active)),
                "intervals_ms": _intervals(items, threshold),
            }
        component_summary = {}
        component_rows = [item.component_scores for item in items if item.component_scores]
        if component_rows:
            matrix = np.asarray(component_rows, dtype=np.float64)
            for column, class_index in enumerate(AUDIOSET_HUMAN_VOCAL_CLASSES):
                values = matrix[:, column]
                component_summary[str(class_index)] = {
                    "peak_score": round(float(np.max(values)), 6),
                    "mean_score": round(float(np.mean(values)), 6),
                    "p95_score": round(float(np.quantile(values, 0.95)), 6),
                }
        result[observer] = {
            "frame_count": len(items),
            "peak_score": round(float(np.max(scores)), 6) if scores.size else 0.0,
            "mean_score": round(float(np.mean(scores)), 6) if scores.size else 0.0,
            "quantiles": ({
                "p50": round(float(np.quantile(scores, 0.50)), 6),
                "p90": round(float(np.quantile(scores, 0.90)), 6),
                "p95": round(float(np.quantile(scores, 0.95)), 6),
                "p99": round(float(np.quantile(scores, 0.99)), 6),
            } if scores.size else {}),
            "thresholds": thresholds,
            "component_scores": component_summary,
        }
    return result


def run(*, voice_root: Path, model: Path) -> dict:
    files = sorted(path for path in voice_root.glob("*.wav") if path.is_file())
    rows = []
    for path in files:
        observer = HumanVocalActivityObserver(model)
        chunk = _load_chunk(path)
        frames = observer.observe(chunk)
        rows.append({
            "file": path.name,
            "duration_ms": round(chunk.duration_ms, 3),
            "observers": summarize_frames(frames),
        })
    return {
        "schema": "autoadv-human-vocal-observer-diagnostic-v1",
        "evidence_layer": "isolated_fixture_observer_measurement",
        "product_acceptance": False,
        "platform": platform.platform(),
        "model": str(model),
        "threshold_grid": list(THRESHOLDS),
        "files": rows,
    }


def main() -> int:
    paths = managed_paths()
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice-root", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=Path(paths.human_vocal_model))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(voice_root=args.voice_root, model=args.model)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
