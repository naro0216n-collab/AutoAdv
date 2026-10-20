"""Explicit HOOK-source identity and session generations.

Hook output is an observation, not a complete line, visual acknowledgement,
or an authorization to advance. This module has no input capability.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
import time


@dataclass(frozen=True, slots=True)
class HookKey:
    code: str
    name: str
    context: int
    context2: int
    # A persisted HookKey must not contain a PID or runtime code address.
    def __post_init__(self):
        if not self.code or self.context < 0 or self.context2 < 0:
            raise ValueError('invalid HOOK identity')

    def compatible(self, other: 'HookKey') -> bool:
        """Luna's compatibility heuristic; NOT sufficient for autoplay."""
        return (self.code == other.code and self.context & 0xFFFF == other.context & 0xFFFF
                and self.context2 & 0xFFFF == other.context2 & 0xFFFF)


@dataclass(frozen=True, slots=True)
class ThreadSource:
    process_id: int
    address: int
    context: int
    context2: int
    hook_code: str
    hook_name: str

    @property
    def key(self) -> HookKey:
        return HookKey(self.hook_code, self.hook_name, self.context, self.context2)


@dataclass(frozen=True, slots=True)
class HookEvent:
    kind: Literal['connected','disconnected','created','removed','output','diagnostic','inserted','emulator']
    host_generation: str
    connection_generation: int
    sequence: int
    received_ns: int
    process_id: int | None = None
    source: ThreadSource | None = None
    text: str = ''  # TextThread wchar_t decoded/buffered output, not raw bytes.
    detail: str = ''

    def __post_init__(self):
        if not self.host_generation or self.connection_generation < 0 or self.sequence <= 0 or self.received_ns <= 0:
            raise ValueError('missing event provenance')
        if self.kind == 'output' and (self.source is None or self.source.process_id != self.process_id):
            raise ValueError('HOOK output must include exact source and PID')


@dataclass
class SessionIdentity:
    """One active game instance; volatile PIDs are never profile identifiers."""
    game_id: str
    profile_id: str
    host_generation: str
    connection_generation: int = 1
    hook_pids: set[int] = field(default_factory=set)
    audio_pids: set[int] = field(default_factory=set)
    input_pid: int | None = None
    input_hwnd: int | None = None
    selected: tuple[HookKey,...] = ()
    pending: dict[HookKey,set[ThreadSource]] = field(default_factory=dict)
    source_sequence: int = 0
    authorized: bool = False
    invalid_reason: str = 'not_authorized'

    def invalidate(self, reason: str) -> None:
        self.connection_generation += 1
        self.pending.clear()
        self.source_sequence = 0
        self.authorized = False
        self.invalid_reason = reason

    def bind_process_roles(self, *, hook_pids: set[int], audio_pids: set[int], input_pid: int, input_hwnd: int) -> None:
        if not hook_pids or not audio_pids or input_pid <= 0 or input_hwnd <= 0 or min(hook_pids | audio_pids) <= 0:
            raise ValueError('process roles are not proven')
        self.invalidate('process_binding_changed')
        self.hook_pids = set(hook_pids)
        self.audio_pids = set(audio_pids)
        self.input_pid = input_pid
        self.input_hwnd = input_hwnd

    def choose(self, keys: tuple[HookKey,...]) -> None:
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate HOOK selection')
        self.invalidate('hook_selection_changed')
        self.selected = keys

    def candidate(self, source: ThreadSource) -> Literal['ignored','pending','unique','ambiguous']:
        if source.process_id not in self.hook_pids:
            return 'ignored'
        matches=[saved for saved in self.selected if saved.compatible(source.key)]
        if len(matches)!=1:
            return 'ambiguous' if matches else 'ignored'
        group=self.pending.setdefault(matches[0],set())
        group.add(source)
        return 'unique' if len(group)==1 else 'ambiguous'

    def accepts(self, event: HookEvent) -> bool:
        if (event.host_generation != self.host_generation or event.connection_generation != self.connection_generation
            or event.sequence <= self.source_sequence):
            return False
        self.source_sequence=event.sequence
        if event.kind!='output' or event.source is None:
            return False
        if event.process_id not in self.hook_pids:
            return False
        # A compatibility hit is only a hint. Unique candidates must have been
        # established from actual source events; no text-only fallback.
        return (any(event.source in sources for sources in self.pending.values())
                and all(len(sources)==1 for sources in self.pending.values())
                and len(self.pending)==len(self.selected))


def monotonic_ns() -> int:
    return time.monotonic_ns()
