from tests.g2p_support import TEST_G2P, FakeG2P

from autoadv.application import AutoAdvApplication
from autoadv.conversation import Target, Token
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import AdvanceMode, HookSelection, ReplacementRule, StructureRule
from autoadv.storage import Catalog


def source(code, context, address=1):
    return ThreadSource(101, address, context, context + 1, code, code)




def runtime_hook(app, source_, text, sequence, *, received_ns=1, generation='host', connection=1, pid=101):
    before = app.runtime_state().token
    app.handle_hook_event(
        HookEvent('output', generation, connection, sequence, received_ns, pid, source_, text))
    after = app.runtime_state().token
    return after if after != before else None


def capture_group(app, events):
    # CaptureWorkspace is the domain under test here; Application runtime uses
    # the same bounded group through handle_hook_event()/flush_hook_outputs().
    app.capture.observe_group(events)



class _Child:
    def poll(self):
        return None


class _Host:
    def __init__(self):
        self.child = _Child()
        self.commands = []

    def command(self, action, **kwargs):
        self.commands.append((action, kwargs))


def connect_hook(app, profile, *, pid=101, hwnd=500, generation='host', connection=1):
    app.select_primary_target(pid, hwnd)
    app.hook_host = _Host()
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', generation, connection, 1, 10, process_id=pid))


def app_with_game():
    catalog = Catalog()
    game = catalog.create_game('Game')
    calls = []
    app = AutoAdvApplication(
        catalog, lambda: calls.append('save'),
        input_send=lambda *_: None, target_ready=lambda *_: True, g2p=TEST_G2P)
    app.select_profile(game.id, game.default_profile)
    return app, calls


def test_capture_workspace_observes_real_output_without_manual_refresh():
    app, _ = app_with_game()
    a = source('A', 1)
    app.capture.observe(a, '大和「一つ目」', sequence=1)
    app.capture.observe(a, '大和「二つ目」', sequence=2)
    view = app.capture_view()
    assert len(view.candidates) == 1
    candidate = view.candidates[0]
    assert candidate.history == ('大和「一つ目」', '大和「二つ目」')
    assert [(sample.speaker, sample.body) for sample in candidate.samples] == [
        ('大和', '一つ目'), ('大和', '二つ目')]
    assert candidate.used_for == ()


def test_one_source_can_supply_both_speaker_and_body_without_exclusive_role():
    app, _ = app_with_game()
    combined = source('COMBINED', 1)
    raw = 'きぬ　（０４０６）あれカレー屋を荒らしてくれた女だ...... ボク達と同じ学校だったんだ'
    app.capture.observe(combined, raw, sequence=1)
    app.set_capture_field_source('body', combined.identity, combined.address)
    # Speaker source left automatic: speaker candidate from the chosen body source.
    view = app.capture_view()
    assert view.can_commit
    assert view.body_source == combined.identity
    assert view.speaker_source is None
    assert view.candidates[0].used_for == ('body', 'speaker')
    assert len(view.assembly) == 1
    assert view.assembly[0].speaker == 'きぬ'
    assert view.assembly[0].body == 'あれカレー屋を荒らしてくれた女だ...... ボク達と同じ学校だったんだ'


def test_explicit_same_source_supplies_both_fields_in_runtime_too():
    app, _ = app_with_game()
    combined = source('COMBINED', 1)
    profile = app.current_profile()
    profile.hooks = [HookSelection('COMBINED', 1, 2)]
    profile.body_source = combined.identity
    profile.speaker_source = combined.identity
    app.select_profile(app.game_id, app.profile_id)
    connect_hook(app, app.current_profile())

    token = runtime_hook(app, combined, '大和「おはよう」', 2, received_ns=20)

    assert token is not None
    assert app.runtime_state().utterance.speaker == '大和'
    assert app.runtime_state().utterance.text == 'おはよう'


def test_different_sources_can_supply_only_the_correct_field_from_each_mixed_output():
    app, _ = app_with_game()
    speaker = source('S', 1)
    body = source('B', 5, address=2)
    speaker_raw = 'きぬ　（０４０６）■■壊れた本文■■'
    body_raw = '???「あれカレー屋を荒らしてくれた女だ」'
    app.capture.observe(speaker, speaker_raw, sequence=1)
    app.capture.observe(body, body_raw, sequence=2)
    # Both sources yield both candidates.  Final assembly chooses fields, not a
    # mutually-exclusive role for each hook.
    speaker_sample = app.capture_candidate(speaker.identity, speaker.address).samples[-1]
    body_sample = app.capture_candidate(body.identity, body.address).samples[-1]
    assert (speaker_sample.speaker, speaker_sample.body) == ('きぬ', '■■壊れた本文■■')
    assert (body_sample.speaker, body_sample.body) == ('???', 'あれカレー屋を荒らしてくれた女だ')
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    app.set_capture_field_source('body', body.identity, body.address)
    capture_group(app, (
        HookEvent('output', 'host', 1, 1, 100, 101, speaker, speaker_raw),
        HookEvent('output', 'host', 1, 2, 110, 101, body, body_raw),
    ))
    view = app.capture_view()
    assert [(item.speaker, item.body) for item in view.assembly] == [
        ('きぬ', 'あれカレー屋を荒らしてくれた女だ')]
    updated = app.commit_capture()
    assert updated.body_source == body.identity
    assert updated.speaker_source == speaker.identity
    assert {hook.identity for hook in updated.hooks} == {body.identity, speaker.identity}




def test_speaker_enclosed_body_option_uses_existing_speaker_field_for_preview_and_runtime():
    app, calls = app_with_game()
    speaker = source('SPEAKER', 1)
    body = source('BODY', 5, address=2)
    app.capture.observe(speaker, 'A', sequence=1)
    app.capture.observe(body, 'A（心の声）', sequence=2)
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    app.set_capture_extraction_preset(body.identity, body.address, 'body')
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_unwrap_speaker_enclosed_body(True)

    capture_group(app, (
        HookEvent('output', 'host', 1, 1, 100, 101, speaker, 'A'),
        HookEvent('output', 'host', 1, 2, 110, 101, body, 'A（心の声）'),
    ))
    view = app.capture_view()
    assert view.unwrap_speaker_enclosed_body
    assert [(item.speaker, item.body) for item in view.assembly] == [('A', '心の声')]

    updated = app.commit_capture()
    assert updated.unwrap_speaker_enclosed_body
    assert calls == ['save']

    controller = __import__('autoadv.conversation', fromlist=['Controller']).Controller(
        lambda *_: None, lambda *_: True, TEST_G2P)
    controller.begin(updated, Target(101, 500), 'host', 1)
    token = controller.observe_group((
        HookEvent('output', 'host', 1, 3, 120, 101, speaker, 'A'),
        HookEvent('output', 'host', 1, 4, 130, 101, body, 'A「通常の台詞」'),
    ))
    assert token is not None
    assert controller.utterance.speaker == 'A'
    assert controller.utterance.text == '通常の台詞'


def test_speaker_enclosed_body_option_reuses_existing_registered_speaker_without_new_speaker_state():
    app, _ = app_with_game()
    body = source('BODY', 5, address=2)
    app.current_profile().speaker_tts['A'] = False
    app.capture.observe(body, 'A（心の声）', sequence=1)
    app.set_capture_extraction_preset(body.identity, body.address, 'body')
    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_unwrap_speaker_enclosed_body(True)

    view = app.capture_view()
    assert [(item.speaker, item.body) for item in view.assembly] == [('A', '心の声')]


