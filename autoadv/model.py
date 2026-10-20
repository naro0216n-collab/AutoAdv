"""The product model. Persisted profiles contain no volatile process identifiers."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import re


class AudioMode(str, Enum):
    NATIVE = 'native'
    TTS = 'tts'
    AUTO = 'auto'
    READING = 'reading'


@dataclass(frozen=True)
class HookSelection:
    code: str
    context: int
    context2: int
    role: str = 'text'

    def __post_init__(self):
        if not self.code or min(self.context, self.context2) < 0 or self.role not in ('text', 'speaker'):
            raise ValueError('invalid selected HOOK')

    def matches(self, source: object) -> bool:
        return (getattr(source, 'hook_code', None) == self.code
                and getattr(source, 'context', -1) == self.context
                and getattr(source, 'context2', -1) == self.context2)


@dataclass
class Profile:
    id: str
    name: str
    hooks: list[HookSelection] = field(default_factory=list)
    default_mode: AudioMode = AudioMode.READING
    default_voice: str = ''
    speaker_modes: dict[str, AudioMode] = field(default_factory=dict)
    speaker_voices: dict[str, str] = field(default_factory=dict)
    replacements: dict[str, str] = field(default_factory=dict)
    reading_ms: int = 1200
    settle_ms: int = 180
    repeat_rule: str = 'distinct_only'  # Or a verified one-notification-per-line HOOK
    input_key: int = 13

    def validate(self) -> None:
        if not self.id or not self.name or self.reading_ms < 0 or self.settle_ms < 0:
            raise ValueError('invalid profile')
        if self.repeat_rule not in ('distinct_only', 'each_after_input') or not 1 <= self.input_key <= 254:
            raise ValueError('invalid advancement settings')
        if len(self.hooks) != len({(h.code, h.context, h.context2) for h in self.hooks}):
            raise ValueError('one HOOK cannot have multiple conflicting roles')
        if not any(h.role == 'text' for h in self.hooks):
            raise ValueError('select at least one text HOOK before monitoring')


@dataclass
class Game:
    id: str
    name: str
    executable: str = ''
    profiles: dict[str, Profile] = field(default_factory=dict)
    default_profile: str = ''


@dataclass(frozen=True)
class Utterance:
    session: int
    line: int
    source: str
    sequence: int
    raw: str
    speaker: str
    text: str
    reading: str
    match_text: str
    mode: AudioMode
    voice: str


_NAME_LINE = re.compile(r'^\s*([^\s「」『』：:]{1,32})\s*[：:]\s*(.+)$', re.S)
_QUOTED = re.compile(r'^\s*([^「」『』\n]{1,32})[「『](.+)[」』]\s*$', re.S)
_RUBY = re.compile(r'([\u3400-\u9fff]+)\(([^()]*)\)')


def interpret(raw: str, profile: Profile, *, session: int, line: int, source: str,
              sequence: int, side_speaker: str = '') -> Utterance:
    speaker, text = side_speaker.strip(), raw.strip()
    for expression in (_QUOTED, _NAME_LINE):
        result = expression.fullmatch(text)
        if result:
            if not speaker:
                speaker = result.group(1).strip()
            text = result.group(2).strip()
            break
    # Ruby is a replacement for the original kanji, not extra spoken text.
    reading = _RUBY.sub(lambda m: m.group(2), text)
    for before, after in profile.replacements.items():
        if before:
            reading = reading.replace(before, after)
    mode = profile.speaker_modes.get(speaker, profile.default_mode)
    voice = profile.speaker_voices.get(speaker, profile.default_voice)
    return Utterance(session, line, source, sequence, raw, speaker, text,
                     reading, text, AudioMode(mode), voice)
