"""User-visible game/profile configuration and one utterance interpretation contract."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from collections.abc import Sequence
import re
import unicodedata

from .g2p import G2PBackend, PronunciationCandidate, WordSpan, reading_units_from_candidates


class AdvanceMode(str, Enum):
    TEXT = 'text'
    AUDIO = 'audio'


class InputMethod(str, Enum):
    WINDOW_MESSAGE = 'window_message'
    SEND_INPUT = 'send_input'


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
    target: str = 'tts'  # tts, match or both
    enabled: bool = True

    def __post_init__(self):
        if not self.pattern or self.target not in ('tts', 'match', 'both'):
            raise ValueError('invalid text replacement')
        if self.regex:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f'invalid replacement regex: {exc}') from exc


# Extraction works on an NFKC-normalized copy.  These are structural patterns,
# not title-specific exceptions.  More unusual layouts remain editable per source.
BODY_ONLY_RULE = StructureRule(r'^\s*(?P<body>.*)\s*$')
SPEAKER_ONLY_RULE = StructureRule(r'^\s*(?P<speaker>.*?)\s*$')
SPEAKER_QUOTED_BODY_RULE = StructureRule(
    r'^\s*(?P<speaker>[^\s「」『』\n]{1,40})\s*[「『](?P<body>.*)[」』]\s*$')
# A bare newline is inherently ambiguous: it may be an engine/UI aggregation,
# wrapped dialogue, or a real speaker/body format.  Keep it as an explicit
# per-source preset, but never infer it automatically.
SPEAKER_NEWLINE_BODY_RULE = StructureRule(
    r'^\s*(?P<speaker>[^\n]{1,40})\n(?P<body>.+)$')
# A colon in dialogue (notably https://) is not a speaker separator.
SPEAKER_COLON_BODY_RULE = StructureRule(
    r'^\s*(?P<speaker>[^\s「」『』、。，．！？!?():\n]{1,40})\s*[:]\s*(?!https?://)(?P<body>.+)$')
SPEAKER_METADATA_BODY_RULE = StructureRule(
    r'^\s*(?P<speaker>\S{1,40})\s+\([^\n)]{1,80}\)\s*(?P<body>.+)$')
QUOTED_BODY_RULE = StructureRule(r'^\s*[「『](?P<body>.*)[」』]\s*$')

DEFAULT_STRUCTURE_RULES = (
    SPEAKER_QUOTED_BODY_RULE,
    SPEAKER_COLON_BODY_RULE,
    SPEAKER_METADATA_BODY_RULE,
    QUOTED_BODY_RULE,
)

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


COMMON_FIELDS = ('advance_mode', 'uncertain_text_fallback', 'force_advance_seconds', 'default_tts',
                 'default_voice', 'reading_ms', 'per_reading_unit_wait_ms',
                 'native_helper', 'alignment_model', 'alignment_vocab', 'input_key',
                 'input_method')


SPEAKER_STYLE_OPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    'age': (
        ('', '未設定'), ('child', '幼い'), ('teen', '10代程度'), ('young', '若い'),
        ('adult', '成人'), ('middle_aged', '中年'), ('senior', '年配'),
    ),
    'gender': (
        ('', '未設定'), ('feminine', '女性的'), ('masculine', '男性的'),
        ('androgynous', '中性的'),
    ),
    'pitch': (
        ('', '未設定'), ('high', '高い'), ('slightly_high', 'やや高い'),
        ('slightly_low', 'やや低い'), ('low', '低い'),
    ),
    'timbre': (
        ('', '未設定'), ('soft', '柔らかい'), ('clear', '澄んだ'),
        ('resonant', '深く響く'), ('husky', 'ハスキー'), ('breathy', '息混じり'),
        ('delicate', '繊細'), ('firm', '芯がある'), ('powerful', '力強い'),
    ),
    'demeanor': (
        ('', '未設定'), ('calm', '穏やか'), ('bright', '明るい'),
        ('energetic', '元気'), ('reserved', '控えめ'), ('shy', '内気'),
        ('cool', '冷静'), ('serious', '真面目'), ('confident', '自信がある'),
        ('elegant', '上品'), ('lively', '活発'), ('mischievous', 'いたずらっぽい'),
        ('aloof', '無愛想'), ('dignified', '威厳がある'),
    ),
    'delivery': (
        ('', '未設定'), ('soft', '柔らかい'), ('articulate', 'はきはき'),
        ('composed', '落ち着いた'), ('polite', '丁寧'), ('forceful', '力強い'),
        ('flat', '淡々'), ('blunt', 'ぶっきらぼう'),
    ),
    'pace': (
        ('', '未設定'), ('slow', '遅い'), ('slightly_slow', 'やや遅い'),
        ('slightly_fast', 'やや速い'), ('fast', '速い'),
    ),
    'intonation': (
        ('', '未設定'), ('restrained', '控えめ'), ('natural', '自然'),
        ('expressive', '豊か'),
    ),
}
SPEAKER_STYLE_FIELDS = tuple(SPEAKER_STYLE_OPTIONS)


@dataclass(frozen=True)
class SpeakerStyle:
    """Per-speaker voice-design choices; empty means no instruction."""
    age: str = ''
    gender: str = ''
    pitch: str = ''
    timbre: str = ''
    demeanor: str = ''
    delivery: str = ''
    pace: str = ''
    intonation: str = ''

    def validate(self) -> None:
        for field_name in SPEAKER_STYLE_FIELDS:
            allowed = {value for value, _label in SPEAKER_STYLE_OPTIONS[field_name]}
            if getattr(self, field_name) not in allowed:
                raise ValueError(f'invalid speaker style {field_name}')


_AGE_PREFIX = {
    'child': '幼い', 'teen': '10代くらいの', 'young': '若い', 'adult': '成人の',
    'middle_aged': '中年の', 'senior': '年配の',
}
_GENDER_PREFIX = {
    'feminine': '女性の', 'masculine': '男性の', 'androgynous': '中性的な',
}
_PITCH_SINGLE = {
    'high': '高めの', 'slightly_high': 'やや高めの',
    'slightly_low': 'やや低めの', 'low': '低めの',
}
_PITCH_CONNECT = {
    'high': '高めで', 'slightly_high': 'やや高めで',
    'slightly_low': 'やや低めで', 'low': '低めで',
}
_TIMBRE = {
    'soft': '柔らかい', 'clear': '澄んだ', 'resonant': '深く響く',
    'husky': 'ハスキーな', 'breathy': '息混じりの', 'delicate': '繊細な',
    'firm': '芯のある', 'powerful': '力強い',
}
_DEMEANOR = {
    'calm': '穏やかな雰囲気で', 'bright': '明るい雰囲気で', 'energetic': '元気よく',
    'reserved': '控えめに', 'shy': '遠慮がちに', 'cool': '冷静に',
    'serious': '真面目な調子で', 'confident': '自信のある調子で',
    'elegant': '上品な調子で', 'lively': '快活に', 'mischievous': 'いたずらっぽく',
    'aloof': 'そっけない調子で', 'dignified': '威厳のある調子で',
}
_DELIVERY = {
    'soft': '柔らかく', 'articulate': 'はきはきと', 'composed': '落ち着いて',
    'polite': '丁寧に', 'forceful': '力強く', 'flat': '淡々と',
    'blunt': 'ぶっきらぼうに',
}
_PACE = {
    'slow': 'ゆっくり', 'slightly_slow': 'ややゆっくり',
    'slightly_fast': 'やや速めに', 'fast': '速いテンポで',
}
_INTONATION = {
    'restrained': '抑揚を控えめにして', 'natural': '自然な抑揚で',
    'expressive': '豊かな抑揚をつけて',
}


def build_speaker_caption(style: SpeakerStyle) -> str:
    """Render only explicitly selected dimensions as natural Japanese prose."""
    style.validate()
    sentences: list[str] = []

    identity = ''
    if style.age:
        identity += _AGE_PREFIX[style.age]
    if style.gender:
        identity += _GENDER_PREFIX[style.gender]

    voice_modifier = ''
    if style.pitch and style.timbre:
        voice_modifier = _PITCH_CONNECT[style.pitch] + _TIMBRE[style.timbre]
    elif style.pitch:
        voice_modifier = _PITCH_SINGLE[style.pitch]
    elif style.timbre:
        voice_modifier = _TIMBRE[style.timbre]

    if identity and voice_modifier:
        sentences.append(f'{identity}、{voice_modifier}声。')
    elif identity:
        sentences.append(f'{identity}声。')
    elif voice_modifier:
        sentences.append(f'{voice_modifier}声。')

    delivery_parts = [
        mapping[value]
        for value, mapping in (
            (style.demeanor, _DEMEANOR), (style.delivery, _DELIVERY),
            (style.pace, _PACE), (style.intonation, _INTONATION),
        )
        if value
    ]
    if delivery_parts:
        sentences.append('、'.join(delivery_parts) + '話す。')
    return ''.join(sentences)


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
    unwrap_speaker_enclosed_body: bool = False
    inserted_hooks: list[str] = field(default_factory=list)
    insert_pc_hooks: bool = False
    advance_mode: AdvanceMode = AdvanceMode.TEXT
    uncertain_text_fallback: bool = True
    force_advance_seconds: int = 30
    default_tts: bool = False
    default_voice: str = ''
    speaker_tts: dict[str, bool] = field(default_factory=dict)
    speaker_voices: dict[str, str] = field(default_factory=dict)
    speaker_styles: dict[str, SpeakerStyle] = field(default_factory=dict)
    replacements: list[ReplacementRule] = field(default_factory=list)
    name_readings: dict[str, str] = field(default_factory=dict)
    reading_ms: int = 700
    per_reading_unit_wait_ms: int = 200
    hook_flush_ms: int = 500
    hook_codepage: int = 932
    hook_max_buffer: int = 3000
    hook_max_history: int = 1000000
    repeat_rule: str = 'distinct_only'
    input_key: int = 13
    input_method: InputMethod = InputMethod.WINDOW_MESSAGE
    source: str = 'hook'
    ocr: OCRSettings = field(default_factory=OCRSettings)
    native_helper: str = ''
    alignment_model: str = ''
    alignment_vocab: str = ''
    inherit_common: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not self.id or not self.name or type(self.reading_ms) is not int or self.reading_ms <= 0 or type(self.per_reading_unit_wait_ms) is not int or self.per_reading_unit_wait_ms <= 0:
            raise ValueError('invalid profile')
        if (not 0 <= self.hook_flush_ms <= 10000 or not 0 <= self.hook_codepage <= 65535
                or not 1 <= self.hook_max_buffer <= 1000000 or not 1 <= self.hook_max_history <= 10000000):
            raise ValueError('invalid Luna capture settings')
        if self.repeat_rule not in ('distinct_only', 'each_flush') or not 1 <= self.input_key <= 254:
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
        if type(self.unwrap_speaker_enclosed_body) is not bool:
            raise ValueError('invalid speaker-aware body formatting')
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
        if (type(self.uncertain_text_fallback) is not bool
                or type(self.force_advance_seconds) is not int or self.force_advance_seconds <= 0
                or type(self.default_tts) is not bool
                or any(type(v) is not bool or not k for k, v in self.speaker_tts.items())):
            raise ValueError('invalid TTS and fallback settings')
        if any(not name or not isinstance(style, SpeakerStyle)
               for name, style in self.speaker_styles.items()):
            raise ValueError('invalid speaker style settings')
        for style in self.speaker_styles.values():
            style.validate()
        AdvanceMode(self.advance_mode)
        InputMethod(self.input_method)
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
class SpeechPlan:
    """Resolved speech inputs for one immutable utterance.

    ``resolved_text`` is the common explicit-reading result used for timing.
    Output-specific corrections may derive different TTS and alignment inputs
    without rewriting the displayed dialogue.
    """
    resolved_text: str
    tts_text: str
    alignment_text: str
    alignment_candidates: tuple[PronunciationCandidate, ...] = ()


@dataclass(frozen=True)
class Token:
    """Immutable identity of one admitted Presentation within a runtime session."""
    session: int
    line: int


@dataclass(frozen=True)
class Utterance:
    session: int
    line: int
    source: str
    sequence: int
    raw: str
    speaker: str
    text: str
    speech: SpeechPlan
    use_tts: bool
    voice: str
    tts_caption: str
    advance_mode: AdvanceMode
    input_method: InputMethod
    input_key: int
    uncertain_text_fallback: bool
    force_advance_seconds: int
    reading_unit_count: int
    text_wait_ms: int


_RUBY = re.compile(r'([\u3400-\u9fff]+)\(([\u3040-\u30ffー]{1,30})\)')



_SPEAKER_BODY_WRAPPERS = {
    '（': '）',
    '(': ')',
    '「': '」',
    '『': '』',
}

def _speaker_key(value: str) -> str:
    return unicodedata.normalize('NFKC', value).strip().casefold()

def configured_speaker_names(profile: Profile) -> tuple[str, ...]:
    """Return the profile's existing user-defined speaker names once."""
    return tuple(dict.fromkeys((*profile.speaker_tts, *profile.speaker_voices, *profile.speaker_styles)))