def test_speaker_enclosed_body_option_does_not_reinterpret_supplement_or_ruby_like_text():
    app, _ = app_with_game()
    speaker = source('SPEAKER', 1)
    body = source('BODY', 5, address=2)
    app.capture.observe(speaker, 'A', sequence=1)
    app.capture.observe(body, '本文（補足）', sequence=2)
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    app.set_capture_extraction_preset(body.identity, body.address, 'body')
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_unwrap_speaker_enclosed_body(True)

    for sequence, raw in enumerate(('本文（補足）', '東京（とうきょう）', 'B『別話者』'), 10):
        capture_group(app, (
            HookEvent('output', 'host', 1, sequence, 100 + sequence, 101, speaker, 'A'),
            HookEvent('output', 'host', 1, sequence + 100, 200 + sequence, 101, body, raw),
        ))
    assert [item.body for item in app.capture_view().assembly[-3:]] == [
        '本文（補足）', '東京（とうきょう）', 'B『別話者』']


def test_speaker_enclosed_body_option_handles_width_and_nested_wrappers_without_partial_strip():
    from autoadv.model import resolve_speaker_enclosed_body

    assert resolve_speaker_enclosed_body('Ａ（内心（補足））', 'A') == ('A', '内心（補足）')
    assert resolve_speaker_enclosed_body('A『独白』', 'A') == ('A', '独白')
    assert resolve_speaker_enclosed_body('A「引用」続き「末尾」', 'A') == ('A', 'A「引用」続き「末尾」')
    assert resolve_speaker_enclosed_body('本文（補足）', 'A') == ('A', '本文（補足）')
    assert resolve_speaker_enclosed_body('A（心の声）', '') == ('', 'A（心の声）')
    assert resolve_speaker_enclosed_body('A（心の声）', '', ('A',)) == ('A', '心の声')


def test_capture_recipe_does_not_persist_until_committed():
    app, calls = app_with_game()
    candidate = source('T', 1)
    app.capture.observe(candidate, '大和「台詞」', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    assert app.current_profile().hooks == []
    assert app.capture_view().dirty
    assert calls == []
    app.commit_capture()
    current = app.current_profile()
    assert [h.code for h in current.hooks] == ['T']
    assert current.body_source == candidate.identity
    assert calls == ['save']


def test_ambiguous_source_cannot_be_committed():
    app, _ = app_with_game()
    candidate = source('T', 1)
    app.capture.observe(candidate, '台詞', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    app.capture.observe(source('T', 1, address=2), '別出力', sequence=2)
    view = app.capture_view()
    assert not view.can_commit
    assert view.problem is not None


def test_source_identity_collision_and_recovery_are_owned_by_capture_workspace():
    app, _ = app_with_game()
    first = source('T', 1, address=1)
    second = source('T', 1, address=2)
    app.capture.observe(first, '台詞1', sequence=1)
    assert not app.capture.identity_is_ambiguous(first.identity)
    app.capture.observe(second, '台詞2', sequence=2)
    assert app.capture.identity_is_ambiguous(first.identity)
    assert app.capture_source(first.identity, first.address) is None
    assert app.capture_source(first.identity, first.address, require_unique=False) == first
    app.capture.remove(second.identity, second.address)
    assert not app.capture.identity_is_ambiguous(first.identity)
    assert app.capture_source(first.identity, first.address) == first


def test_recovery_trial_becomes_persistent_only_when_used_by_final_fields():
    app, _ = app_with_game()
    candidate = source('RECOVERY', 7)
    connect_hook(app, app.current_profile())
    assert app.insert_recovery_hook(candidate.hook_code, discovered=True)
    app.capture.observe(candidate, '本文', sequence=1)
    assert app.current_profile().inserted_hooks == []
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    app.commit_capture()
    assert app.current_profile().inserted_hooks == [candidate.hook_code]


def test_recovery_trial_is_not_persisted_after_switching_to_another_final_source():
    app, _ = app_with_game()
    trial = source('RECOVERY', 7)
    normal = source('NORMAL', 8, address=2)
    connect_hook(app, app.current_profile())
    assert app.insert_recovery_hook(trial.hook_code, discovered=True)
    app.capture.observe(trial, '試用本文', sequence=1)
    app.capture.observe(normal, '採用本文', sequence=2)
    app.set_capture_field_source('body', trial.identity, trial.address)
    app.set_capture_field_source('body', normal.identity, normal.address)
    app.commit_capture()
    profile = app.current_profile()
    assert profile.body_source == normal.identity
    assert [hook.code for hook in profile.hooks] == [normal.hook_code]
    assert profile.inserted_hooks == []


def test_speaker_from_later_flush_is_never_reused_for_older_body():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    app.capture.observe(body, '一つ目', sequence=1)
    app.capture.observe(speaker, '大和', sequence=2)
    app.set_capture_extraction_preset(body.identity, body.address, 'body')
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    capture_group(app, (
        HookEvent('output', 'host', 1, 1, 100, 101, body, '一つ目'),
        HookEvent('output', 'host', 1, 2, 110, 101, speaker, '大和'),
    ))
    capture_group(app, (
        HookEvent('output', 'host', 1, 3, 200, 101, body, '二つ目'),
    ))
    assert [(p.speaker, p.body) for p in app.capture_view().assembly] == [('大和', '一つ目'), ('', '二つ目')]


def test_capture_preview_uses_only_groups_observed_after_provider_sync():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    app.capture.observe(body, '古い本文', sequence=1)
    app.capture.observe(speaker, '古い話者', sequence=2)
    app.set_capture_extraction_preset(body.identity, body.address, 'body')
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, app.current_profile())

    # No providers are selected yet, so these outputs must not create a hidden
    # 100 ms group that can later be reused by Preview.
    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 100, 101, body, '同期前本文'))
    app.handle_hook_event(HookEvent('output', 'host', 1, 3, 110, 101, speaker, '同期前話者'))
    assert scheduled == []

    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    assert app.capture_view().assembly == ()

    app.handle_hook_event(HookEvent('output', 'host', 1, 4, 200, 101, body, '同期後本文'))
    app.handle_hook_event(HookEvent('output', 'host', 1, 5, 210, 101, speaker, '同期後話者'))
    assert len(scheduled) == 1 and scheduled[0][0] == 100
    scheduled.pop()[1]()
    assert [(p.speaker, p.body) for p in app.capture_view().assembly] == [('同期後話者', '同期後本文')]


def test_runtime_assembles_body_and_speaker_from_the_same_synced_hook_group():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    profile = app.current_profile()
    body_hook = HookSelection('T', 1, 2)
    speaker_hook = HookSelection('S', 5, 6)
    profile.hooks = [body_hook, speaker_hook]
    profile.body_source = body_hook.identity
    profile.speaker_source = speaker_hook.identity
    profile.source_rules[body_hook.identity] = [StructureRule(r'^(?P<body>.*)$')]
    profile.source_rules[speaker_hook.identity] = [StructureRule(r'^(?P<speaker>.*)$')]
    app.select_profile(app.game_id, app.profile_id)
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, app.current_profile())
    app.handle_hook_event(HookEvent('created', 'host', 1, 2, 20, 101, body, ''))
    app.handle_hook_event(HookEvent('created', 'host', 1, 3, 30, 101, speaker, ''))

    app.handle_hook_event(HookEvent('output', 'host', 1, 4, 100, 101, body, 'おはよう'))
    app.handle_hook_event(HookEvent('output', 'host', 1, 5, 110, 101, speaker, '大和'))
    flushes = [(delay, callback) for delay, callback in scheduled if delay == 100]
    assert len(flushes) == 1
    flushes[0][1]()
    assert app.runtime_state().utterance.text == 'おはよう'
    assert app.runtime_state().utterance.speaker == '大和'


