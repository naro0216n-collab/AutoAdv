"""User-visible game/profile configuration and one utterance interpretation contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import re
import unicodedata


class AdvanceMode(str, Enum):
    TEXT = 'text'
    AUDIO = 'audio'


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
    # A colon in dialogue (notably https://) is not a speaker separator.
    # Unusual names can still be handled through a game's explicit rules.
    StructureRule(r'^\s*(?P<speaker>[^\s「」『』、。，．！？!?（）()：:\n]{1,40})\s*[：:]\s*(?!https?://)(?P<body>.+)$'),
    StructureRule(r'^\s*[「『](?P<body>.*)[」』]\s*$'),
)


@dataclass
class OCRSettings:
    executable: str = 'tesseract'
    language: str = 'jpn'
    region: tuple[int, int, int, int] = (0, 0, 640, 180)
    interval_ms: int = 350
    stable_samples: int = 2

    def validate(self):
        if (not self.executable or not self.language or len(self.region) != 4
                or any(type(v) is not int for v in self.region)
                or min(self.region[:2]) < 0 or min(self.region[2:]) <= 0
                or not 200 <= self.interval_ms <= 5000 or not 1 <= self.stable_samples <= 10):
            raise ValueError('invalid OCR configuration')


# Only these values can be inherited. The persisted profile owns the choice;
# the effective session profile is resolved from the catalog when monitoring begins.
COMMON_FIELDS = ('advance_mode', 'uncertain_text_fallback', 'default_tts',
                 'default_voice', 'reading_ms', 'per_character_wait_ms',
                 'native_helper', 'asr_model', 'asr_tokens', 'input_key',
                 'next_line_timeout_ms')


@dataclass
class Profile:
    id: str
    name: str
    hooks: list[HookSelection] = field(default_factory=list)
    inserted_hooks: list[str] = field(default_factory=list)
    speaker_links: dict[str, str] = field(default_factory=dict)
    advance_mode: AdvanceMode = AdvanceMode.TEXT
    uncertain_text_fallback: bool = False
    default_tts: bool = False
    default_voice: str = ''
    speaker_tts: dict[str, bool] = field(default_factory=dict)
    speaker_voices: dict[str, str] = field(default_factory=dict)
    structure_rules: list[StructureRule] = field(default_factory=lambda: list(DEFAULT_STRUCTURE_RULES))
    replacements: list[ReplacementRule] = field(default_factory=list)
    name_readings: dict[str, str] = field(default_factory=dict)
    reading_ms: int = 700
    per_character_wait_ms: int = 200
    hook_flush_ms: int = 500
    hook_codepage: int = 932
    hook_max_buffer: int = 3000
    hook_max_history: int = 1000000
    repeat_rule: str = 'distinct_only'
    input_key: int = 13
    next_line_timeout_ms: int = 8000
    source: str = 'hook'
    ocr: OCRSettings = field(default_factory=OCRSettings)
    native_helper: str = ''
    asr_model: str = ''
    asr_tokens: str = ''
    inherit_common: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.id or not self.name or type(self.reading_ms) is not int or self.reading_ms <= 0 or type(self.per_character_wait_ms) is not int or self.per_character_wait_ms <= 0:
            raise ValueError('invalid profile')
        if (not 0 <= self.hook_flush_ms <= 10000 or not 1 <= self.hook_codepage <= 65535
                or not 1 <= self.hook_max_buffer <= 1000000 or not 1 <= self.hook_max_history <= 10000000):
            raise ValueError('invalid Luna capture settings')
        if (self.repeat_rule not in ('distinct_only', 'each_after_input') or not 1 <= self.input_key <= 254
                or not 1000 <= self.next_line_timeout_ms <= 120000):
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
        if (type(self.uncertain_text_fallback) is not bool or type(self.default_tts) is not bool
                or any(type(v) is not bool or not k for k, v in self.speaker_tts.items())):
            raise ValueError('invalid TTS and fallback settings')
        AdvanceMode(self.advance_mode)
        if len(self.inherit_common) != len(set(self.inherit_common)) or any(
                name not in COMMON_FIELDS for name in self.inherit_common):
            raise ValueError('invalid common setting inheritance')
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
    audio_executable: str = ''
    input_executable: str = ''


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
    use_tts: bool
    voice: str
    advance_mode: AdvanceMode
    uncertain_text_fallback: bool
    character_count: int
    text_wait_ms: int


_RUBY = re.compile(r'([\u3400-\u9fff]+)\(([\u3040-\u30ffー]{1,30})\)')
_KANJI = r'[\u3400-\u9fff]'


def count_reading_characters(body: str) -> int:
    """Count display glyphs only, never expanded reading or duplicate ruby."""
    without_ruby = _RUBY.sub(lambda m: m.group(1), body)
    def countable(index: int, character: str) -> bool:
        if character == 'ー':
            # A long vowel mark by itself is punctuation-like. Within kana it
            # is a pronounced character even though Unicode categorizes it Lm.
            return index > 0 and ('\u3040' <= without_ruby[index-1] <= '\u30ff'
                                  or without_ruby[index-1] == 'ー')
        return unicodedata.category(character)[0] in 'LN'

    return sum(countable(i, c) for i, c in enumerate(without_ruby))


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
    match_text = _RUBY.sub(lambda m: m.group(1), body)
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
    use_tts = profile.speaker_tts.get(speaker, profile.default_tts)
    voice = profile.speaker_voices.get(speaker, profile.default_voice)
    count = count_reading_characters(body)
    return Utterance(session, line, source, sequence, raw, speaker, body,
                     reading, match_text, use_tts, voice,
                     AdvanceMode(profile.advance_mode), profile.uncertain_text_fallback,
                     count, max(profile.reading_ms, count * profile.per_character_wait_ms))