def resolve_speaker_enclosed_body(
        body: str, speaker: str, configured_speakers: Sequence[str] = ()) -> tuple[str, str]:
    """Resolve ``speaker + one outer wrapper`` from existing speaker information.

    An already assembled speaker is authoritative.  When the current dialogue has
    no speaker candidate, an exact match against the profile's existing speaker
    settings may identify the prefix.  Wrapper syntax alone never creates a new
    speaker, so prose such as ``東京（とうきょう）`` or ``本文（補足）`` stays
    untouched unless that prefix is already known as a speaker.
    """
    speaker_key = _speaker_key(speaker)
    configured_by_key: dict[str, list[str]] = {}
    for name in configured_speakers:
        key = _speaker_key(name)
        if key:
            configured_by_key.setdefault(key, []).append(name)
    sample = body.strip()
    if not sample:
        return speaker, body
    for opener, closer in _SPEAKER_BODY_WRAPPERS.items():
        open_at = sample.find(opener)
        if open_at <= 0 or not sample.endswith(closer):
            continue
        prefix_key = _speaker_key(sample[:open_at])
        resolved_speaker = speaker
        if speaker_key:
            if prefix_key != speaker_key:
                continue
        else:
            matches = configured_by_key.get(prefix_key, ())
            if len(matches) != 1:
                continue
            resolved_speaker = matches[0]
        depth = 0
        closed_at = -1
        for index in range(open_at, len(sample)):
            ch = sample[index]
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth < 0:
                    break
                if depth == 0:
                    closed_at = index
                    break
        if closed_at != len(sample) - 1:
            continue
        inner = sample[open_at + 1:closed_at]
        return (resolved_speaker, inner) if inner.strip() else (speaker, body)
    return speaker, body

