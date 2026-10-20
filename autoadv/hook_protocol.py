"""Stable native Host observations, independent of dialogue and input state."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ThreadSource:
    process_id: int
    address: int
    context: int
    context2: int
    hook_code: str
    hook_name: str

    @property
    def identity(self) -> str:
        return f'{self.hook_code}|{self.context}|{self.context2}'


@dataclass(frozen=True)
class HookEvent:
    kind: Literal['connected', 'disconnected', 'created', 'removed', 'output',
                  'diagnostic', 'inserted', 'emulator', 'hook_found']
    host_generation: str
    connection_generation: int
    sequence: int
    received_ns: int
    process_id: int | None = None
    source: ThreadSource | None = None
    text: str = ''
    detail: str = ''

    def __post_init__(self):
        if not self.host_generation or self.connection_generation < 0 or self.sequence <= 0 or self.received_ns <= 0:
            raise ValueError('invalid Host provenance')
        if self.kind == 'output' and (not self.source or self.source.process_id != self.process_id):
            raise ValueError('output must be tied to a process and source')
