"""Application orchestration and state-ownership contracts across product domains."""
from __future__ import annotations

from copy import deepcopy

import pytest

from autoadv.application import AutoAdvApplication
from autoadv.utterance_progress import (
    HumanVocalEvidence, LexicalProgressEvidence, VocalRecurrenceEvidence,
)
from autoadv.conversation import Target
from autoadv.hook_protocol import HookEvent
from autoadv.model import AdvanceMode, HookSelection
from autoadv.storage import Catalog
from autoadv.utterance_progress import NativeAudioObservation
from tests.contracts_support import app_with_game, capture_group, connect_hook, runtime_hook, source
from tests.g2p_support import TEST_G2P


def test_capture_workspace_composes_fields_only_within_observed_group_and_preserves_source_content():
    app, _ = app_with_game()
    speaker = source('S', 1)
    body = source('B', 5, address=2)
    speaker_raw = 'きぬ　（０４０６）■■壊れた本文■■'
    body_raw = '???「あれカレー屋を荒らしてくれた女だ」'
    app.capture.observe(speaker, speaker_raw, sequence=1)
    app.capture.observe(body, body_raw, sequence=2)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    app.set_capture_field_source('body', body.identity, body.address)

    capture_group(app, (
        HookEvent('output', 'host', 1, 1, 100, 101, speaker, speaker_raw),
        HookEvent('output', 'host', 1, 2, 110, 101, body, body_raw),
    ))
    capture_group(app, (
        HookEvent('output', 'host', 1, 3, 200, 101, body, '二つ目'),
    ))
    view = app.capture_view()
    assert [(x.speaker, x.body) for x in view.assembly] == [
        ('きぬ', 'あれカレー屋を荒らしてくれた女だ'),
        ('', '二つ目'),
    ]

    raw = '  ＡＢＣ１２３　ﾊﾟﾋﾟﾌﾟ  \n'
    untouched = source('RAW', 9, address=3)
    app.capture.observe(untouched, raw, sequence=4)
    sample = app.capture_candidate(untouched.identity, untouched.address)
    assert sample.history == (raw,)
    assert sample.samples[0].raw == raw and sample.samples[0].body == raw