def test_profile_setting_commands_hide_inheritance_storage_from_the_gui_contract():
    app, calls = app_with_game()
    profile = app.current_profile()
    profile.inherit_common = ['reading_ms']
    app.catalog.common.reading_ms = 900
    assert app.effective_setting('reading_ms') == (900, 'common')
    updated = app.set_profile_setting('reading_ms', 1200)
    assert updated.reading_ms == 1200
    assert 'reading_ms' not in updated.inherit_common
    assert app.effective_setting('reading_ms') == (1200, 'game')
    app.reset_profile_setting('reading_ms')
    assert app.effective_setting('reading_ms') == (900, 'common')
    assert calls == ['save', 'save']


def test_source_specific_extraction_rules_are_part_of_capture_transaction():
    app, calls = app_with_game()
    candidate = source('T', 1)
    app.capture.observe(candidate, '大和：台詞', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    original_rules = dict(app.current_profile().source_rules)
    rule = StructureRule(r'^(?P<speaker>[^:]+):(?P<body>.+)$')
    app.save_capture_source_rule(candidate.identity, candidate.address, None, rule)
    assert calls == []
    assert app.current_profile().source_rules == original_rules
    sample = app.capture_candidate(candidate.identity, candidate.address).samples[-1]
    assert (sample.speaker, sample.body) == ('大和', '台詞')
    app.commit_capture()
    assert calls == ['save']
    assert app.current_profile().source_rules[candidate.identity][0] == rule


def test_common_setting_is_persisted_by_application_without_form_reconstruction():
    app, calls = app_with_game()
    app.set_common_setting('reading_ms', 880)
    assert app.catalog.common.reading_ms == 880
    assert calls == ['save']


def test_capture_runtime_problem_is_semantic_and_recovery_action_is_explicit():
    app, _ = app_with_game()
    app.handle_capture_activation_error('native failure')
    view = app.capture_view()
    assert view.problem is not None
    assert view.problem.message == 'ゲームから会話を取得できません。取得の復旧を確認してください。'
    assert view.problem.action == 'open_capture_recovery'
    app.clear_capture_problem()
    assert app.capture_view().problem is None


def test_play_view_hides_normal_internal_state_and_surfaces_only_actionable_hold():
    app, _ = app_with_game()
    app.controller.authorized = True
    app.controller.hold_reason = None
    view = app.play_view()
    assert view.advance_authorized
    assert view.issue is None
    app.controller.hold_reason = 'native_acoustic_unconfirmed_hold'
    view = app.play_view()
    assert view.issue is not None
    assert '対象台詞の音響完了' in view.issue.message
    assert view.issue.action == 'open_audio_policy'
    app.controller.hold_reason = 'ocr_failure'
    view = app.play_view()
    assert view.issue is not None
    assert view.issue.action == 'open_capture'
    app.controller.hold_reason = 'presenting'
    assert app.play_view().issue is None


def test_play_view_keeps_tts_failure_actionable_even_when_auto_is_off():
    app, _ = app_with_game()
    app.controller.authorized = False
    app.controller.hold_reason = 'tts_playback_failed'
    view = app.play_view()
    assert view.issue is not None
    assert view.issue.action == 'open_voice'



def test_application_owns_hook_host_lifecycle_and_discards_stale_callbacks(monkeypatch):
    from types import SimpleNamespace
    import autoadv.application as application_module

    class Child:
        def poll(self):
            return None

    class FakeHost:
        instances = []

        def __init__(self, path, on_event, on_closed, on_error):
            self.path = path
            self.on_event = on_event
            self.on_closed = on_closed
            self.on_error = on_error
            self.child = None
            self.closed = False
            self.commands = []
            FakeHost.instances.append(self)

        def start(self):
            self.child = Child()

        def close(self):
            self.closed = True

        def command(self, action, **kwargs):
            self.commands.append((action, kwargs))

    monkeypatch.setattr(application_module, 'HookHost', FakeHost)
    monkeypatch.setattr(application_module, 'adopted_runtime', lambda: SimpleNamespace(host='host.exe'))
    app, _ = app_with_game()
    semantic = []
    app.configure_runtime_loop(
        schedule=lambda _delay, callback: callback(),
        on_event=lambda name, token, detail: semantic.append((name, token, detail)))

    app.start_hook_host()
    first = FakeHost.instances[-1]
    assert app.hook_host_running
    app.select_primary_target(101, 500)
    assert app.activate_selected_target() == 'connecting'
    assert first.commands[-1] == ('connect', {'pid': 101})
    first_event = HookEvent('diagnostic', 'host', 1, 1, 1, detail='first')
    first.on_event(first_event)
    assert [detail['hook_event'].detail for name, _, detail in semantic if name == 'hook_event'] == ['first']

    app.start_hook_host()
    second = FakeHost.instances[-1]
    assert first.closed
    first.on_event(HookEvent('diagnostic', 'old', 1, 2, 2, detail='stale'))
    first.on_error('stale error')
    second_event = HookEvent('diagnostic', 'new', 1, 3, 3, detail='second')
    second.on_event(second_event)
    second.on_error('native failure')
    assert [detail['hook_event'].detail for name, _, detail in semantic if name == 'hook_event'] == ['first', 'second']
    assert not any(name == 'hook_error' and detail.get('reason') == 'stale error'
                   for name, _, detail in semantic)
    assert any(name == 'hook_error' and detail.get('reason') == 'native failure'
               for name, _, detail in semantic)
    assert app.capture_view().problem is not None

    app.close_hook_host()
    assert second.closed
    assert not app.hook_host_running


def test_replacing_catalog_invalidates_old_profile_and_capture_state():
    app, _ = app_with_game()
    old_profile = app.current_profile()
    assert old_profile is not None
    replacement = Catalog()
    game = replacement.create_game('Imported')
    app.install_catalog(replacement)
    assert app.catalog is replacement
    assert app.current_profile() is None
    assert app.capture is None
    selected = app.select_profile(game.id, game.default_profile)
    assert selected is replacement.games[game.id].profiles[game.default_profile]


def test_application_starts_and_stops_native_audio_from_monitor_lifecycle(monkeypatch):
    import autoadv.application as application_module

    class ImmediateThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
        def start(self):
            self.target()

    class FakeNative:
        instances = []
        def __init__(self, pid, helper, model, vocab, notify, **callbacks):
            self.pid = pid
            self.notify = notify
            self.callbacks = callbacks
            self.started = False
            self.closed = False
            FakeNative.instances.append(self)
        def start(self):
            self.started = True
        def close(self):
            self.closed = True
        def cancel(self):
            pass
        def boundary(self):
            pass
        def capture_status(self):
            return {'packets': 0}

    monkeypatch.setattr(application_module.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(application_module, 'NativeAudio', FakeNative)
    app, _ = app_with_game()
    profile = app.current_profile()
    profile.source = 'ocr'
    app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    app.set_profile_setting('native_helper', 'helper.dll')
    app.set_profile_setting('alignment_model', 'model.onnx')
    app.set_profile_setting('alignment_vocab', 'tokens.txt')
    app.select_primary_target(101, 500)
    app.select_audio_target(202)
    monkeypatch.setattr(app, '_start_ocr', lambda _profile: True)
    runtime_events = []
    app.configure_runtime_loop(
        schedule=lambda _delay, callback: callback(),
        on_event=lambda name, token, detail: runtime_events.append((name, token, detail)))

    assert app.activate_selected_target() == 'monitoring'
    native = FakeNative.instances[-1]
    assert native.pid == 202
    assert native.started
    assert app.native_audio_available
    assert {'on_pcm', 'on_inference', 'on_observation'} <= set(native.callbacks)
    assert any(name == 'native_ready' and detail['ready']
               for name, _, detail in runtime_events)

    app.stop_monitoring('session_changed')
    assert native.closed
    assert not app.native_audio_available


def test_application_resolves_managed_native_runtime_when_profile_overrides_are_empty(monkeypatch):
    import autoadv.application as application_module
    import autoadv.native_runtime as native_runtime_module
    from types import SimpleNamespace

    class ImmediateThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
        def start(self):
            self.target()

    class FakeNative:
        instances = []
        def __init__(self, pid, helper, model, vocab, notify, **callbacks):
            self.pid, self.helper, self.model, self.vocab = pid, helper, model, vocab
            self.closed = False
            FakeNative.instances.append(self)
        def start(self):
            pass
        def close(self):
            self.closed = True
        def cancel(self):
            pass
        def boundary(self):
            pass
        def capture_status(self):
            return {'packets': 0}

    managed = SimpleNamespace(
        helper_dll='C:/AutoAdv/runtime/native/loopback/helper.dll',
        alignment_model='C:/AutoAdv/runtime/native/asr/model.int8.onnx',
        alignment_vocab='C:/AutoAdv/runtime/native/alignment/vocab.json',
    )
    monkeypatch.setattr(application_module.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(application_module, 'NativeAudio', FakeNative)
    monkeypatch.setattr(native_runtime_module, 'managed_paths', lambda: managed)
    app, _ = app_with_game()
    profile = app.current_profile()
    profile.source = 'ocr'
    app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    assert app.current_profile().native_helper == ''
    assert app.current_profile().alignment_model == ''
    assert app.current_profile().alignment_vocab == ''
    app.select_primary_target(101, 500)
    monkeypatch.setattr(app, '_start_ocr', lambda _profile: True)
    app.configure_runtime_loop(schedule=lambda _delay, callback: callback())

    assert app.activate_selected_target() == 'monitoring'
    native = FakeNative.instances[-1]
    assert str(native.helper) == managed.helper_dll
    assert str(native.model) == managed.alignment_model
    assert str(native.vocab) == managed.alignment_vocab
    assert app.native_audio_available


def test_application_owns_ocr_worker_admission_and_failure_policy(monkeypatch):
    import autoadv.application as application_module

    class ImmediateThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
        def start(self):
            self.target()

    class FakeCapture:
        def __init__(self, hwnd, pid):
            self.hwnd, self.pid = hwnd, pid

    class FakeReader:
        def __init__(self, config, capture):
            self.config, self.capture = config, capture

    class FakeWorker:
        instances = []
        def __init__(self, reader, on_text, on_error):
            self.reader, self.on_text, self.on_error = reader, on_text, on_error
            self.started = False
            self.closed = False
            FakeWorker.instances.append(self)
        def start(self):
            self.started = True
        def close(self):
            self.closed = True

    monkeypatch.setattr(application_module.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(application_module, 'WindowsClientCapture', FakeCapture)
    monkeypatch.setattr(application_module, 'TesseractOCR', FakeReader)
    monkeypatch.setattr(application_module, 'OCRWorker', FakeWorker)
    app, _ = app_with_game()
    profile = app.current_profile()
    profile.source = 'ocr'
    app.select_primary_target(101, 500)
    delayed = []
    events = []
    app.configure_runtime_loop(
        schedule=lambda delay, callback: callback() if delay == 0 else delayed.append((delay, callback)),
        on_event=lambda name, token, detail: events.append((name, token, detail)))
    assert app.activate_selected_target() == 'monitoring'
    worker = FakeWorker.instances[-1]
    assert worker.started
    worker.on_text(3, '台詞')
    assert app.runtime_state().utterance.raw == '台詞'
    assert any(name == 'utterance' for name, _, _ in events)
    assert delayed  # presentation deadline is Application-owned
    worker.on_error('capture failed')
    assert any(name == 'ocr_error' and detail['reason'] == 'capture failed'
               for name, _, detail in events)
    assert app.runtime_state().pause_reason == 'ocr_failure'
    app.stop_monitoring('session_changed')
    assert worker.closed


def test_application_owns_tts_and_preview_jobs(monkeypatch):
    import autoadv.application as application_module

    class FakeJobs:
        instances = []
        def __init__(self, report, on_timing=None):
            self.report = report
            self.on_timing = on_timing
            self.starts = []
            self.cancelled = 0
            FakeJobs.instances.append(self)
        def start(self, token, url, reading, voice):
            self.starts.append((token, url, reading, voice))
        def cancel(self):
            self.cancelled += 1

    monkeypatch.setattr(application_module, 'VoiceJobs', FakeJobs)
    app, _ = app_with_game()
    text = source('T', 1)
    profile = app.current_profile()
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    profile.speaker_tts['大和'] = True
    profile.speaker_voices['大和'] = 'voice-1'
    app.select_profile(app.game_id, app.profile_id)
    connect_hook(app, app.current_profile())
    app.configure_runtime_loop(schedule=lambda _delay, callback: callback())
    results, previews = [], []
    app.configure_voice_jobs(
        on_result=lambda *args: results.append(args),
        on_preview=lambda *args: previews.append(args))
    voice_jobs, preview_jobs = FakeJobs.instances

    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, text, '大和「読み」'))
    token = app.runtime_state().token
    assert voice_jobs.starts == [(token, app.catalog.irodori_url, '読み', 'voice-1')]

    app.start_voice_preview('試聴', 'voice-2')
    assert preview_jobs.starts[0][1:] == (app.catalog.irodori_url, '試聴', 'voice-2')
    voice_jobs.report(token, True, '')
    preview_jobs.report(preview_jobs.starts[0][0], False, 'cancelled')
    assert results == [(token, True, '')]
    assert previews == [(False, 'cancelled')]
    app.stop_monitoring('session_changed')
    app.cancel_voice_preview()
    assert voice_jobs.cancelled >= 1
    assert preview_jobs.cancelled >= 1


def test_application_owns_irodori_lifecycle_and_ignores_stale_results(monkeypatch):
    from types import SimpleNamespace
    import autoadv.application as application_module

    class DeferredThread:
        pending = []
        def __init__(self, *, target, daemon, name):
            self.target = target
        def start(self):
            self.pending.append(self.target)

    class FakeServer:
        instances = []
        def __init__(self, root, *, url):
            self.root = root
            self.url = url
            self.closed = False
            self.result = SimpleNamespace(status='connected', voices=('voice-a',), message='')
            FakeServer.instances.append(self)
        def connect_or_start(self):
            return self.result
        def close(self):
            self.closed = True

    class FakeJobs:
        instances = []
        def __init__(self, report, on_timing=None):
            self.report = report
            self.cancelled = 0
            FakeJobs.instances.append(self)
        def start(self, *_):
            pass
        def cancel(self):
            self.cancelled += 1

    monkeypatch.setattr(application_module.threading, 'Thread', DeferredThread)
    monkeypatch.setattr(application_module, 'IrodoriServer', FakeServer)
    monkeypatch.setattr(application_module, 'VoiceJobs', FakeJobs)
    app, _ = app_with_game()
    app.configure_voice_jobs(on_result=None, on_preview=lambda *_: None)
    runtime_jobs, preview_jobs = FakeJobs.instances
    received = []

    app.start_irodori(received.append)
    assert (runtime_jobs.cancelled, preview_jobs.cancelled) == (1, 1)
    first = FakeServer.instances[-1]
    first_work = DeferredThread.pending.pop(0)
    app.start_irodori(received.append)
    assert (runtime_jobs.cancelled, preview_jobs.cancelled) == (2, 2)
    second = FakeServer.instances[-1]
    second_work = DeferredThread.pending.pop(0)
    assert first.closed

    first_work()
    assert received == []
    second_work()
    assert received[-1].voices == ('voice-a',)
    assert received[-1].issue == ''

    app.close_irodori()
    assert second.closed
    assert (runtime_jobs.cancelled, preview_jobs.cancelled) == (3, 3)

def test_capture_provider_selection_drives_luna_sync_without_public_sync_api():
    class Child:
        def poll(self): return None
    class Host:
        def __init__(self): self.child = Child(); self.commands = []
        def command(self, action, **kwargs): self.commands.append((action, kwargs))

    app, _ = app_with_game()
    text = source('T', 1)
    speaker = source('S', 5, address=2)
    app.select_primary_target(101, 500)
    host = Host(); app.hook_host = host
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host', 1, 1, 10, 101))
    app.capture.observe(text, '本文', sequence=1)
    app.capture.observe(speaker, '大和', sequence=2)
    app.set_capture_extraction_preset(text.identity, text.address, 'body')
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    app.set_capture_field_source('body', text.identity, text.address)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    enables = [kwargs['source']['hook_code'] for action, kwargs in host.commands
               if action == 'sync_thread' and kwargs['enabled']]
    assert enables == ['T', 'S']

    host.commands.clear()
    app.set_capture_field_source('speaker', None)
    disables = [kwargs['source']['hook_code'] for action, kwargs in host.commands
                if action == 'sync_thread' and not kwargs['enabled']]
    assert set(disables) == {'T', 'S'}

def test_application_owns_hook_connection_identity_and_rejects_stale_events():
    class Child:
        def poll(self): return None
    class Host:
        def __init__(self): self.child = Child(); self.commands = []
        def command(self, action, **kwargs): self.commands.append((action, kwargs))

    app, _ = app_with_game()
    app.select_primary_target(101, 500)
    host = Host(); app.hook_host = host
    assert app.activate_selected_target() == 'connecting'
    assert app.activate_selected_target() == 'connecting'
    assert [action for action, _ in host.commands] == ['settings', 'connect']

    app.handle_hook_event(HookEvent('connected', 'old-host', 1, 1, 10, 999))
    assert app.activate_selected_target() == 'connecting'

    app.handle_hook_event(HookEvent('connected', 'host-a', 7, 2, 20, 101))
    assert app.activate_selected_target() == 'connected'
    hook = source('T', 1)
    current_output = HookEvent('output', 'host-a', 7, 3, 30, 101, hook, '本文')
    stale_output = HookEvent('output', 'host-a', 6, 4, 40, 101, hook, '旧本文')
    app.handle_hook_event(current_output)
    app.handle_hook_event(stale_output)
    assert app.capture_candidate(hook.identity, hook.address).history == ('本文',)

    app.handle_hook_event(HookEvent('disconnected', 'host-a', 7, 5, 50, 101))
    app.handle_hook_event(HookEvent('output', 'host-a', 7, 6, 60, 101, hook, '切断後'))
    assert app.capture_view().candidates == ()

def test_hook_host_restart_invalidates_connection_identity():
    class Child:
        def poll(self): return None
    class Host:
        def __init__(self): self.child = Child(); self.commands = []
        def command(self, action, **kwargs): self.commands.append((action, kwargs))
        def close(self): pass

    app, _ = app_with_game()
    app.select_primary_target(101, 500)
    app.hook_host = Host()
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host-a', 3, 1, 10, 101))
    hook = source('T', 1)
    output = HookEvent('output', 'host-a', 3, 2, 20, 101, hook, '本文')
    app.handle_hook_event(output)
    assert app.capture_candidate(hook.identity, hook.address).history == ('本文',)
    app.close_hook_host()
    app.handle_hook_event(HookEvent('output', 'host-a', 3, 3, 30, 101, hook, '再起動後'))
    assert app.capture_candidate(hook.identity, hook.address).history == ('本文',)

def test_changing_body_provider_recomputes_final_preview_without_changing_source_formatting():
    app, _ = app_with_game()
    first = source('A', 1, address=1)
    second = source('B', 3, address=2)
    app.capture.observe(first, 'Aの台詞', sequence=1)
    app.capture.observe(second, 'Bの台詞', sequence=2)
    app.set_capture_extraction_preset(first.identity, first.address, 'body')
    app.set_capture_extraction_preset(second.identity, second.address, 'body')
    app.set_capture_field_source('body', first.identity, first.address)
    assert [item.body for item in app.capture_view().assembly] == ['Aの台詞']
    app.set_capture_field_source('body', second.identity, second.address)
    assert [item.body for item in app.capture_view().assembly] == ['Bの台詞']
    assert app.capture_candidate(first.identity, first.address).preset == 'body'


def test_saved_monitor_starts_only_after_output_from_the_explicit_body_source():
    app, _ = app_with_game()
    primary = source('A', 1, address=1)
    unselected = source('B', 3, address=2)
    profile = app.current_profile()
    profile.hooks = [HookSelection('A', 1, 2), HookSelection('B', 3, 4)]
    profile.body_source = primary.identity
    app.select_profile(app.game_id, app.profile_id)
    app.configure_runtime_loop(schedule=lambda _delay, callback: callback())
    connect_hook(app, app.current_profile())

    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, unselected, 'other line'))
    assert app.runtime_state().profile is None
    app.handle_hook_event(HookEvent('output', 'host', 1, 3, 30, 101, primary, 'selected line'))
    assert app.runtime_state().profile is not None
    assert app.runtime_state().utterance.raw == 'selected line'