def extraction_rules_for(profile: Profile, source_identity: str) -> tuple[StructureRule, ...]:
    configured = profile.source_rules.get(source_identity)
    return tuple(configured) if configured is not None else DEFAULT_STRUCTURE_RULES


def matching_structure_rule(raw: str, rules: Sequence[StructureRule]) -> tuple[re.Match | None, int | None, str]:
    """Prefer source coordinates; normalize only where a structural rule needs it."""
    normalized = unicodedata.normalize('NFKC', raw)
    for index, rule in enumerate(rules):
        if not rule.enabled:
            continue
        for view in (normalized,) if raw != normalized else (raw,):
            if rule.condition and not re.search(rule.condition, view, re.S):
                continue
            match = re.fullmatch(rule.pattern, view, re.S)
            if match:
                return match, index, view
    return None, None, raw


def _original_span(raw: str, normalized: str, start: int, end: int) -> str | None:
    """Project one NFKC-view span back to an unmodified source substring.

    Prefix normalization handles both expansion and cross-character composition
    such as half-width kana plus a voiced mark. A projection is accepted only
    when exactly one source substring normalizes to the matched view; ambiguous
    boundaries are never guessed.
    """
    if normalized == raw:
        return raw[start:end]
    if not 0 <= start <= end <= len(normalized):
        return None
    prefix_offsets = [len(unicodedata.normalize('NFKC', raw[:index]))
                      for index in range(len(raw) + 1)]
    source_starts = [index for index, offset in enumerate(prefix_offsets) if offset == start]
    source_ends = [index for index, offset in enumerate(prefix_offsets) if offset == end]
    expected = normalized[start:end]
    matches = [raw[source_start:source_end]
               for source_start in source_starts
               for source_end in source_ends
               if source_start <= source_end and
               unicodedata.normalize('NFKC', raw[source_start:source_end]) == expected]
    return matches[0] if len(matches) == 1 else None


