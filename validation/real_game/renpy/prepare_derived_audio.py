"""Build deterministic derived human-vocal fixtures from the licensed source clips.

These files exist only inside the prebuilt validation game.  They provide
single-source acoustic oracles for temporal classes that would otherwise need
multiple queued voice files (lexical + nonverbal tail, intermittent vocal
bursts, and finite recurrent speech).  AutoAdv never reads their labels.
"""
from __future__ import annotations

import argparse
from array import array
from pathlib import Path
import wave

RATE = 48_000
CHANNELS = 1
WIDTH = 2


def _read(path: Path) -> array:
    with wave.open(str(path), "rb") as reader:
        fmt = (reader.getframerate(), reader.getnchannels(), reader.getsampwidth())
        if fmt != (RATE, CHANNELS, WIDTH):
            raise ValueError(f"unsupported fixture WAV format {fmt}: {path}")
        values = array("h")
        values.frombytes(reader.readframes(reader.getnframes()))
        return values


def _silence(ms: int) -> array:
    return array("h", [0]) * int(round(RATE * ms / 1000))


def _slice(values: array, start_s: float, end_s: float, gain: float = 1.0) -> array:
    start = max(0, int(round(start_s * RATE)))
    end = min(len(values), int(round(end_s * RATE)))
    result = array("h", values[start:end])
    if gain != 1.0:
        for i, sample in enumerate(result):
            result[i] = max(-32768, min(32767, int(round(sample * gain))))
    return result


def _write(path: Path, pieces: list[array]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(CHANNELS)
        writer.setsampwidth(WIDTH)
        writer.setframerate(RATE)
        writer.writeframes(b"".join(piece.tobytes() for piece in pieces))


def build(voice_dir: Path) -> None:
    normal_001 = _read(voice_dir / "normal_001.wav")
    normal_020 = _read(voice_dir / "normal_020.wav")
    normal_030 = _read(voice_dir / "normal_030.wav")
    cry = _read(voice_dir / "stress_cry.wav")
    whisper = _read(voice_dir / "whisper_001.wav")

    # Same displayed lexical text as normal_001, but with a long human-vocal
    # tail.  One WAV keeps the independent acoustic endpoint knowable even if
    # AutoAdv advances before the tail would have started.
    _write(voice_dir / "derived_same_text_long_tail.wav", [
        normal_001, _silence(120), cry,
    ])

    # Same long lexical content as normal_030 at roughly twice the original
    # speaking rate.  Sample decimation intentionally changes acting duration
    # without changing the displayed text or adding a second source.
    _write(voice_dir / "derived_normal_030_fast.wav", [
        array("h", normal_030[::2]),
    ])

    # Intermittent human-vocal activity with internal quiet gaps.  The source
    # is real human vocal material; the fixture label describes the temporal
    # class, not a semantic classifier target.
    _write(voice_dir / "derived_panting_temporal.wav", [
        _slice(cry, .25, .72), _silence(110),
        _slice(cry, 1.10, 1.56, .90), _silence(170),
        _slice(cry, 2.05, 2.46, .82), _silence(90),
        _slice(cry, 3.02, 3.50, .76), _silence(210),
        _slice(cry, 4.35, 4.82, .68),
    ])

    # Breath/whisper-like bursts around an internal pause; useful for checking
    # that a brief inactive interval is not itself an endpoint.
    _write(voice_dir / "derived_breath_pause.wav", [
        _slice(whisper, .05, .60, .75), _silence(320),
        _slice(whisper, .63, 1.25, .65),
    ])

    # High self-similarity but finite and non-periodic in amplitude/gaps.  A
    # recurrence detector must not turn this natural finite repetition into a
    # persistent loop source.
    _write(voice_dir / "derived_finite_repetition.wav", [
        _slice(normal_020, 0, len(normal_020) / RATE), _silence(110),
        _slice(normal_020, 0, len(normal_020) / RATE, .82), _silence(240),
        _slice(normal_020, 0, len(normal_020) / RATE, .64),
    ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("voice_dir", type=Path)
    args = parser.parse_args()
    build(args.voice_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