def test_runtime_target_identity_is_application_session_state():
    app, _ = app_with_game()
    state = app.select_primary_target(101, 0x1234)
    assert state.hook_pid == state.audio_pid == state.input_pid == 101
    assert state.hwnd == 0x1234
    app.select_audio_target(202)
    app.select_input_target(303, 0x5678)
    state = app.runtime_target_state()
    assert (state.hook_pid, state.audio_pid, state.input_pid, state.input_hwnd) == (101, 202, 303, 0x5678)
    app.clear_runtime_target()
    assert not app.runtime_target_state().selected


def test_profile_selection_invalidates_pending_capture_group_epoch():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    app.capture.observe(body, '本文', sequence=1)
    app.capture.observe(speaker, '話者', sequence=2)
    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, app.current_profile())
    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, body, '旧本文'))
    app.handle_hook_event(HookEvent('output', 'host', 1, 3, 30, 101, speaker, '旧話者'))
    assert scheduled
    old_flush = scheduled[-1][1]
    game_id, profile_id = app.game_id, app.profile_id
    app.select_profile(game_id, profile_id)
    old_flush()
    assert app.capture_view().assembly == ()

def test_runtime_rebuild_setting_does_not_pause_or_rewrite_active_utterance():
    app, _ = app_with_game()
    text = source('T', 1)
    profile = app.current_profile()
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    app.select_profile(app.game_id, app.profile_id)
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, app.current_profile())
    app.set_auto_advance(True)

    token = runtime_hook(app, text, 'first', 2, received_ns=100)
    assert token is not None
    assert app.runtime_state().utterance.advance_mode == AdvanceMode.TEXT
    text_timer = next(callback for delay, callback in scheduled if delay > 0)

    app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    state = app.runtime_state()
    assert state.token == token
    assert state.utterance.advance_mode == AdvanceMode.TEXT
    assert state.pause_reason is None

    text_timer()
    assert app.runtime_state().sent


