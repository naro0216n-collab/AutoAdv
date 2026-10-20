"""One game session consuming real LunaHost events and controlling HOOK acquisition.

This layer deliberately cannot authorize game input. Only confirmed, unique
source observations reach the existing Utterance/Controller pipeline, where
independent progression evidence and audio completion determine if input is allowed.
"""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from .hook_protocol import HookEvent, HookKey, SessionIdentity, ThreadSource
from .observation import OriginObservation


@dataclass(frozen=True)
class HookView:
    state: str
    reason: str
    game_id: str
    profile_id: str
    hook_pids: tuple[int, ...]
    active_hook_count: int
    unresolved_hook_count: int
    latest_original: str
    candidate_count: int


class HookApplication:
    """Application-owned Hook selection, unique-source evidence and invalidation."""

    def __init__(self, game_id: str, profile_id: str,
                 on_original: Callable[[OriginObservation], None],
                 on_invalidate: Callable[[str], None]):
        if not game_id or not profile_id: raise ValueError('game and profile IDs required')
        self.session = SessionIdentity(game_id, profile_id, '')
        self.on_original = on_original
        self.on_invalidate = on_invalidate
        self.state = 'host_not_started'
        self.reason = 'Host not connected'
        self.latest_original = ''
        self.candidates: dict[ThreadSource, str] = {}
        self.search_results: dict[str, list[str]] = {}
        self.connected_pids: set[int] = set()
        self._generation_seq = 0

    def status(self) -> HookView:
        return HookView(self.state,self.reason,self.session.game_id,self.session.profile_id,
                        tuple(sorted(self.session.hook_pids)),
                        sum(len(v)==1 for v in self.session.pending.values()),
                        len(self.session.selected)-sum(len(v)==1 for v in self.session.pending.values()),
                        self.latest_original,len(self.candidates))

    def _invalidate(self, reason: str) -> None:
        self.session.invalidate(reason)
        self.latest_original = ''
        self.state = 'not_verified'
        self.reason = reason
        self.on_invalidate(reason)

    def update_roles(self, *, hook_pids: set[int], audio_pids: set[int], input_pid: int, input_hwnd: int) -> None:
        self.session.bind_process_roles(hook_pids=hook_pids, audio_pids=audio_pids,
                                        input_pid=input_pid, input_hwnd=input_hwnd)
        self.candidates.clear()
        self.connected_pids.clear()
        self._invalidate('game_role_binding_changed')

    def select(self, keys: tuple[HookKey, ...]) -> None:
        self.session.choose(keys)
        self._invalidate('hook_selection_changed')
        for source in tuple(self.candidates):
            if self.session.candidate(source) == 'ambiguous':
                self._invalidate('ambiguous_restored_hook')
                break

    def switch_profile(self, profile_id: str, keys: tuple[HookKey, ...]) -> None:
        if not profile_id: raise ValueError('profile ID required')
        self._invalidate('profile_changed')
        self.session.profile_id = profile_id
        self.select(keys)

    def host_closed(self, reason: str) -> None:
        self._invalidate('native_host_closed: ' + reason)
        self.connected_pids.clear()
        self.candidates.clear()
        self.state = 'host_closed'

    def handle(self, event: HookEvent) -> None:
        if event.kind == 'diagnostic' and event.detail == 'native_host_ready':
            if self.session.host_generation and self.session.host_generation != event.host_generation:
                self._invalidate('host_restarted')
            self.session.host_generation=event.host_generation
            self.session.connection_generation=event.connection_generation
            self.session.source_sequence=event.sequence
            self.state='host_ready'
            self.reason='Awaiting game connection'
            return
        if self.session.host_generation != event.host_generation:
            return
        if event.kind == 'connected' and event.process_id in self.session.hook_pids:
            if event.connection_generation < self.session.connection_generation:
                return
            if event.connection_generation != self.session.connection_generation:
                self.session.attach_host(event.host_generation,event.connection_generation,event.sequence)
                self.candidates.clear()
                self.on_invalidate('host_connection_changed')
            self.connected_pids.add(event.process_id)
            self.state='connected'
            self.reason='Awaiting selected HOOK output confirmation'
            return
        if (event.kind == 'disconnected' and event.process_id in self.session.hook_pids
                and event.connection_generation == self.session.connection_generation):
            self.connected_pids.discard(event.process_id)
            self._invalidate('hook_process_disconnected')
            return
        if event.connection_generation != self.session.connection_generation:
            return
        if event.kind == 'diagnostic' and (event.detail.startswith('injection_conflict')
                or event.detail.startswith('command_failed')):
            self._invalidate(event.detail)
            return
        if event.kind == 'hook_found':
            if self.session.observes(event) and event.process_id in self.connected_pids:
                self.search_results.setdefault(event.detail or event.text,[]).append(event.text)
            return
        if event.kind in ('created','removed') and event.source is not None:
            if not self.session.observes(event): return
            if event.process_id not in self.connected_pids: return
            if event.kind == 'created':
                self.candidates[event.source] = ''
                selection = self.session.candidate(event.source)
                if selection == 'ambiguous':self._invalidate('ambiguous_restored_hook')
            else:
                self.candidates.pop(event.source,None)
                for group in self.session.pending.values():
                    if event.source in group:
                        self._invalidate('selected_hook_removed')
                        return
            return
        if event.kind == 'output' and event.source is not None:
            if event.process_id not in self.connected_pids: return
            if event.source not in self.candidates:return
            self.candidates[event.source] = event.text
            if not self.session.accepts(event):return
            # Independent HOOK events are NOT speaker/body pairs. Combining
            # multiple selected sources needs a separately proven boundary.
            if len(self.session.selected) != 1:
                self.state='preview_only'
                self.reason='Multiple HOOK boundaries unresolved'
                return
            if not event.text.strip():
                self._invalidate('selected_hook_empty_output')
                return
            self.latest_original=event.text
            self.state='observing'
            self.reason='Observed source; input still requires Controller checks'
            self.on_original(OriginObservation(event.sequence,event.text,event.text,
                kind='hook',game_id=self.session.game_id,profile_id=self.session.profile_id,
                source_generation=self.session.session_epoch,
                origin=f'{event.source.process_id}:{event.source.hook_code}:'
                       f'{event.source.context:x}:{event.source.context2:x}:'
                       f'{event.host_generation}:{event.connection_generation}'))