def test_capture_recipe_is_a_transaction_and_ambiguous_or_failed_commit_never_changes_profile():
    app, saves = app_with_game()
    candidate = source('T', 1)
    app.capture.observe(candidate, '大和「台詞」', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    assert app.current_profile().hooks == [] and app.capture_view().dirty and saves == []

    app.capture.observe(source('T', 1, address=2), '別出力', sequence=2)
    assert not app.capture_view().can_commit
    app.capture.remove(candidate.identity, 2)
    assert app.capture_view().can_commit
    updated = app.commit_capture()
    assert [h.code for h in updated.hooks] == ['T']
    assert updated.body_source == candidate.identity and saves == ['save']

    catalog = Catalog()
    game = catalog.create_game('Game')
    failing = AutoAdvApplication(
        catalog, lambda: (_ for _ in ()).throw(OSError('disk full')),
        input_send=lambda *_: None, target_ready=lambda *_: True, g2p=TEST_G2P,
    )
    failing.select_profile(game.id, game.default_profile)
    recovery = source('RECOVERY', 7)
    failing.capture.observe(recovery, 'きぬ「本文」', sequence=1)
    failing.set_capture_field_source('body', recovery.identity, recovery.address)
    before = failing.capture_view()
    with pytest.raises(OSError):
        failing.commit_capture()
    assert failing.capture_view() == before and failing.capture_view().dirty
    assert failing.current_profile().hooks == []

    # Recovery-discovered hooks are trial state until a committed field actually
    # adopts them; merely inserting or previewing one must not persist it.
    recovery_app, _ = app_with_game()
    trial = source('RECOVERY', 7)
    normal = source('NORMAL', 8, address=2)
    connect_hook(recovery_app)
    assert recovery_app.insert_recovery_hook(trial.hook_code, discovered=True)
    recovery_app.capture.observe(trial, '試用本文', sequence=1)
    recovery_app.capture.observe(normal, '採用本文', sequence=2)
    assert recovery_app.current_profile().inserted_hooks == []
    recovery_app.set_capture_field_source('body', trial.identity, trial.address)
    recovery_app.set_capture_field_source('body', normal.identity, normal.address)
    committed = recovery_app.commit_capture()
    assert committed.body_source == normal.identity
    assert committed.inserted_hooks == []


def test_hook_connection_identity_owns_capture_and_stale_or_disconnected_output_is_inert():
    app, _ = app_with_game()
    p = app.current_profile()
    p.hook_flush_ms = 850
    p.hook_codepage = 932
    p.hook_max_buffer = 7500
    p.hook_max_history = 500000
    p.insert_pc_hooks = True
    p.inserted_hooks = ['HS@456']
    app.select_primary_target(101, 500)
    class Child:
        def poll(self): return None
    class Host:
        def __init__(self): self.child = Child(); self.commands = []
        def command(self, action, **kwargs): self.commands.append((action, kwargs))
        def close(self): pass
    host = Host()
    app.hook_host = host
    assert app.activate_selected_target() == 'connecting'
    assert [a for a, _ in host.commands] == ['settings', 'connect']

    app.handle_hook_event(HookEvent('connected', 'host-a', 7, 1, 10, 101))
    assert [a for a, _ in host.commands] == [
        'settings', 'connect', 'settings', 'insert_pc_hooks', 'insert_hook']
    first_settings = host.commands[0][1]
    restored_settings = host.commands[2][1]
    assert first_settings == restored_settings == {
        'flush_ms': 850, 'codepage': 932, 'max_buffer': 7500, 'max_history': 500000}
    hook = source('T', 1)
    app.handle_hook_event(HookEvent('output', 'host-a', 7, 2, 20, 101, hook, '本文'))
    app.handle_hook_event(HookEvent('output', 'host-a', 6, 3, 30, 101, hook, '旧本文'))
    assert app.capture_candidate(hook.identity, hook.address).history == ('本文',)

    app.handle_hook_event(HookEvent('disconnected', 'host-a', 7, 4, 40, 101))
    app.handle_hook_event(HookEvent('output', 'host-a', 7, 5, 50, 101, hook, '切断後'))
    assert app.capture_view().candidates == ()


def test_runtime_publishes_selected_provenance_and_utterance_before_presentation_side_effects():
    app, _ = app_with_game()
    profile = app.current_profile()
    selected = source('T', 1)
    unselected = source('X', 7, address=2)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = selected.identity
    app.select_profile(app.game_id, app.profile_id)

    timeline = []
    app.configure_runtime_loop(schedule=lambda delay, callback: timeline.append(('schedule', delay, callback)))
    app.configure_telemetry(lambda name, token, detail: timeline.append(('event', name, token, dict(detail))))
    connect_hook(app)
    timeline.clear()

    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, unselected, '候補'))
    app.handle_hook_event(HookEvent('output', 'host', 0, 3, 30, 101, selected, '旧台詞'))
    assert not [x for x in timeline if x[0] == 'event' and x[1] == 'selected_hook_output']

    token = runtime_hook(app, selected, '現在の台詞', 4, received_ns=40)
    assert token is not None
    names = [x[1] for x in timeline if x[0] == 'event']
    assert names.index('selected_hook_output') < names.index('utterance')
    utterance_index = next(i for i, x in enumerate(timeline) if x[0] == 'event' and x[1] == 'utterance')
    schedule_index = next(i for i, x in enumerate(timeline) if x[0] == 'schedule')
    assert utterance_index < schedule_index


