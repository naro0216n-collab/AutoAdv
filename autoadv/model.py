"""User-visible game/profile configuration and one utterance interpretation contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import re
import unicodedata


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

    @property
    def identity(self) -> str:
        return f'{self.code}|{self.context}|{self.context2}'

    def matches(self, source: object) -> bool:
        return (getattr(source, 'hook_code', None) == self.code
                and getattr(source, 'context', -1) == self.context
                and getattr(source, 'context2', -1) == self.context2)


@dataclass(frozen=True)
class StructureRule:
    """First matching named capture wins. Rules are ordered per profile."""
    pattern: str
    condition: str = ''
    enabled: bool = True

    def __post_init__(self):
        try:
            groups = re.compile(self.pattern, re.S).groupindex
            if 'body' not in groups or not set(groups).issubset({'body', 'speaker'}):
                raise ValueError('structure rule requires a named body and optional speaker')
            if self.condition:
                re.compile(self.condition, re.S)
        except re.error as exc:
            raise ValueError(f'invalid structure regex: {exc}') from exc


@dataclass(frozen=True)
class ReplacementRule:
    pattern: str
    replacement: str
    regex: bool = False
    target: str = 'tts'  # tts, asr or both
    enabled: bool = True

    def __post_init__(self):
        if not self.pattern or self.target not in ('tts', 'asr', 'both'):
            raise ValueError('invalid text replacement')
        if self.regex:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f'invalid replacement regex: {exc}') from exc


DEFAULT_STRUCTURE_RULES = (
    StructureRule(r'^\s*(?P<speaker>[^\s「」『』\n]{1,40})\s*[「『](?P<body>.*)[」』]\s*$'),
    StructureRule(r'^\s*(?P<speaker>[^\n]{1,40})\n(?P<body>.+)$'),
    StructureRule(r'^\s*(?P<speaker>[^\s：:\n]{1,40})\s*[：:]\s*(?P<body>.+)$'),
    StructureRule(r'^\s*[「『](?P<body>.*)[」』]\s*$'),
)


@dataclass
class OCRSettings:
    executable: str = 'tesseract'
    language: str = 'jpn'
    region: tuple[int, int, int, int] = (0, 0, 640, 180)
    interval_ms: int = 350

    def validate(self):
        if (not self.executable or not self.language or len(self.region) != 4
                or any(type(v) is not int for v in self.region)
                or min(self.region[:2]) < 0 or min(self.region[2:]) <= 0
                or not 200 <= self.interval_ms <= 5000):
            raise ValueError('invalid OCR configuration')


@dataclass
class Profile:
    id: str
    name: str
    hooks: list[HookSelection] = field(default_factory=list)
    inserted_hooks: list[str] = field(default_factory=list)
    speaker_links: dict[str, str] = field(default_factory=dict)
    default_mode: AudioMode = AudioMode.READING
    default_voice: str = ''
    speaker_modes: dict[str, AudioMode] = field(default_factory=dict)
    speaker_voices: dict[str, str] = field(default_factory=dict)
    structure_rules: list[StructureRule] = field(default_factory=lambda: list(DEFAULT_STRUCTURE_RULES))
    replacements: list[ReplacementRule] = field(default_factory=list)
    name_readings: dict[str, str] = field(default_factory=dict)
    reading_ms: int = 1200
    settle_ms: int = 180
    hook_flush_ms: int = 500
    hook_codepage: int = 932
    hook_max_buffer: int = 3000
    hook_max_history: int = 1000000
    repeat_rule: str = 'distinct_only'
    input_key: int = 13
    source: str = 'hook'
    ocr: OCRSettings = field(default_factory=OCRSettings)
    native_helper: str = ''
    asr_model: str = ''
    asr_tokens: str = ''
    native_wait_ms: int = 1200

    def validate(self) -> None:
        if not self.id or not self.name or self.reading_ms < 0 or self.settle_ms < 0:
            raise ValueError('invalid profile')
        if (not 0 <= self.hook_flush_ms <= 10000 or not 1 <= self.hook_codepage <= 65535
                or not 1 <= self.hook_max_buffer <= 1000000 or not 1 <= self.hook_max_history <= 10000000):
            raise ValueError('invalid Luna capture settings')
        if self.repeat_rule not in ('distinct_only', 'each_after_input') or not 1 <= self.input_key <= 254:
            raise ValueError('invalid advancement settings')
        if len(self.hooks) != len({(h.code, h.context, h.context2) for h in self.hooks}):
            raise ValueError('one HOOK cannot have multiple conflicting roles')
        selected_roles = {h.identity: h.role for h in self.hooks}
        for text_source, speaker_source in self.speaker_links.items():
            if selected_roles.get(text_source) != 'text' or selected_roles.get(speaker_source) != 'speaker':
                raise ValueError('speaker-to-text mapping requires selected HOOK roles')
        if self.source not in ('hook', 'ocr'):
            raise ValueError('invalid acquisition mode')
        if any(not code.strip() for code in self.inserted_hooks):
            raise ValueError('invalid manual HOOK')
        if any(not k or not v for k, v in self.name_readings.items()):
            raise ValueError('invalid name reading')
        if self.native_wait_ms < 0:
            raise ValueError('invalid native wait')
        self.ocr.validate()
        for rule in self.structure_rules:
            StructureRule(rule.pattern, rule.condition, rule.enabled)
        for rule in self.replacements:
            ReplacementRule(rule.pattern, rule.replacement, rule.regex, rule.target, rule.enabled)


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


_RUBY = re.compile(r'([\u3400-\u9fff]+)\(([\u3040-\u30ffー]{1,30})\)')
_KANJI = r'[\u3400-\u9fff]'


def interpret(raw: str, profile: Profile, *, session: int, line: int, source: str,
              sequence: int, side_speaker: str = '') -> Utterance:
    body, speaker = raw, side_speaker.strip()
    for rule in profile.structure_rules:
        if not rule.enabled or (rule.condition and not re.search(rule.condition, raw, re.S)):
            continue
        matched = re.fullmatch(rule.pattern, raw, re.S)
        if matched:
            body = matched.group('body')
            if not speaker:
                speaker = (matched.groupdict().get('speaker') or '').strip()
            break
    body = unicodedata.normalize('NFKC', body).strip()
    reading = _RUBY.sub(lambda m: m.group(2), body)
    for word, replacement in sorted(profile.name_readings.items(), key=lambda item: -len(item[0])):
        reading = re.sub(r'(?<!' + _KANJI + ')' + re.escape(word) + r'(?!' + _KANJI + ')',
                         lambda _: replacement, reading)
    match_text = body
    for rule in profile.replacements:
        if not rule.enabled:
            continue
        def replace(text):
            return (re.sub(rule.pattern, rule.replacement, text) if rule.regex else
                    text.replace(rule.pattern, rule.replacement))
        if rule.target in ('tts', 'both'):
            reading = replace(reading)
        if rule.target in ('asr', 'both'):
            match_text = replace(match_text)
    mode = AudioMode(profile.speaker_modes.get(speaker, profile.default_mode))
    voice = profile.speaker_voices.get(speaker, profile.default_voice)
    return Utterance(session, line, source, sequence, raw, speaker, body,
                     reading, match_text, mode, voice)
