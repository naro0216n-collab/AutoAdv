"""Opt-in preparation of CC0 human vocalizations, NOT part of AutoAdv runtime or CI.

Convert Wikimedia originals to 48 kHz 16-bit mono WAV for the independent
Ren'Py scenario and real-model ASR investigation. No synthetic "crying" audio.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
STRESS = HERE / 'ASR_STRESS.json'
VOICE = HERE / 'renpy' / 'game' / 'audio' / 'voice'


def prepare(*, ffmpeg: str = 'ffmpeg', out: Path = VOICE) -> dict:
    assets = json.loads(STRESS.read_text(encoding='utf-8'))['external_assets']
    out.mkdir(parents=True, exist_ok=True)
    report = []
    with TemporaryDirectory() as directory:
        for item in assets:
            path = out / item['filename']
            if path.exists():
                raise FileExistsError(f'Refusing to overwrite voice recording: {path}')
            # URLs and conversion parameters are committed, not supplied by an untrusted manifest.
            with urlopen(item['url'], timeout=45) as response:
                original = response.read(10 * 1024 * 1024 + 1)
            if not original or len(original) > 10 * 1024 * 1024:
                raise ValueError(f'Invalid original asset size: {item["id"]}')
            digest = hashlib.sha1(original).hexdigest()
            if item['source_sha1'] and digest != item['source_sha1']:
                raise ValueError(f'Original asset integrity mismatch: {item["id"]}')
            source = Path(directory) / (item['id'] + '.ogg')
            source.write_bytes(original)
            candidate = Path(directory) / (item['id'] + '.wav')
            subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                            '-i', str(source), '-ss', str(item['trim_start_s']),
                            '-t', str(item['trim_duration_s']), '-ar', '48000',
                            '-ac', '1', '-c:a', 'pcm_s16le', str(candidate)], check=True)
            import wave
            with wave.open(str(candidate), 'rb') as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (48000, 1, 2):
                    raise ValueError('Converted audio has unexpected format')
            candidate.replace(path)
            report.append({'id': item['id'], 'source_sha1': digest, 'prepared_sha256':
                          hashlib.sha256(path.read_bytes()).hexdigest(), 'path': str(path)})
    return {'assets': report}


def main() -> int:
    parser = argparse.ArgumentParser(description='Explicitly acquire CC0 cry and scream for ASR stress cases')
    parser.add_argument('--ffmpeg', default='ffmpeg')
    args = parser.parse_args()
    print(json.dumps(prepare(ffmpeg=args.ffmpeg), ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