def test_application_owns_audio_fallback_deadline_without_gui_policy():
    app, _ = app_with_game()
    p = app.current_profile()
    text = source('T', 1)
    p.hooks = [HookSelection('T', 1, 2)]
    p.body_source = text.identity
    app.select_profile(app.game_id, app.profile_id)
    app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    app.set_profile_setting('uncertain_text_fallback', False)
    p = app.current_profile()
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app)
    token = runtime_hook(app, text, '本文', 2)
    assert token is not None
    app.set_auto_advance(True)
    deadline = next(callback for delay, callback in scheduled if delay == app.runtime_state().utterance.text_wait_ms)

    class RetainedNative:
        pid = 101
        def capture_status(self): return {}
        def finish_line(self, _token): raise AssertionError('inconclusive audio must remain active')
    app._native_audio = RetainedNative()
    deadline()
    state = app.runtime_state()
    assert state.pause_reason == 'audio_judgment_unknown_hold'
    assert state.authorized and not state.sent

    # The Application must propagate non-terminal progress into Controller
    # state before the fallback deadline; otherwise an active finite voice
    # would be indistinguishable from unresolved audio at the policy boundary.
    active_app, _ = app_with_game()
    ap = active_app.current_profile()
    active_source = source('ACTIVE', 2)
    ap.hooks = [HookSelection('ACTIVE', 2, 3)]
    ap.body_source = active_source.identity
    active_app.select_profile(active_app.game_id, active_app.profile_id)
    active_app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    active_app.set_profile_setting('uncertain_text_fallback', True)
    active_scheduled = []
    active_app.configure_runtime_loop(
        schedule=lambda delay, callback: active_scheduled.append((delay, callback)))
    connect_hook(active_app)
    active_token = runtime_hook(active_app, active_source, '短い本文', 2)
    assert active_token is not None
    active_app._native_audio = RetainedNative()
    active_app._consume_native_observation(active_token, NativeAudioObservation(
        kind='acoustic', capture_start_ms=1000, capture_end_ms=1500,
        lexical=LexicalProgressEvidence(
            target_units=5, aligned_target_units=3, target_associated=True,
            complete_alignment=False, committed_target_end=False,
            target_start_ms=1050, aligned_end_ms=1300, target_end_ms=None,
            reading_rank=0, path_margin=3.0, boundary_margin=0.0),
        vocal=HumanVocalEvidence(
            start_ms=1000, end_ms=1500, frame_count=20, mean_probability=.8,
            peak_probability=.95, active_fraction=.8, first_active_ms=1050,
            last_active_ms=1490, active_intervals=((1050, 1490),),
            post_causal_onset_ms=1050),
        recurrence=VocalRecurrenceEvidence(), causal_lower_bound_ms=1000,
        target_attached_ms=1100))
    active_app.set_auto_advance(True)
    active_deadline = next(
        callback for delay, callback in active_scheduled
        if delay == active_app.runtime_state().utterance.text_wait_ms)
    active_deadline()
    active_state = active_app.runtime_state()
    assert active_state.pause_reason == 'audio_judgment_active_hold'
    assert active_state.authorized and not active_state.sent and not active_state.completed

    text_app, _ = app_with_game()
    text_source = source('TEXT', 3)
    tp = text_app.current_profile()
    tp.hooks = [HookSelection('TEXT', 3, 4)]
    tp.body_source = text_source.identity
    text_app.select_profile(text_app.game_id, text_app.profile_id)
    scheduled = []
    text_app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(text_app)
    text_token = runtime_hook(text_app, text_source, '本文', 2)
    text_app.set_auto_advance(True)
    presentation_delay, presentation = scheduled.pop(0)
    assert presentation_delay == text_app.runtime_state().utterance.text_wait_ms
    presentation()
    timeout_delay, timeout = scheduled.pop(0)
    assert timeout_delay == tp.next_line_timeout_ms
    timeout()
    assert text_app.runtime_state().token == text_token
    assert text_app.runtime_state().authorized
    assert text_app.runtime_state().pause_reason == 'next_line_timeout'


