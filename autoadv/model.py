"""User-visible game/profile configuration and one utterance interpretation contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Sequence
import re
import unicodedata


class AdvanceMode(str, Enum):
    TEXT = 'text'
    AUDIO = 'audio'


@dataclass(frozen=True)
class HookSelection:
    """Stable reference to one Luna output source.

    A source has no dialogue/speaker role.  The capture recipe independently
    chooses which selected sources provide the final body and speaker fields.
    """
    code: str
    context: int
    context2: int

    def __post_init__(self):
        if not self.code or min(self.context, self.context2) < 0:
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
    """Extract speaker and/or body from one source sample.

    Rules belong to a capture source, never to the whole profile.  A rule may
    expose only one field so a source with a correct speaker but unusable body
    can still participate in the final dialogue assembly (and vice versa).
    """
    pattern: str
    condition: str = ''
    enabled: bool = True

    def __post_init__(self):
        try:
            groups = re.compile(self.pattern, re.S).groupindex
            names = set(groups)
            if not names or not names.issubset({'body', 'speaker'}) or not names.intersection({'body', 'speaker'}):
                raise ValueError('structure rule requires named speaker and/or body')
            if self.condition:
                re.compile(self.condition, re.S)
        except re.error as exc:
            raise ValueError(f'invalid structure regex: {exc}') from exc


@dataclass(frozen=True)
class ExtractedFields:
    speaker: str = ''
    body: str = ''
    matched_rule: int | None = None


@dataclass(frozen=True)
class DialogueAssembly:
    """One deterministic field selection from a synchronized source group."""
    body_index: int | None = None
    speaker_index: int | None = None
    body: str = ''
    speaker: str = ''
    problem: str = ''


def assemble_dialogue_fields(
        samples: Sequence[tuple[str, ExtractedFields]], *,
        body_source: str, speaker_source: str = '') -> DialogueAssembly:
    """Select final dialogue fields once for Preview and runtime admission.

    Missing optional speaker data is valid. Multiple candidates for a selected
    field are ambiguous and must never be guessed or merged.
    """
    body_indexes = [
        index for index, (identity, fields) in enumerate(samples)
        if identity == body_source and fields.body
    ]
    if not body_indexes:
        return DialogueAssembly(problem='missing_body')
    if len(body_indexes) != 1:
        return DialogueAssembly(problem='ambiguous_body')
    body_index = body_indexes[0]
    body_fields = samples[body_index][1]

    if speaker_source:
        speaker_indexes = [
            index for index, (identity, fields) in enumerate(samples)
            if identity == speaker_source and fields.speaker
        ]
        if len(speaker_indexes) > 1:
            return DialogueAssembly(problem='ambiguous_speaker')
        speaker_index = speaker_indexes[0] if speaker_indexes else None
        speaker = samples[speaker_index][1].speaker if speaker_index is not None else ''
    else:
        speaker_index = body_index if body_fields.speaker else None
        speaker = body_fields.speaker

    return DialogueAssembly(
        body_index=body_index, speaker_index=speaker_index,
        body=body_fields.body, speaker=speaker)


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


# Extraction works on an NFKC-normalized copy.  These are structural patterns,
# not title-specific exceptions.  More unusual layouts remain editable per source.
DEFAULT_STRUCTURE_RULES = (
    StructureRule(r'^\s*(?P<speaker>[^\s「」『』\n]{1,40})\s*[「『](?P<body>.*)[」』]\s*$'),
    StructureRule(r'^\s*(?P<speaker>[^\n]{1,40})\n(?P<body>.+)$'),
    # A colon in dialogue (notably https://) is not a speaker separator.
    StructureRule(r'^\s*(?P<speaker>[^\s「」『』、。，．！？!?():\n]{1,40})\s*[:]\s*(?!https?://)(?P<body>.+)$'),
    # Common engine metadata layout: NAME (line/id/voice tag) BODY.
    StructureRule(r'^\s*(?P<speaker>\S{1,40})\s+\([^\n)]{1,80}\)\s*(?P<body>.+)$'),
    StructureRule(r'^\s*[「『](?P<body>.*)[」』]\s*$'),
)

BODY_ONLY_RULE = StructureRule(r'^\s*(?P<body>.*)\s*$')
SPEAKER_ONLY_RULE = StructureRule(r'^\s*(?P<speaker>.*?)\s*$')
SPEAKER_QUOTED_BODY_RULE = DEFAULT_STRUCTURE_RULES[0]
SPEAKER_NEWLINE_BODY_RULE = DEFAULT_STRUCTURE_RULES[1]
SPEAKER_COLON_BODY_RULE = DEFAULT_STRUCTURE_RULES[2]
SPEAKER_METADATA_BODY_RULE = DEFAULT_STRUCTURE_RULES[3]
QUOTED_BODY_RULE = DEFAULT_STRUCTURE_RULES[4]

EXTRACTION_PRESETS: dict[str, tuple[StructureRule, ...]] = {
    'auto': DEFAULT_STRUCTURE_RULES,
    'body': (BODY_ONLY_RULE,),
    'speaker': (SPEAKER_ONLY_RULE,),
    'quoted': (SPEAKER_QUOTED_BODY_RULE,),
    'colon': (SPEAKER_COLON_BODY_RULE,),
    'newline': (SPEAKER_NEWLINE_BODY_RULE,),
    'metadata': (SPEAKER_METADATA_BODY_RULE,),
    'quoted_body': (QUOTED_BODY_RULE,),
}


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


COMMON_FIELDS = ('advance_mode', 'uncertain_text_fallback', 'default_tts',
                 'default_voice', 'reading_ms', 'per_character_wait_ms',
                 'native_helper', 'asr_model', 'asr_tokens', 'input_key',
                 'next_line_timeout_ms')


@dataclass
class Profile:
    id: str
    name: str
    hooks: list[HookSelection] = field(default_factory=list)
    # Final dialogue fields are assembled independently from one explicit
    # body source and, optionally, one speaker source in the same sync group.
    body_source: str = ''
    speaker_source: str = ''
    # Source identity -> ordered extraction rules. Missing identity uses the
    # structural defaults above.  'ocr' may also be configured here.
    source_rules: dict[str, list[StructureRule]] = field(default_factory=dict)
    inserted_hooks: list[str] = field(default_factory=list)
    insert_pc_hooks: bool = False
    advance_mode: AdvanceMode = AdvanceMode.TEXT
    uncertain_text_fallback: bool = False
    default_tts: bool = False
    default_voice: str = ''
    speaker_tts: dict[str, bool] = field(default_factory=dict)
    speaker_voices: dict[str, str] = field(default_factory=dict)
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
        if (not 0 <= self.hook_flush_ms <= 10000 or not 0 <= self.hook_codepage <= 65535
                or not 1 <= self.hook_max_buffer <= 1000000 or not 1 <= self.hook_max_history <= 10000000):
            raise ValueError('invalid Luna capture settings')
        if (self.repeat_rule not in ('distinct_only', 'each_after_input') or not 1 <= self.input_key <= 254
                or not 1000 <= self.next_line_timeout_ms <= 120000):
            raise ValueError('invalid advancement settings')
        identities = [hook.identity for hook in self.hooks]
        if len(identities) != len(set(identities)):
            raise ValueError('duplicate selected HOOK')
        selected = set(identities)
        if self.body_source and self.body_source not in selected:
            raise ValueError('invalid body source')
        if self.speaker_source and not self.body_source:
            raise ValueError('speaker source requires body source')
        if self.speaker_source and self.speaker_source not in selected:
            raise ValueError('invalid speaker source')
        if any(identity not in selected and identity != 'ocr' for identity in self.source_rules):
            raise ValueError('extraction rules require a selected source')
        for rules in self.source_rules.values():
            for rule in rules:
                StructureRule(rule.pattern, rule.condition, rule.enabled)
        if self.source not in ('hook', 'ocr'):
            raise ValueError('invalid acquisition mode')
        if type(self.insert_pc_hooks) is not bool:
            raise ValueError('invalid Luna Win32 generic hook setting')
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
            return index > 0 and ('\u3040' <= without_ruby[index-1] <= '\u30ff'
                                  or without_ruby[index-1] == 'ー')
        return unicodedata.category(character)[0] in 'LN'

    return sum(countable(i, c) for i, c in enumerate(without_ruby))


def extraction_rules_for(profile: Profile, source_identity: str) -> tuple[StructureRule, ...]:
    configured = profile.source_rules.get(source_identity)
    return tuple(configured) if configured is not None else DEFAULT_STRUCTURE_RULES


def matching_structure_rule(raw: str, rules: Sequence[StructureRule]) -> tuple[re.Match | None, int | None, str]:
    """Apply one source's structural rules to an NFKC-normalized sample."""
    normalized = unicodedata.normalize('NFKC', raw)
    for index, rule in enumerate(rules):
        if not rule.enabled or (rule.condition and not re.search(rule.condition, normalized, re.S)):
            continue
        match = re.fullmatch(rule.pattern, normalized, re.S)
        if match:
            return match, index, normalized
    return None, None, normalized