def test_capture_transition_rebuilds_only_at_next_distinct_hook_line():
    app, _ = app_with_game()
    text = source('T', 1)
    profile = app.current_profile()
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    app.select_profile(app.game_id, app.profile_id)
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, app.current_profile())
    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 100, 101, text, 'first'))
    first_session = app.runtime_state().session
    assert app.runtime_state().utterance.raw == 'first'

    app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    app.handle_hook_event(HookEvent('output', 'host', 1, 3, 200, 101, text, 'first'))
    assert app.runtime_state().session == first_session
    app.handle_hook_event(HookEvent('output', 'host', 1, 4, 300, 101, text, 'second'))
    assert app.runtime_state().session > first_session
    assert app.runtime_state().utterance.raw == 'second'

def test_runtime_target_alternates_reset_to_primary_without_losing_primary_window():
    app, _ = app_with_game()
    app.select_primary_target(101, 0x1234)
    app.select_audio_target(202)
    app.select_input_target(303, 0x5678)
    state = app.select_audio_target(None)
    assert state.audio_pid == 101
    state = app.select_input_target(None, None)
    assert (state.input_pid, state.hwnd, state.hook_hwnd) == (101, 0x1234, 0x1234)
    state = app.runtime_target_state()
    assert (state.hook_pid, state.audio_pid, state.input_pid, state.input_hwnd) == (101, 101, 101, 0x1234)