def test_persistence_commands_are_atomic_and_user_settings_hide_storage_mechanics():
    app, saves = app_with_game()
    p = app.current_profile()
    p.inherit_common = ['reading_ms']
    app.catalog.common.reading_ms = 900
    assert app.effective_setting('reading_ms') == (900, 'common')
    updated = app.set_profile_setting('reading_ms', 1200)
    assert updated.reading_ms == 1200 and 'reading_ms' not in updated.inherit_common
    app.reset_profile_setting('reading_ms')
    assert app.effective_setting('reading_ms') == (900, 'common')
    assert saves == ['save', 'save']

    catalog = Catalog()
    game = catalog.create_game('Game')
    second = catalog.add_profile(game.id, 'Second')
    original_default = game.default_profile
    failing = AutoAdvApplication(
        catalog, lambda: (_ for _ in ()).throw(OSError('disk full')),
        input_send=lambda *_: None, target_ready=lambda *_: True, g2p=TEST_G2P,
    )
    failing.select_profile(game.id, second.id)
    original_profiles = set(game.profiles)
    for operation in (
        lambda: failing.add_profile('New'),
        lambda: failing.rename_current_profile('Renamed'),
        lambda: failing.set_default_profile(),
    ):
        with pytest.raises(OSError): operation()
    assert set(game.profiles) == original_profiles
    assert failing.current_profile().name == 'Second'
    assert game.default_profile == original_default


def test_play_view_exposes_only_actionable_runtime_holds():
    app, _ = app_with_game()
    app.controller.authorized = True
    expected = {
        None: None,
        'audio_judgment_unknown_hold': 'open_audio_policy',
        'audio_judgment_active_hold': 'open_audio_policy',
        'ocr_failure': 'open_capture',
        'presenting': None,
        'tts_playback_failed': 'open_voice',
    }
    for reason, action in expected.items():
        app.controller.hold_reason = reason
        issue = app.play_view().issue
        assert (issue.action if issue else None) == action

