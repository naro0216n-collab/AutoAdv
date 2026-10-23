"""Select independent real-audio validation cases from the canonical scenario."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIO = HERE / 'SCENARIO.json'


@dataclass(frozen=True)
class RealAudioCase:
    id: str
    start_line: int


def real_audio_cases(scenario_path: Path = SCENARIO) -> tuple[RealAudioCase, ...]:
    lines = json.loads(scenario_path.read_text(encoding='utf-8'))['lines']
    cases = []
    for index, line in enumerate(lines, 1):
        if (line.get('advance_mode') == 'audio'
                and not line.get('use_tts')
                and line.get('foreground_voice') is True
                and line.get('foreground_file')):
            cases.append(RealAudioCase(str(line['id']), index))
    return tuple(cases)


def main() -> int:
    print(json.dumps([asdict(case) for case in real_audio_cases()],
                     ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
