from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from autoadv.application import AutoAdvApplication
from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import AdvanceMode, HookSelection, Profile
from autoadv.storage import Catalog
from tests.g2p_support import TEST_G2P


def profile(*, mode: AdvanceMode = AdvanceMode.TEXT, tts: bool = False,
            fallback: bool = True) -> Profile:
    return Profile(
        'p', 'game', hooks=[HookSelection('H', 1, 2)], body_source='H|1|2',
        advance_mode=mode, default_tts=tts, default_voice='v0',
        uncertain_text_fallback=fallback, reading_ms=350,
        per_reading_unit_wait_ms=180,
    )


def source(code: str = 'H', context: int = 1, *, address: int = 1,
           pid: int = 101) -> ThreadSource:
    return ThreadSource(pid, address, context, context + 1, code, code)


SOURCE = SimpleNamespace(hook_code='H', context=1, context2=2)


def controller(*, mode: AdvanceMode = AdvanceMode.TEXT, tts: bool = False,
               fallback: bool = True, ready: bool = True):
    sent: list[int] = []
    readiness = {'ready': ready}
    ctrl = Controller(
        lambda _target, key: sent.append(key),
        lambda _target: readiness['ready'],
        TEST_G2P,
    )
    ctrl.begin(profile(mode=mode, tts=tts, fallback=fallback), Target(101, 202), 'host', 1)
    return ctrl, sent, readiness


def observe(ctrl: Controller, text: str, sequence: int = 1, *, pid: int = 101,
            generation: str = 'host', connection: int = 1):
    return ctrl.observe(
        generation=generation, connection=connection, sequence=sequence,
        source=SOURCE, text=text, pid=pid,
    )


def app_with_game():
    catalog = Catalog()
    game = catalog.create_game('Game')
    saves: list[str] = []
    app = AutoAdvApplication(
        catalog,
        lambda: saves.append('save'),
        input_send=lambda *_: None,
        target_ready=lambda *_: True,
        g2p=TEST_G2P,
    )
    app.select_profile(game.id, game.default_profile)
    return app, saves


class _Child:
    def poll(self):
        return None


class _Host:
    def __init__(self):
        self.child = _Child()
        self.commands = []

    def command(self, action, **kwargs):
        self.commands.append((action, kwargs))


def connect_hook(app: AutoAdvApplication, *, pid: int = 101, hwnd: int = 500,
                 generation: str = 'host', connection: int = 1):
    app.select_primary_target(pid, hwnd)
    app.hook_host = _Host()
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', generation, connection, 1, 10, process_id=pid))


def runtime_hook(app: AutoAdvApplication, source_: ThreadSource, text: str, sequence: int,
                 *, received_ns: int = 1, generation: str = 'host',
                 connection: int = 1, pid: int = 101):
    before = app.runtime_state().token
    app.handle_hook_event(HookEvent(
        'output', generation, connection, sequence, received_ns, pid, source_, text,
    ))
    after = app.runtime_state().token
    return after if after != before else None


def capture_group(app: AutoAdvApplication, events):
    app.capture.observe_group(events)


def staged_copy(p: Profile, **changes) -> Profile:
    q = deepcopy(p)
    for name, value in changes.items():
        setattr(q, name, value)
    return q