def test_application_owns_external_worker_lifecycles_and_stale_results_cannot_cross_sessions(monkeypatch):
    import autoadv.application as application_module
    from types import SimpleNamespace

    class ImmediateThread:
        def __init__(self, *, target, daemon, name): self.target = target
        def start(self): self.target()

    class FakeCapture:
        def __init__(self, hwnd, pid): self.hwnd, self.pid = hwnd, pid
    class FakeReader:
        def __init__(self, config, capture): self.config, self.capture = config, capture
    class FakeWorker:
        instances = []
        def __init__(self, reader, on_text, on_error):
            self.on_text, self.on_error = on_text, on_error; self.started = False; self.closed = False
            self.__class__.instances.append(self)
        def start(self): self.started = True
        def close(self): self.closed = True

    monkeypatch.setattr(application_module.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(application_module, 'WindowsClientCapture', FakeCapture)
    monkeypatch.setattr(application_module, 'TesseractOCR', FakeReader)
    monkeypatch.setattr(application_module, 'OCRWorker', FakeWorker)
    app, _ = app_with_game()
    app.current_profile().source = 'ocr'
    app.select_primary_target(101, 500)
    delayed, events = [], []
    app.configure_runtime_loop(
        schedule=lambda delay, callback: callback() if delay == 0 else delayed.append((delay, callback)),
        on_event=lambda name, token, detail: events.append((name, token, detail)),
    )
    assert app.activate_selected_target() == 'monitoring'
    worker = FakeWorker.instances[-1]
    assert worker.started
    worker.on_text(3, '台詞')
    assert app.runtime_state().utterance.raw == '台詞'
    assert delayed
    worker.on_error('capture failed')
    assert app.runtime_state().pause_reason == 'ocr_failure'
    app.stop_monitoring('session_changed')
    assert worker.closed

    class DeferredThread:
        pending = []
        def __init__(self, *, target, daemon, name): self.target = target
        def start(self): self.__class__.pending.append(self.target)
    class FakeServer:
        instances = []
        def __init__(self, root, *, url):
            self.closed = False
            self.result = SimpleNamespace(status='connected', voices=('voice-a',), message='')
            self.__class__.instances.append(self)
        def connect_or_start(self): return self.result
        def close(self): self.closed = True
    class FakeJobs:
        instances = []
        def __init__(self, report, on_timing=None): self.report = report; self.cancelled = 0; self.__class__.instances.append(self)
        def start(self, *_): pass
        def cancel(self): self.cancelled += 1

    monkeypatch.setattr(application_module.threading, 'Thread', DeferredThread)
    monkeypatch.setattr(application_module, 'IrodoriServer', FakeServer)
    monkeypatch.setattr(application_module, 'VoiceJobs', FakeJobs)
    app, _ = app_with_game()
    app.configure_voice_jobs(on_result=None, on_preview=lambda *_: None)
    received = []
    app.start_irodori(received.append)
    first, first_work = FakeServer.instances[-1], DeferredThread.pending.pop(0)
    app.start_irodori(received.append)
    second, second_work = FakeServer.instances[-1], DeferredThread.pending.pop(0)
    assert first.closed
    first_work()
    assert received == []
    second_work()
    assert received[-1].voices == ('voice-a',)
    app.close_irodori()
    assert second.closed

def test_monitor_lifecycle_owns_native_audio_instance_and_uses_managed_runtime_defaults(monkeypatch):
    import autoadv.application as application_module
    import autoadv.native_runtime as native_runtime_module
    from types import SimpleNamespace

    class ImmediateThread:
        def __init__(self, *, target, daemon, name): self.target = target
        def start(self): self.target()
    class FakeNative:
        instances = []
        def __init__(self, pid, helper, model, vocab, notify, **callbacks):
            self.pid, self.helper, self.model, self.vocab = pid, helper, model, vocab
            self.callbacks = callbacks; self.started = False; self.closed = False
            self.__class__.instances.append(self)
        def start(self): self.started = True
        def close(self): self.closed = True
        def cancel(self): pass
        def boundary(self): pass
        def capture_status(self): return {'packets': 0}

    managed = SimpleNamespace(
        helper_dll='C:/runtime/helper.dll',
        alignment_model='C:/runtime/model.onnx',
        alignment_vocab='C:/runtime/vocab.json',
    )
    monkeypatch.setattr(application_module.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(application_module, 'NativeAudio', FakeNative)
    monkeypatch.setattr(native_runtime_module, 'managed_paths', lambda: managed)
    app, _ = app_with_game()
    app.current_profile().source = 'ocr'
    app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    app.select_primary_target(101, 500)
    app.select_audio_target(202)
    monkeypatch.setattr(app, '_start_ocr', lambda _profile: True)
    app.configure_runtime_loop(schedule=lambda _delay, callback: callback())
    assert app.activate_selected_target() == 'monitoring'
    native = FakeNative.instances[-1]
    assert native.started and native.pid == 202
    assert (str(native.helper), str(native.model), str(native.vocab)) == (
        managed.helper_dll, managed.alignment_model, managed.alignment_vocab)
    assert {'on_pcm', 'on_inference', 'on_observation'} <= set(native.callbacks)
    app.stop_monitoring('session_changed')
    assert native.closed and not app.native_audio_available


def test_selected_capture_source_and_catalog_replacement_invalidate_only_the_owned_state():
    app, _ = app_with_game()
    profile = app.current_profile()
    primary = source('A', 1, address=1)
    other = source('B', 3, address=2)
    profile.hooks = [HookSelection('A', 1, 2), HookSelection('B', 3, 4)]
    profile.body_source = primary.identity
    app.select_profile(app.game_id, app.profile_id)
    connect_hook(app)

    runtime_hook(app, primary, 'A', 2, received_ns=20)
    app.handle_hook_event(HookEvent('output', 'host', 1, 3, 25, 101, other, 'B'))
    app.handle_hook_event(HookEvent('removed', 'host', 1, 4, 30, 101, primary))
    assert app.runtime_state().profile is None
    assert app.capture_view().problem is not None
    # An unselected provider cannot silently become the body source.
    app.handle_hook_event(HookEvent('output', 'host', 1, 5, 40, 101, other, 'B2'))
    assert app.runtime_state().profile is None

    replacement = Catalog()
    game = replacement.create_game('Imported')
    app.install_catalog(replacement)
    assert app.catalog is replacement
    assert app.current_profile() is None and app.capture is None
    selected = app.select_profile(game.id, game.default_profile)
    assert selected is replacement.games[game.id].profiles[game.default_profile]


def test_application_owns_runtime_and_preview_voice_jobs(monkeypatch):
    import autoadv.application as application_module

    class FakeJobs:
        instances = []
        def __init__(self, report, on_timing=None):
            self.report = report
            self.starts = []
            self.cancelled = 0
            self.__class__.instances.append(self)
        def start(self, token, url, reading, voice, caption=''):
            self.starts.append((token, url, reading, voice, caption))
        def cancel(self):
            self.cancelled += 1

    monkeypatch.setattr(application_module, 'VoiceJobs', FakeJobs)
    app, _ = app_with_game()
    text = source('T', 1)
    p = app.current_profile()
    p.hooks = [HookSelection('T', 1, 2)]
    p.body_source = text.identity
    p.speaker_tts['大和'] = True
    p.speaker_voices['大和'] = 'voice-1'
    from autoadv.model import SpeakerStyle
    p.speaker_styles['大和'] = SpeakerStyle(age='young', gender='feminine', timbre='soft')
    app.select_profile(app.game_id, app.profile_id)
    connect_hook(app)
    app.configure_runtime_loop(schedule=lambda _delay, callback: callback())
    results, previews = [], []
    app.configure_voice_jobs(on_result=lambda *args: results.append(args),
                             on_preview=lambda *args: previews.append(args))
    runtime_jobs, preview_jobs = FakeJobs.instances

    runtime_hook(app, text, '大和「読み」', 2)
    token = app.runtime_state().token
    assert runtime_jobs.starts == [(
        token, app.catalog.irodori_url, '読み', 'voice-1', '若い女性の、柔らかい声。')]
    app.start_voice_preview('試聴', 'voice-2')
    assert preview_jobs.starts[0][1:] == (app.catalog.irodori_url, '試聴', 'voice-2', '')

    runtime_jobs.report(token, True, '')
    preview_jobs.report(preview_jobs.starts[0][0], False, 'cancelled')
    assert results == [(token, True, '')]
    assert previews == [(False, 'cancelled')]
    app.stop_monitoring('session_changed')
    app.cancel_voice_preview()
    assert runtime_jobs.cancelled >= 1 and preview_jobs.cancelled >= 1


def test_selected_capture_providers_own_luna_sync_and_connected_profile_configuration():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    app.select_primary_target(101, 500)
    from tests.contracts_support import _Host
    app.hook_host = _Host()
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host', 1, 1, 10, 101))

    app.capture.observe(body, '本文', sequence=1)
    app.capture.observe(speaker, '大和', sequence=2)
    app.set_capture_extraction_preset(body.identity, body.address, 'body')
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    enabled = [kwargs['source']['hook_code'] for action, kwargs in app.hook_host.commands
               if action == 'sync_thread' and kwargs['enabled']]
    assert enabled == ['T', 'S']

    app.hook_host.commands.clear()
    app.set_capture_field_source('speaker', None)
    disabled = {kwargs['source']['hook_code'] for action, kwargs in app.hook_host.commands
                if action == 'sync_thread' and not kwargs['enabled']}
    assert disabled == {'T', 'S'}

    game_id = app.game_id
    second = app.add_profile('Alt')
    second.hook_flush_ms = 222
    second.hook_codepage = 65001
    second.inserted_hooks = ['H2']
    app.hook_host.commands.clear()
    app.select_profile(game_id, second.id)
    assert app.activate_selected_target() == 'connected'
    settings = [kwargs for action, kwargs in app.hook_host.commands if action == 'settings']
    assert settings == [{
        'flush_ms': 222, 'codepage': 65001,
        'max_buffer': second.hook_max_buffer, 'max_history': second.hook_max_history,
    }]
    assert ('insert_hook', {'pid': 101, 'code': 'H2'}) in app.hook_host.commands