def extract_fields(raw: str, rules: Sequence[StructureRule]) -> ExtractedFields:
    matched, matched_index, normalized = matching_structure_rule(raw, rules)
    if matched is None:
        # Unknown structures stay usable as body text; the workspace can then
        # define a source-specific extractor without losing the sample.
        return ExtractedFields(body=normalized.strip())
    groups = matched.groupdict()
    speaker = (groups.get('speaker') or '').strip()
    body = (groups.get('body') or '').strip()
    return ExtractedFields(speaker=speaker, body=body, matched_rule=matched_index)


def extract_source_fields(raw: str, profile: Profile, source_identity: str) -> ExtractedFields:
    return extract_fields(raw, extraction_rules_for(profile, source_identity))


def interpret(raw: str, profile: Profile, *, session: int, line: int, source: str,
              sequence: int, speaker_override: str | None = None,
              body_override: str | None = None, trace: list[str] | None = None) -> Utterance:
    if body_override is None:
        fields = extract_source_fields(raw, profile, source)
        body = fields.body
        speaker = fields.speaker if speaker_override is None else speaker_override.strip()
        if trace is not None:
            trace.append(f'抽出: 優先順位 {fields.matched_rule + 1}' if fields.matched_rule is not None
                         else '抽出: 規則なし（原文を本文として使用）')
    else:
        body = unicodedata.normalize('NFKC', body_override).strip()
        speaker = (speaker_override or '').strip()
        if trace is not None:
            trace.append('抽出: 会話取得レシピで確定済み')
    reading = _RUBY.sub(lambda m: m.group(2), body)
    for word, replacement in sorted(profile.name_readings.items(), key=lambda item: -len(item[0])):
        prior = reading
        reading = re.sub(r'(?<!' + _KANJI + ')' + re.escape(word) + r'(?!' + _KANJI + ')',
                         lambda _: replacement, reading)
        if trace is not None and reading != prior:
            trace.append(f'読み: {word} → {replacement}')
    match_text = _RUBY.sub(lambda m: m.group(1), body)
    for index, rule in enumerate(profile.replacements, 1):
        if not rule.enabled:
            continue
        def replace(text):
            return (re.sub(rule.pattern, rule.replacement, text) if rule.regex else
                    text.replace(rule.pattern, rule.replacement))
        if rule.target in ('tts', 'both'):
            updated = replace(reading)
            if trace is not None and updated != reading:
                trace.append(f'置換 {index}: TTS文を変更')
            reading = updated
        if rule.target in ('asr', 'both'):
            updated = replace(match_text)
            if trace is not None and updated != match_text:
                trace.append(f'置換 {index}: ASR文を変更')
            match_text = updated
    use_tts = profile.speaker_tts.get(speaker, profile.default_tts)
    voice = profile.speaker_voices.get(speaker, profile.default_voice)
    count = count_reading_characters(body)
    return Utterance(session, line, source, sequence, raw, speaker, body,
                     reading, match_text, use_tts, voice,
                     AdvanceMode(profile.advance_mode), profile.uncertain_text_fallback,
                     count, max(profile.reading_ms, count * profile.per_character_wait_ms))