def extract_fields(raw: str, rules: Sequence[StructureRule]) -> ExtractedFields:
    matched, matched_index, view = matching_structure_rule(raw, rules)
    if matched is None:
        return ExtractedFields(body=raw)
    result = {}
    for key in ('speaker', 'body'):
        if key not in matched.re.groupindex:
            continue
        start, end = matched.span(key)
        if start < 0:
            continue
        value = _original_span(raw, view, start, end)
        if value is None:
            return ExtractedFields(body=raw)
        result[key] = value.strip() if key == 'speaker' else value
    return ExtractedFields(speaker=result.get('speaker', ''), body=result.get('body', ''),
                           matched_rule=matched_index)


def extract_source_fields(raw: str, profile: Profile, source_identity: str) -> ExtractedFields:
    return extract_fields(raw, extraction_rules_for(profile, source_identity))


def _covered_by_words(start: int, end: int, spans: Sequence[WordSpan]) -> bool:
    """True when [start, end) is exactly a contiguous sequence of whole words."""
    cursor = start
    for span in spans:
        if span.end <= cursor:
            continue
        if span.start != cursor or span.end > end:
            return False
        cursor = span.end
        if cursor == end:
            return True
    return False


def _canonical_offset(text: str, source_offset: int) -> int:
    """Project one source boundary into the NFKC view without rewriting source."""
    return len(unicodedata.normalize('NFKC', text[:source_offset]))


