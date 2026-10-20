"""One per-game interpreter for runtime, ASR matching, TTS and GUI preview."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any
from .contracts import AudioRoute, ContentKind, TTSPolicy
import hashlib
import json
import re
import unicodedata

_SYMBOLS = frozenset('…‥―—－ー!！?？。、，．・･〜～-‐–~♪♫☆★♡♥ \t\n\r')
_KANJI = r'[\u3400-\u9fff]'
_RUBY = re.compile(r'([\u3400-\u9fff]+)\(([\u3040-\u30ffー]{1,30})\)')


@dataclass(frozen=True)
class StructureRule:
    rule_id: str
    pattern: str
    enabled: bool = True
    condition: str = ''  # optional expression matching the full original, never executable Python

    def __post_init__(self):
        try:
            groups = re.compile(self.pattern, re.DOTALL).groupindex
            if 'body' not in groups or not set(groups).issubset({'body','speaker'}):
                raise ValueError('structure regex requires named body and optional speaker groups')
            if self.condition:
                re.compile(self.condition, re.DOTALL)
        except re.error as error:
            raise ValueError(f'invalid structure rule {self.rule_id}: {error}') from error


@dataclass(frozen=True)
class TextReplacement:
    pattern: str
    replacement: str
    regex: bool = False
    enabled: bool = True
    target: str = 'tts'  # tts, asr, or both

    def __post_init__(self):
        if not self.pattern or self.target not in ('tts','asr','both'):
            raise ValueError('invalid replacement')
        if self.regex:
            try: re.compile(self.pattern)
            except re.error as error: raise ValueError('invalid replacement regex') from error


@dataclass(frozen=True)
class Utterance:
    observed_original: str
    speaker: str | None
    body: str
    structure_rule_id: str | None
    content_kind: ContentKind
    tts_text: str
    asr_target: str
    effective_chars: int
    tts_route: TTSPolicy
    voice_id: str | None
    audio_route: AudioRoute
    config_revision: str
    matching: str
    conflicts: tuple[str, ...] = ()


_DEFAULT_RULES = (
    StructureRule('quoted', r'^\s*(?P<speaker>[^\s「」\n]{1,40})\s*「(?P<body>.*)」\s*$'),
    StructureRule('newline', r'^\s*(?P<speaker>[^\s「」\n]{1,40})\n(?P<body>.+)$'),
    StructureRule('anonymous-quoted', r'^\s*「(?P<body>.*)」\s*$'),
)


@dataclass(frozen=True)
class GameTextConfig:
    structure_rules: tuple[StructureRule, ...] = _DEFAULT_RULES
    replacements: tuple[TextReplacement, ...] = ()
    name_readings: dict[str, str] = field(default_factory=dict)
    speaker_routes: dict[str, tuple[TTSPolicy, str | None]] = field(default_factory=dict)
    unknown_route: TTSPolicy = TTSPolicy.UNASSIGNED
    unknown_voice: str | None = None
    default_voice: str | None = None

    def __post_init__(self):
        ids=[r.rule_id for r in self.structure_rules]
        if len(ids)!=len(set(ids)):
            raise ValueError('duplicate structure rule IDs')
        if any(not word or not reading for word,reading in self.name_readings.items()):
            raise ValueError('empty name reading')
        for policy,voice in (*self.speaker_routes.values(),(self.unknown_route,self.unknown_voice)):
            if policy is TTSPolicy.SELECTED and not voice:
                raise ValueError('selected voice requires an ID')
        if (self.unknown_route is TTSPolicy.DEFAULT or
            any(route is TTSPolicy.DEFAULT for route, _ in self.speaker_routes.values())) and not self.default_voice:
            raise ValueError('default voice requires an ID')

    def resolve_voice(self,speaker:str | None) -> tuple[TTSPolicy,str | None]:
        """One owner of speaker policy and effective voice across GUI/runtime."""
        policy,voice=self.speaker_routes.get(speaker,(self.unknown_route,self.unknown_voice))
        if policy is TTSPolicy.DEFAULT:
            voice=self.default_voice
        elif policy in (TTSPolicy.AUTO,TTSPolicy.UNASSIGNED):
            voice=voice or self.default_voice
        elif policy is not TTSPolicy.SELECTED:
            voice=None
        return policy,voice

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'GameTextConfig':
        rules=value.get('structure_rules')
        return cls(
            structure_rules=tuple(StructureRule(**r) for r in rules) if rules is not None else _DEFAULT_RULES,
            replacements=tuple(TextReplacement(**r) for r in value.get('replacements', [])),
            name_readings=dict(value.get('name_readings',{})),
            speaker_routes={s:(TTSPolicy[r['policy']],r.get('voice_id')) for s,r in value.get('speaker_routes',{}).items()},
            unknown_route=TTSPolicy[value.get('unknown_route','UNASSIGNED')],
            unknown_voice=value.get('unknown_voice'),default_voice=value.get('default_voice'))

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return {'structure_rules':[asdict(x) for x in self.structure_rules],
                'replacements':[asdict(x) for x in self.replacements],
                'name_readings':self.name_readings,
                'speaker_routes':{k:{'policy':v[0].name,'voice_id':v[1]} for k,v in self.speaker_routes.items()},
                'unknown_route':self.unknown_route.name,'unknown_voice':self.unknown_voice,
                'default_voice':self.default_voice}

    @property
    def revision(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(),sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]


def _readings(text: str, names: dict[str,str]) -> str:
    text = _RUBY.sub(lambda m:m.group(2), text)
    for written, reading in names.items():
        # Treat a configured Japanese name as a complete kanji word.
        text=re.sub(r'(?<!'+_KANJI+r')'+re.escape(written)+r'(?!'+_KANJI+r')',
                    lambda _:reading,text)
    return text


class TextInterpreter:
    def __init__(self, config: GameTextConfig | None=None):
        self.config = config or GameTextConfig()

    def parse(self, original: str) -> Utterance:
        matched=[]
        for rule in self.config.structure_rules:
            if not rule.enabled or rule.condition and not re.search(rule.condition,original,re.DOTALL):
                continue
            m=re.fullmatch(rule.pattern,original,re.DOTALL)
            if m: matched.append((rule,m))
        chosen,match=matched[0] if matched else (None,None)
        body=match.group('body') if match else original
        speaker=match.groupdict().get('speaker') if match else None
        body=unicodedata.normalize('NFKC',body).strip()
        speaker=speaker.strip() if speaker else None
        meaningful=[c for c in body if not c.isspace()]
        symbolic=bool(meaningful) and all(c in _SYMBOLS or unicodedata.category(c).startswith('P') for c in meaningful)
        kind=ContentKind.NON_DIALOGUE if not meaningful else ContentKind.SYMBOLIC if symbolic else ContentKind.SPEAKABLE
        tts=_readings(body,self.config.name_readings) if kind is ContentKind.SPEAKABLE else ''
        asr=body
        for rule in self.config.replacements:
            if not rule.enabled: continue
            if rule.target in ('tts','both'):
                tts=re.sub(rule.pattern,rule.replacement,tts) if rule.regex else tts.replace(rule.pattern,rule.replacement)
            if rule.target in ('asr','both'):
                asr=re.sub(rule.pattern,rule.replacement,asr) if rule.regex else asr.replace(rule.pattern,rule.replacement)
        policy,voice=self.config.resolve_voice(speaker)
        route=(AudioRoute.NATIVE if policy is TTSPolicy.NATIVE else
               AudioRoute.TTS if policy in (TTSPolicy.SELECTED,TTSPolicy.DEFAULT) else
               AudioRoute.AUTO if policy in (TTSPolicy.AUTO,TTSPolicy.UNASSIGNED) else
               AudioRoute.SILENT)
        return Utterance(original,speaker,body,chosen.rule_id if chosen else None,kind,tts,asr,
                         len(meaningful),policy,voice,route,self.config.revision,
                         re.sub(r'\s+','',asr),tuple(x.rule_id for x,_ in matched[1:]))
