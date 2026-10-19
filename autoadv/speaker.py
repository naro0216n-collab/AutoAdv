"""Per-game speaker policies without adding game-specific branches to Controller."""
from __future__ import annotations
from dataclasses import dataclass
import re
from .contracts import TTSPolicy

_DIALOGUE = re.compile(r'^\s*([^\s「」]{1,20})「(.+)」\s*$',re.DOTALL)


def split_speaker(text:str)->tuple[str|None,str]:
    m=_DIALOGUE.fullmatch(text)
    return (m.group(1),m.group(2)) if m else (None,text)


@dataclass(frozen=True)
class VoiceRoute:
    policy: TTSPolicy
    voice_id: str | None = None


class SpeakerRouter:
    def __init__(self,rules:dict[str,VoiceRoute],fallback:VoiceRoute):
        self.rules=dict(rules)
        self.fallback=fallback

    def resolve(self,raw:str)->VoiceRoute:
        speaker,_=split_speaker(raw)
        return self.rules.get(speaker,self.fallback)


def repair_ruby_and_names(text:str,names:dict[str,str]|None=None)->str:
    # A parenthesized reading after a Japanese kanji run replaces its base.
    # Ordinary parenthetical stage directions are unchanged.
    text=re.sub(r'[\u3400-\u9fff]+\(([\u3040-\u30ffー]{1,20})\)',lambda m:m.group(1),text)
    for written,reading in (names or {}).items():
        if not written or not reading:raise ValueError('empty alias')
        # Do not rewrite compositional names such as 東京 when 京 is an alias.
        text=re.sub(r'(?<![\u3400-\u9fff])'+re.escape(written)+
                    r'(?![\u3400-\u9fff])',reading,text)
    return text