def _overlaps_any(start: int, end: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(start < protected_end and protected_start < end
               for protected_start, protected_end in ranges)


def _expand_ruby(text: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Expand explicit ruby and return output ranges protected from later readings.

    Registered readings are lower priority than ruby. Keeping the expanded
    ranges explicit prevents a configured surface that happens to equal ruby's
    kana from rewriting the user's inline pronunciation.
    """
    out: list[str] = []
    protected: list[tuple[int, int]] = []
    source_index = 0
    output_length = 0
    for match in _RUBY.finditer(text):
        literal = text[source_index:match.start()]
        out.append(literal)
        output_length += len(literal)
        reading = match.group(2)
        start = output_length
        out.append(reading)
        output_length += len(reading)
        protected.append((start, output_length))
        source_index = match.end()
    tail = text[source_index:]
    out.append(tail)
    return ''.join(out), tuple(protected)


def _apply_name_readings(text: str, readings: dict[str, str], g2p: G2PBackend,
                         trace: list[str] | None = None, *,
                         protected_ranges: Sequence[tuple[int, int]] = ()) -> str:
    """Apply registered readings only to complete primary Haqumei word ranges.

    Matching always starts from the literal source spelling configured by the
    user. Its source boundaries are projected into Haqumei's canonical
    coordinate space and must cover one or more adjacent primary words exactly.
    Explicit-ruby output ranges are excluded because ruby has higher priority.
    No character class, particular kanji, or title-specific exception decides
    whether a reading applies.
    """
    if not readings or not text:
        return text
    canonical = unicodedata.normalize('NFKC', text)
    spans = g2p.word_spans(text)
    choices = sorted(readings.items(), key=lambda item: (-len(item[0]), item[0]))
    out: list[str] = []
    index = 0
    while index < len(text):
        chosen = None
        for surface, reading in choices:
            end = index + len(surface)
            if end > len(text) or not text.startswith(surface, index):
                continue
            if _overlaps_any(index, end, protected_ranges):
                continue
            canonical_start = _canonical_offset(text, index)
            canonical_end = _canonical_offset(text, end)
            if canonical_start >= canonical_end:
                continue
            if unicodedata.normalize('NFKC', text[index:end]) != canonical[canonical_start:canonical_end]:
                continue
            if _covered_by_words(canonical_start, canonical_end, spans):
                chosen = (surface, reading, end)
                break
        if chosen is None:
            out.append(text[index])
            index += 1
            continue
        surface, reading, end = chosen
        out.append(reading)
        if trace is not None:
            trace.append(f'読み: {surface} → {reading}')
        index = end
    return ''.join(out)


def _replace(text: str, rule: ReplacementRule) -> str:
    return (re.sub(rule.pattern, rule.replacement, text) if rule.regex else
            text.replace(rule.pattern, rule.replacement))


def interpret(raw: str, profile: Profile, *, session: int, line: int, source: str,
              sequence: int, g2p: G2PBackend, speaker_override: str | None = None,
              body_override: str | None = None, trace: list[str] | None = None) -> Utterance:
    if body_override is None:
        fields = extract_source_fields(raw, profile, source)
        body = fields.body
        speaker = fields.speaker if speaker_override is None else speaker_override.strip()
        if trace is not None:
            trace.append(f'抽出: 優先順位 {fields.matched_rule + 1}' if fields.matched_rule is not None
                         else '抽出: 規則なし（原文を本文として使用）')
    else:
        body = body_override
        speaker = (speaker_override or '').strip()
        if trace is not None:
            trace.append('抽出: 会話取得レシピで確定済み')

    # Explicit ruby wins before configured readings. Ruby output remains marked
    # so a configured surface cannot accidentally rewrite the inline reading.
    resolved, ruby_ranges = _expand_ruby(body)
    resolved = _apply_name_readings(
        resolved, profile.name_readings, g2p, trace, protected_ranges=ruby_ranges)

    # Shared corrections alter the common speech target. Output-specific rules
    # derive their own inputs from that target and never mutate display text.
    for index, rule in enumerate(profile.replacements, 1):
        if not rule.enabled or rule.target != 'both':
            continue
        updated = _replace(resolved, rule)
        if trace is not None and updated != resolved:
            trace.append(f'置換 {index}: 共通発音を変更')
        resolved = updated

    tts_text = resolved
    alignment_text = resolved
    for index, rule in enumerate(profile.replacements, 1):
        if not rule.enabled or rule.target == 'both':
            continue
        if rule.target == 'tts':
            updated = _replace(tts_text, rule)
            if trace is not None and updated != tts_text:
                trace.append(f'置換 {index}: TTS文を変更')
            tts_text = updated
        elif rule.target == 'match':
            updated = _replace(alignment_text, rule)
            if trace is not None and updated != alignment_text:
                trace.append(f'置換 {index}: 原音照合文を変更')
            alignment_text = updated

    use_tts = profile.speaker_tts.get(speaker, profile.default_tts)
    voice = profile.speaker_voices.get(speaker, profile.default_voice)
    tts_caption = build_speaker_caption(profile.speaker_styles.get(speaker, SpeakerStyle()))
    advance_mode = AdvanceMode(profile.advance_mode)
    input_method = InputMethod(profile.input_method)
    use_alignment = advance_mode == AdvanceMode.AUDIO and not use_tts
    primary_candidates = g2p.candidates(resolved, max_candidates=4 if use_alignment else 1)
    reading_units = reading_units_from_candidates(resolved, primary_candidates)
    alignment_candidates = (
        primary_candidates if alignment_text == resolved else
        g2p.candidates(alignment_text, max_candidates=4)
    ) if use_alignment else ()
    speech = SpeechPlan(resolved, tts_text, alignment_text, alignment_candidates)
    return Utterance(
        session=session, line=line, source=source, sequence=sequence, raw=raw,
        speaker=speaker, text=body, speech=speech, use_tts=use_tts, voice=voice,
        tts_caption=tts_caption,
        advance_mode=advance_mode, input_method=input_method, input_key=profile.input_key,
        uncertain_text_fallback=profile.uncertain_text_fallback,
        force_advance_seconds=profile.force_advance_seconds,
        reading_unit_count=reading_units,
        text_wait_ms=max(profile.reading_ms, reading_units * profile.per_reading_unit_wait_ms),
    )