def test_explicit_input_override_requires_a_real_window():
    app, _ = app_with_game()
    app.select_primary_target(101, 0x1234)
    import pytest
    with pytest.raises(ValueError):
        app.select_input_target(303, 0)
    with pytest.raises(ValueError):
        app.select_input_target(303, None)


def test_catalog_crud_rolls_back_when_persistence_fails():
    catalog = Catalog()
    game = catalog.create_game('Game')
    second = catalog.add_profile(game.id, 'Second')
    original_default = game.default_profile
    app = AutoAdvApplication(
        catalog, lambda: (_ for _ in ()).throw(OSError('disk full')),
        input_send=lambda *_: None, target_ready=lambda *_: True, g2p=TEST_G2P)
    app.select_profile(game.id, second.id)
    original_profiles = set(game.profiles)
    import pytest
    with pytest.raises(OSError):
        app.add_profile('New')
    assert set(game.profiles) == original_profiles
    with pytest.raises(OSError):
        app.rename_current_profile('Renamed')
    assert app.current_profile().name == 'Second'
    with pytest.raises(OSError):
        app.set_default_profile()
    assert game.default_profile == original_default


def test_capture_commit_failure_keeps_the_workspace_draft_intact():
    catalog = Catalog()
    game = catalog.create_game('Game')
    app = AutoAdvApplication(
        catalog, lambda: (_ for _ in ()).throw(OSError('disk full')),
        input_send=lambda *_: None, target_ready=lambda *_: True, g2p=TEST_G2P)
    app.select_profile(game.id, game.default_profile)
    candidate = source('RECOVERY', 7)
    connect_hook(app, app.current_profile())
    assert app.insert_recovery_hook(candidate.hook_code, discovered=True)
    app.capture.observe(candidate, 'きぬ「本文」', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    before = app.capture_view()
    import pytest
    with pytest.raises(OSError):
        app.commit_capture()
    after = app.capture_view()
    assert after == before
    assert after.dirty
    assert app.current_profile().hooks == []
    assert app.current_profile().inserted_hooks == []


def test_import_catalog_rolls_back_application_source_when_persistence_fails():
    catalog = Catalog()
    game = catalog.create_game('Game')
    app = AutoAdvApplication(
        catalog, lambda: (_ for _ in ()).throw(OSError('disk full')),
        input_send=lambda *_: None, target_ready=lambda *_: True, g2p=TEST_G2P)
    app.select_profile(game.id, game.default_profile)
    replacement = Catalog()
    replacement.create_game('Imported')
    import pytest
    with pytest.raises(OSError):
        app.install_catalog(replacement)
    assert app.catalog is catalog
    assert app.current_profile() is catalog.games[game.id].profiles[game.default_profile]


def test_import_catalog_persists_before_invalidating_selection():
    app, calls = app_with_game()
    replacement = Catalog()
    replacement.create_game('Imported')
    app.install_catalog(replacement)
    assert calls == ['save']
    assert app.catalog is replacement
    assert app.current_profile() is None
    assert app.capture is None


def test_profile_capture_restore_is_connection_scoped_through_target_activation():
    class Child:
        def poll(self): return None
    class Host:
        def __init__(self): self.child = Child(); self.commands = []
        def command(self, action, **kwargs): self.commands.append((action, kwargs))
        def close(self): pass

    app, _ = app_with_game()
    profile = app.current_profile()
    profile.insert_pc_hooks = True
    profile.inserted_hooks = ['H1', 'H2']
    app.select_primary_target(101, 500)
    first = Host(); app.hook_host = first
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host-a', 3, 1, 10, 101))
    actions = [action for action, _ in first.commands]
    assert actions == ['settings', 'connect', 'settings', 'insert_pc_hooks', 'insert_hook', 'insert_hook']

    before = len(first.commands)
    app.handle_hook_event(HookEvent('connected', 'host-a', 3, 2, 20, 101))
    assert len(first.commands) == before

    app.close_hook_host()
    second = Host(); app.hook_host = second
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host-b', 4, 1, 30, 101))
    assert [action for action, _ in second.commands] == [
        'settings', 'connect', 'settings', 'insert_pc_hooks', 'insert_hook', 'insert_hook']

def test_diagnostics_view_is_generated_from_application_state():
    app, _ = app_with_game()
    app.select_primary_target(101, 0x1234)
    view = app.diagnostics_view()
    assert 'Target: hook=101' in view.text
    assert 'Luna connection: disconnected' in view.text
    assert 'Runtime: session=' in view.text


def test_application_owns_text_presentation_deadline_and_next_line_timeout():
    catalog = Catalog()
    game = catalog.create_game('Game')
    sent = []
    app = AutoAdvApplication(
        catalog, lambda: None,
        input_send=lambda target, key: sent.append((target.pid, key)),
        target_ready=lambda _target: True, g2p=TEST_G2P)
    profile = app.select_profile(game.id, game.default_profile)
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    scheduled = []
    changed = []
    events = []
    app.configure_runtime_loop(
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
        on_change=lambda: changed.append(app.runtime_state().pause_reason),
        on_event=lambda name, token, detail: events.append((name, token, detail)))
    connect_hook(app, profile)
    token = runtime_hook(app, text, '本文', 2, received_ns=100)
    assert token is not None
    app.set_auto_advance(True)
    assert scheduled[0][0] == app.runtime_state().utterance.text_wait_ms
    scheduled.pop(0)[1]()
    assert sent == [(101, profile.input_key)]
    assert app.runtime_state().sent
    assert any(name == 'presentation_complete' for name, _, _ in events)
    timeout_delay, timeout = scheduled.pop(0)
    assert timeout_delay == profile.next_line_timeout_ms
    timeout()
    assert app.play_view().issue is not None
    assert app.runtime_state().pause_reason == 'next_line_timeout'
    assert any(name == 'next_line_timeout' for name, _, _ in events)


def test_utterance_is_published_before_presentation_side_effects_begin():
    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    timeline = []
    app.configure_runtime_loop(
        schedule=lambda delay, callback: timeline.append(('schedule', delay, callback)))
    app.configure_telemetry(
        lambda name, token, detail: timeline.append(('event', name, token)))
    connect_hook(app, profile)
    timeline.clear()

    token = runtime_hook(app, text, '本文', 2, received_ns=100)
    assert token is not None
    utterance_at = next(i for i, item in enumerate(timeline)
                        if item[0] == 'event' and item[1] == 'utterance')
    first_presentation_effect = next(i for i, item in enumerate(timeline)
                                     if item[0] == 'schedule')
    assert utterance_at < first_presentation_effect

def test_application_owns_uncertain_audio_fallback_policy():
    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    app.set_profile_setting('advance_mode', AdvanceMode.AUDIO)
    app.set_profile_setting('uncertain_text_fallback', False)
    profile = app.current_profile()
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, profile)
    token = runtime_hook(app, text, '本文', 2)
    assert token is not None
    app.set_auto_advance(True)
    # Managed NativeAudio startup and the uncertainty deadline are independent
    # scheduled transitions. This test owns only the deadline policy.
    deadline = next(callback for delay, callback in scheduled
                    if delay == app.runtime_state().utterance.text_wait_ms)
    class RetainedNative:
        pid = 101
        def capture_status(self):
            return {}
        def finish_line(self, _token):
            raise AssertionError('an inconclusive hold must retain the active audio line')

    app._native_audio = RetainedNative()
    deadline()
    assert app.runtime_state().pause_reason == 'native_acoustic_unconfirmed_hold'
    assert not app.runtime_state().sent

def test_application_auto_advance_can_complete_without_gui_runtime_policy():
    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, profile)
    token = runtime_hook(app, text, '本文', 2)
    assert token is not None
    scheduled.pop(0)[1]()
    assert app.runtime_state().completed
    assert not app.runtime_state().sent
    app.set_auto_advance(True)
    assert app.runtime_state().sent


def test_selected_hook_provenance_is_emitted_before_utterance_and_filters_stale_or_unselected_outputs():
    app, _ = app_with_game()
    profile = app.current_profile()
    selected = source('T', 1)
    unselected = source('X', 7, address=2)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    app.select_profile(app.game_id, app.profile_id)
    scheduled = []
    telemetry = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    app.configure_telemetry(
        lambda name, token, detail: telemetry.append((name, token, dict(detail))))
    connect_hook(app, profile)

    # Candidate output is visible to capture, but it is not selected runtime provenance.
    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, unselected, '候補'))
    # A stale connection generation must not be promoted to selected provenance either.
    app.handle_hook_event(HookEvent('output', 'host', 0, 3, 30, 101, selected, '旧台詞'))
    assert not [item for item in telemetry if item[0] == 'selected_hook_output']

    app.handle_hook_event(HookEvent('output', 'host', 1, 4, 40, 101, selected, '現在の台詞'))
    names = [name for name, _, _ in telemetry]
    assert names.index('selected_hook_output') < names.index('utterance')
    selected_event = next(detail for name, _, detail in telemetry if name == 'selected_hook_output')
    assert selected_event['session'] == app.runtime_state().session
    assert selected_event['source'] == selected.identity
    assert selected_event['hook_sequence'] == 4
    assert selected_event['raw'] == '現在の台詞'


def test_each_selected_output_precedes_grouped_utterance_after_luna_sync():
    app, _ = app_with_game()
    profile = app.current_profile()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    body_hook = HookSelection('T', 1, 2)
    speaker_hook = HookSelection('S', 5, 6)
    profile.hooks = [body_hook, speaker_hook]
    profile.body_source = body_hook.identity
    profile.speaker_source = speaker_hook.identity
    profile.source_rules[body_hook.identity] = [StructureRule(r'^(?P<body>.*)$')]
    profile.source_rules[speaker_hook.identity] = [StructureRule(r'^(?P<speaker>.*)$')]
    app.select_profile(app.game_id, app.profile_id)
    scheduled = []
    telemetry = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    app.configure_telemetry(
        lambda name, token, detail: telemetry.append((name, token, dict(detail))))
    connect_hook(app, profile)

    # The first body output makes the saved profile monitorable.  The speaker
    # thread is then discovered and Luna synchronization is established before
    # any group used as dialogue evidence is collected.
    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, body, 'warmup'))
    app.handle_hook_event(HookEvent('created', 'host', 1, 3, 30, 101, speaker, ''))
    telemetry.clear()
    scheduled.clear()

    app.handle_hook_event(HookEvent('output', 'host', 1, 4, 40, 101, body, '本文'))
    app.handle_hook_event(HookEvent('output', 'host', 1, 5, 50, 101, speaker, '大和'))
    assert len(scheduled) == 1
    assert 'utterance' not in [name for name, _, _ in telemetry]
    scheduled.pop()[1]()

    names = [name for name, _, _ in telemetry]
    selected_positions = [i for i, name in enumerate(names) if name == 'selected_hook_output']
    utterance_position = names.index('utterance')
    assert len(selected_positions) == 2
    assert all(position < utterance_position for position in selected_positions)
    assert [detail['source'] for name, _, detail in telemetry if name == 'selected_hook_output'] == [
        body.identity, speaker.identity]
    assert app.runtime_state().utterance.text == '本文'
    assert app.runtime_state().utterance.speaker == '大和'

def test_activate_selected_target_reconciles_saved_monitor_from_current_state():
    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    app.select_profile(app.game_id, app.profile_id)
    app.select_primary_target(101, 500)
    app.hook_host = _Host()

    assert app.activate_selected_target() == 'connecting'
    assert [action for action, _ in app.hook_host.commands] == ['settings', 'connect']
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host', 1, 1, 10, process_id=101))
    assert app.activate_selected_target() == 'connected'
    assert app.runtime_state().profile is None

    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, text, '最初の台詞'))
    assert app.runtime_state().profile is not None
    assert app.runtime_state().profile.body_source == text.identity
    assert app.runtime_state().utterance.raw == '最初の台詞'

def test_reusing_connected_target_after_profile_switch_reapplies_profile_capture_settings():
    class Child:
        def poll(self):
            return None

    class Host:
        def __init__(self):
            self.child = Child()
            self.commands = []

        def command(self, action, **kwargs):
            self.commands.append((action, kwargs))

    app, _ = app_with_game()
    game_id = app.game_id
    first = app.current_profile()
    first.hook_flush_ms = 111
    first.inserted_hooks = ['H1']
    second = app.add_profile('Alt')
    second.hook_flush_ms = 222
    second.hook_codepage = 65001
    second.inserted_hooks = ['H2']

    app.select_primary_target(101, 500)
    app.hook_host = Host()
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host-a', 1, 1, 10, process_id=101))

    app.hook_host.commands.clear()
    app.select_profile(game_id, second.id)
    assert app.runtime_target_state().selected
    assert app.activate_selected_target() == 'connected'

    settings = [kwargs for action, kwargs in app.hook_host.commands if action == 'settings']
    assert settings == [{
        'flush_ms': 222,
        'codepage': 65001,
        'max_buffer': second.hook_max_buffer,
        'max_history': second.hook_max_history,
    }]
    assert ('insert_hook', {'pid': 101, 'code': 'H2'}) in app.hook_host.commands


def test_single_hook_event_starts_saved_monitor_and_enters_runtime_without_group_delay():
    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    app.select_profile(app.game_id, app.profile_id)
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    connect_hook(app, app.current_profile())

    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, text, '最初の台詞'))
    assert app.runtime_state().profile is not None
    assert app.runtime_state().token is not None
    assert app.runtime_state().utterance.raw == '最初の台詞'
    assert not any(delay == 100 for delay, _ in scheduled)

def test_selected_body_source_removal_does_not_fall_back_to_an_unselected_provider():
    app, _ = app_with_game()
    profile = app.current_profile()
    primary = source('A', 1, address=1)
    unselected = source('B', 3, address=2)
    profile.hooks = [HookSelection('A', 1, 2), HookSelection('B', 3, 4)]
    profile.body_source = primary.identity
    app.select_profile(app.game_id, app.profile_id)
    profile = app.current_profile()
    connect_hook(app, profile)

    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, primary, 'A'))
    assert app.runtime_state().profile is not None
    app.handle_hook_event(HookEvent('output', 'host', 1, 3, 25, 101, unselected, 'B'))

    app.handle_hook_event(HookEvent('removed', 'host', 1, 4, 30, 101, primary))
    assert app.runtime_state().profile is None
    assert app.capture_view().problem is not None

    app.handle_hook_event(HookEvent('output', 'host', 1, 5, 40, 101, primary, 'A2'))
    assert app.runtime_state().profile is not None
    assert app.runtime_state().profile.body_source == primary.identity

def test_unselected_candidate_ambiguity_does_not_interrupt_active_monitoring():
    app, _ = app_with_game()
    profile = app.current_profile()
    primary = source('A', 1, address=1)
    profile.hooks = [HookSelection('A', 1, 2)]
    profile.body_source = primary.identity
    app.select_profile(app.game_id, app.profile_id)
    connect_hook(app, app.current_profile())
    runtime_hook(app, primary, 'active', 2, received_ns=20)
    session = app.runtime_state().session

    unused_a = source('B', 3, address=10)
    unused_b = source('B', 3, address=11)
    app.handle_hook_event(HookEvent('created', 'host', 1, 3, 30, 101, unused_a))
    app.handle_hook_event(HookEvent('created', 'host', 1, 4, 40, 101, unused_b))

    assert app.capture.identity_is_ambiguous(unused_a.identity)
    assert app.runtime_state().session == session
    assert app.runtime_state().utterance.raw == 'active'


def test_active_speaker_source_removal_invalidates_runtime_like_body_source():
    app, _ = app_with_game()
    profile = app.current_profile()
    body = source('B', 1, address=1)
    speaker = source('S', 5, address=2)
    profile.hooks = [HookSelection('B', 1, 2), HookSelection('S', 5, 6)]
    profile.body_source = body.identity
    profile.speaker_source = speaker.identity
    app.select_profile(app.game_id, app.profile_id)
    connect_hook(app, app.current_profile())
    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, body, '本文'))
    app.handle_hook_event(HookEvent('output', 'host', 1, 3, 25, 101, speaker, '話者'))
    assert app.runtime_state().profile is not None

    app.handle_hook_event(HookEvent('removed', 'host', 1, 4, 30, 101, speaker))

    assert app.runtime_state().profile is None
    assert app.capture_view().problem is not None


def test_monitor_lifecycle_is_observable_from_application_events_without_gui_policy():
    class Child:
        def poll(self):
            return None
    class Host:
        def __init__(self):
            self.child = Child()
            self.commands = []
        def command(self, action, **kwargs):
            self.commands.append((action, kwargs))

    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_source = profile.hooks[0].identity
    app.select_profile(app.game_id, app.profile_id)
    profile = app.current_profile()
    app.select_primary_target(101, 500)
    app.hook_host = Host()
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_event(HookEvent('connected', 'host', 1, 1, 10, 101))
    events = []
    app.configure_runtime_loop(
        schedule=lambda *_: None,
        on_event=lambda name, token, detail: events.append((name, token, detail)))
    app.handle_hook_event(HookEvent('output', 'host', 1, 2, 20, 101, text, '本文'))
    state = app.runtime_state()
    assert any(name == 'monitor_started' and detail['session'] == state.session
               for name, _, detail in events)
    app.stop_monitoring('session_changed')
    assert any(name == 'monitor_ended' and detail['session'] == state.session
               for name, _, detail in events)


def test_speaker_commands_preserve_only_explicit_overrides_when_renaming():
    app, calls = app_with_game()
    profile = app.current_profile()
    profile.speaker_voices['京'] = 'v1'
    assert '京' not in profile.speaker_tts

    app.rename_speaker('京', 'みやこ')

    updated = app.current_profile()
    assert updated.speaker_voices == {'みやこ': 'v1'}
    assert 'みやこ' not in updated.speaker_tts
    assert calls == ['save']


def test_user_level_edit_commands_persist_without_gui_profile_mutators():
    app, calls = app_with_game()
    app.set_capture_environment(
        ocr_executable='tesseract.exe', ocr_language='jpn', ocr_region=(1, 2, 3, 4),
        ocr_stable_samples=3, insert_pc_hooks=True, hook_flush_ms=250,
        hook_codepage=65001, hook_max_buffer=2048, hook_max_history=64)
    app.add_speaker('京', True, 'v1')
    app.set_speaker_voice('京', 'v2')
    app.add_reading('京', 'みやこ')
    app.save_replacement_rule(None, ReplacementRule('旧', '新', False, 'tts', True))

    profile = app.current_profile()
    assert profile.ocr.executable == 'tesseract.exe'
    assert profile.ocr.region == (1, 2, 3, 4)
    assert profile.hook_flush_ms == 250
    assert profile.hook_codepage == 65001
    assert profile.speaker_tts['京'] is True
    assert profile.speaker_voices['京'] == 'v2'
    assert profile.name_readings['京'] == 'みやこ'
    assert profile.replacements[-1].replacement == '新'
    assert len(calls) == 5


def test_capture_source_rule_commands_remain_inside_capture_transaction():
    app, calls = app_with_game()
    before = dict(app.current_profile().source_rules)
    rule = StructureRule(r'^(?P<speaker>.+?)「(?P<body>.+)」$')
    candidate = source('T', 1)
    app.capture.observe(candidate, '大和「台詞」', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    app.save_capture_source_rule(candidate.identity, candidate.address, None, rule)
    assert app.current_profile().source_rules == before
    assert app.capture_source_rules(candidate.identity)[0] == rule
    assert calls == []
    app.commit_capture()
    assert app.current_profile().source_rules[candidate.identity][0] == rule
    assert calls == ['save']


def test_capture_history_preserves_original_spaces_width_and_source_newline():
    app, _ = app_with_game()
    hook = source('RAW', 9)
    raw = '  ＡＢＣ１２３　ﾊﾟﾋﾟﾌﾟ  \n'
    app.capture.observe(hook, raw, sequence=1)
    sample = app.capture_view().candidates[0]
    assert sample.history == (raw,)
    assert sample.samples[0].raw == raw
    assert sample.samples[0].body == raw
