from autoadv.application import AutoAdvApplication
from autoadv.conversation import Target, Token
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.model import AdvanceMode, HookSelection, ReplacementRule, StructureRule
from autoadv.storage import Catalog


def source(code, context, address=1):
    return ThreadSource(101, address, context, context + 1, code, code)




def runtime_hook(app, source_, text, sequence, *, received_ns=1, generation='host', connection=1, pid=101):
    return app.handle_runtime_hook_output(
        HookEvent('output', generation, connection, sequence, received_ns, pid, source_, text))[0]


def capture_group(app, events):
    for event in events:
        app.queue_capture_output(event)
    app.flush_capture_outputs()

def app_with_game():
    catalog = Catalog()
    game = catalog.create_game('Game')
    calls = []
    app = AutoAdvApplication(
        catalog, lambda: calls.append('save'),
        input_send=lambda *_: None, target_ready=lambda *_: True)
    app.select_profile(game.id, game.default_profile)
    return app, calls


def test_capture_workspace_observes_real_output_without_manual_refresh():
    app, _ = app_with_game()
    a = source('A', 1)
    app.observe_capture(a, '大和「一つ目」', sequence=1)
    app.observe_capture(a, '大和「二つ目」', sequence=2)
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
    app.observe_capture(combined, raw, sequence=1)
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


def test_different_sources_can_supply_only_the_correct_field_from_each_mixed_output():
    app, _ = app_with_game()
    speaker = source('S', 1)
    body = source('B', 5, address=2)
    speaker_raw = 'きぬ　（０４０６）■■壊れた本文■■'
    body_raw = '???「あれカレー屋を荒らしてくれた女だ」'
    app.observe_capture(speaker, speaker_raw, sequence=1)
    app.observe_capture(body, body_raw, sequence=2)
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
    assert updated.body_sources == [body.identity]
    assert updated.speaker_sources == [speaker.identity]
    assert {hook.identity for hook in updated.hooks} == {body.identity, speaker.identity}


def test_capture_recipe_does_not_persist_until_committed():
    app, calls = app_with_game()
    candidate = source('T', 1)
    app.observe_capture(candidate, '大和「台詞」', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    assert app.current_profile().hooks == []
    assert app.capture_view().dirty
    assert calls == []
    app.commit_capture()
    current = app.current_profile()
    assert [h.code for h in current.hooks] == ['T']
    assert current.body_sources == [candidate.identity]
    assert calls == ['save']


def test_ambiguous_source_cannot_be_committed():
    app, _ = app_with_game()
    candidate = source('T', 1)
    app.observe_capture(candidate, '台詞', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    app.observe_capture(source('T', 1, address=2), '別出力', sequence=2)
    view = app.capture_view()
    assert not view.can_commit
    assert view.problem is not None


def test_source_identity_collision_and_recovery_are_owned_by_capture_workspace():
    app, _ = app_with_game()
    first = source('T', 1, address=1)
    second = source('T', 1, address=2)
    app.observe_capture(first, '台詞1', sequence=1)
    assert not app.capture_identity_is_ambiguous(first.identity)
    app.observe_capture(second, '台詞2', sequence=2)
    assert app.capture_identity_is_ambiguous(first.identity)
    assert app.capture_source(first.identity, first.address) is None
    assert app.capture_source(first.identity, first.address, require_unique=False) == first
    app.remove_capture_source(second.identity, second.address)
    assert not app.capture_identity_is_ambiguous(first.identity)
    assert app.capture_source(first.identity, first.address) == first


def test_recovery_trial_becomes_persistent_only_when_used_by_final_fields():
    app, _ = app_with_game()
    candidate = source('RECOVERY', 7)
    assert app.begin_recovery_hook_trial(candidate.hook_code)
    app.mark_recovery_hook_inserted(candidate.hook_code)
    app.observe_capture(candidate, '本文', sequence=1)
    assert app.current_profile().inserted_hooks == []
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    assert app.capture.profile.inserted_hooks == [candidate.hook_code]
    app.commit_capture()
    assert app.current_profile().inserted_hooks == [candidate.hook_code]


def test_recovery_trial_is_not_persisted_after_switching_to_another_final_source():
    app, _ = app_with_game()
    trial = source('RECOVERY', 7)
    normal = source('NORMAL', 8, address=2)
    assert app.begin_recovery_hook_trial(trial.hook_code)
    app.mark_recovery_hook_inserted(trial.hook_code)
    app.observe_capture(trial, '試用本文', sequence=1)
    app.observe_capture(normal, '採用本文', sequence=2)
    app.set_capture_field_source('body', trial.identity, trial.address)
    assert app.capture.profile.inserted_hooks == [trial.hook_code]
    app.set_capture_field_source('body', normal.identity, normal.address)
    app.commit_capture()
    profile = app.current_profile()
    assert profile.body_sources == [normal.identity]
    assert [hook.code for hook in profile.hooks] == [normal.hook_code]
    assert profile.inserted_hooks == []


def test_speaker_from_later_flush_is_never_reused_for_older_body():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    app.observe_capture(body, '一つ目', sequence=1)
    app.observe_capture(speaker, '大和', sequence=2)
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


def test_capture_batch_buffer_is_application_owned_and_preserves_same_flush_field_assembly():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    app.observe_capture(body, 'おはよう', sequence=1)
    app.observe_capture(speaker, '大和', sequence=2)
    app.set_capture_extraction_preset(body.identity, body.address, 'body')
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    app.set_capture_field_source('body', body.identity, body.address)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)
    assert app.queue_capture_output(HookEvent('output', 'host', 1, 1, 100, 101, body, 'おはよう')) == 100
    assert app.queue_capture_output(HookEvent('output', 'host', 1, 2, 110, 101, speaker, '大和')) == 100
    assert app.capture_view().assembly == ()
    app.flush_capture_outputs()
    assert [(p.speaker, p.body) for p in app.capture_view().assembly] == [('大和', 'おはよう')]


def test_runtime_assembles_body_and_speaker_from_independent_sources():
    app, _ = app_with_game()
    body = source('T', 1)
    speaker = source('S', 5, address=2)
    profile = app.current_profile()
    body_hook = HookSelection('T', 1, 2)
    speaker_hook = HookSelection('S', 5, 6)
    profile.hooks = [body_hook, speaker_hook]
    profile.body_sources = [body_hook.identity]
    profile.speaker_sources = [speaker_hook.identity]
    profile.source_rules[body_hook.identity] = [StructureRule(r'^(?P<body>.*)$')]
    profile.source_rules[speaker_hook.identity] = [StructureRule(r'^(?P<speaker>.*)$')]
    app.begin_runtime(profile, Target(101, 500, hook_pid=101), 'host', 1,
                      available_sources=(body.identity, speaker.identity))
    app.handle_runtime_hook_output(HookEvent('output', 'host', 1, 1, 100, 101, body, 'おはよう'))
    app.handle_runtime_hook_output(HookEvent('output', 'host', 1, 2, 110, 101, speaker, '大和'))
    token = app.flush_runtime_hook_outputs()
    assert token is not None
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
    app.observe_capture(candidate, '大和：台詞', sequence=1)
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
    app.report_capture_problem('台詞取得に失敗しました')
    view = app.capture_view()
    assert view.problem is not None
    assert view.problem.message == '台詞取得に失敗しました'
    assert view.problem.action == 'open_capture_recovery'
    app.clear_capture_problem()
    assert app.capture_view().problem is None


def test_play_view_hides_normal_internal_state_and_surfaces_only_actionable_hold():
    app, _ = app_with_game()
    app.controller.authorized = True
    app.controller.pause_reason = None
    view = app.play_view()
    assert view.advance_authorized
    assert view.issue is None
    app.controller.pause_reason = 'native_inconclusive_hold'
    view = app.play_view()
    assert view.issue is not None
    assert '原音の終了' in view.issue.message
    assert view.issue.action == 'open_audio_policy'
    app.controller.pause_reason = 'ocr_failure'
    view = app.play_view()
    assert view.issue is not None
    assert view.issue.action == 'open_capture'
    app.controller.pause_reason = 'presenting'
    assert app.play_view().issue is None


def test_play_view_keeps_tts_failure_actionable_even_when_auto_is_off():
    app, _ = app_with_game()
    app.controller.authorized = False
    app.controller.pause_reason = 'tts_playback_failed'
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
    events = []
    closed = []
    errors = []

    app.configure_hook_service(on_event=events.append, on_closed=closed.append, on_error=errors.append)
    app.start_hook_host()
    first = FakeHost.instances[-1]
    assert app.hook_host_running
    app.hook_command('connect', pid=101)
    assert first.commands == [('connect', {'pid': 101})]
    first.on_event('first')
    assert events == ['first']

    app.start_hook_host()
    second = FakeHost.instances[-1]
    assert first.closed
    first.on_event('stale')
    first.on_error('stale error')
    second.on_event('second')
    assert events == ['first', 'second']
    assert errors == []

    app.close_hook_host()
    assert second.closed
    assert not app.hook_host_running


def test_replacing_catalog_invalidates_old_profile_and_capture_state():
    app, _ = app_with_game()
    old_profile = app.current_profile()
    assert old_profile is not None
    replacement = Catalog()
    game = replacement.create_game('Imported')
    app.replace_catalog(replacement)
    assert app.catalog is replacement
    assert app.current_profile() is None
    assert app.capture is None
    selected = app.select_profile(game.id, game.default_profile)
    assert selected is replacement.games[game.id].profiles[game.default_profile]


def test_application_owns_native_audio_lifecycle_and_runtime_evidence(monkeypatch):
    import autoadv.application as application_module

    class ImmediateThread:
        def __init__(self, *, target, daemon, name):
            self.target = target
        def start(self):
            self.target()

    class FakeNative:
        instances = []
        def __init__(self, pid, helper, model, tokens, notify, **callbacks):
            self.pid = pid
            self.notify = notify
            self.callbacks = callbacks
            self.started = False
            self.closed = False
            self.begun = []
            self.boundaries = 0
            self.cancelled = 0
            FakeNative.instances.append(self)
        def start(self):
            self.started = True
        def begin(self, token, match_text, *, replay_from_ms=None):
            self.begun.append((token, match_text, replay_from_ms))
        def boundary(self):
            self.boundaries += 1
        def cancel(self):
            self.cancelled += 1
        def holds_deadline(self, token):
            return True
        def capture_status(self):
            return {'packets': 3}
        def close(self):
            self.closed = True

    monkeypatch.setattr(application_module.threading, 'Thread', ImmediateThread)
    monkeypatch.setattr(application_module, 'NativeAudio', FakeNative)
    app, _ = app_with_game()
    profile = app.current_profile()
    profile.source = 'ocr'
    profile.advance_mode = AdvanceMode.AUDIO
    profile.native_helper = 'helper.dll'
    profile.asr_model = 'model.onnx'
    profile.asr_tokens = 'tokens.txt'
    app.begin_runtime(profile, Target(101, 500, audio_pid=202), 'ocr', 1)
    ready = []
    assert app.start_native_audio(
        profile, on_ready=lambda *args: ready.append(args),
        on_event=lambda *_: None)
    native = FakeNative.instances[-1]
    assert native.started
    assert app.native_audio_available
    assert app.native_audio_pid == 202
    assert ready == [(app.runtime_state().session, True, '')]

    token = app.handle_runtime_ocr_output(
        sequence=1, text='「声」', pid=101, session=app.runtime_state().session)
    assert token is not None
    assert app.begin_native_audio(token, app.runtime_state().utterance.match_text, replay_from_ms=12)
    assert native.begun[-1] == (token, app.runtime_state().utterance.match_text, 12)
    assert app.native_audio_holds_deadline(token)
    assert app.confirm_external_advance(token)
    app.cancel_native_audio()
    assert native.boundaries == 1
    assert native.cancelled == 1
    assert app.native_audio_capture_status() == {'packets': 3}
    assert app.handle_native_event(token, 'completed')
    assert app.runtime_state().presentation_reason == 'native'

    app.close_native_audio()
    assert native.closed
    assert not app.native_audio_available


def test_application_owns_ocr_worker_and_turns_failure_into_runtime_hold(monkeypatch):
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
    app.begin_runtime(profile, Target(101, 500), 'ocr', 1)
    lines, errors = [], []
    assert app.start_ocr(profile, on_text=lambda *args: lines.append(args), on_error=errors.append)
    worker = FakeWorker.instances[-1]
    assert worker.started
    worker.on_text(3, '台詞')
    assert lines == [(app.runtime_state().session, 101, 3, '台詞')]
    worker.on_error('capture failed')
    assert errors == ['capture failed']
    assert app.runtime_state().pause_reason == 'ocr_failure'
    app.close_ocr()
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
    results, previews = [], []
    app.configure_voice_jobs(
        on_result=lambda *args: results.append(args),
        on_preview=lambda *args: previews.append(args))
    voice_jobs, preview_jobs = FakeJobs.instances
    token = Token(1, 2)
    app.start_voice(token, '読み', 'voice-1')
    app.start_voice_preview('試聴', 'voice-2')
    assert voice_jobs.starts == [(token, app.catalog.irodori_url, '読み', 'voice-1')]
    assert preview_jobs.starts[0][1:] == (app.catalog.irodori_url, '試聴', 'voice-2')
    voice_jobs.report(token, True, '')
    preview_jobs.report(preview_jobs.starts[0][0], False, 'cancelled')
    assert results == [(token, True, '')]
    assert previews == [(False, 'cancelled')]
    app.cancel_voice()
    app.cancel_voice_preview()
    assert voice_jobs.cancelled == 1
    assert preview_jobs.cancelled == 1



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

def test_application_owns_selected_thread_sync_state():
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
    host = Host()
    app.hook_host = host
    text = source('T', 1)
    speaker = source('S', 5, address=2)
    app.observe_capture(text, '本文', sequence=1)
    app.observe_capture(speaker, '大和', sequence=2)
    app.set_capture_extraction_preset(text.identity, text.address, 'body')
    app.set_capture_extraction_preset(speaker.identity, speaker.address, 'speaker')
    app.set_capture_field_source('body', text.identity, text.address)
    app.set_capture_field_source('speaker', speaker.identity, speaker.address)

    assert app.sync_hook_threads(101) == ()
    enables = [kwargs['source']['hook_code'] for action, kwargs in host.commands
               if action == 'sync_thread' and kwargs['enabled']]
    assert enables == ['T', 'S']

    host.commands.clear()
    app.set_capture_field_source('speaker', None)
    assert app.sync_hook_threads(101) == ()
    disables = [kwargs['source']['hook_code'] for action, kwargs in host.commands
                if action == 'sync_thread' and not kwargs['enabled']]
    assert set(disables) == {'T', 'S'}


def test_application_owns_hook_connection_identity_and_rejects_stale_events():
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
    host = Host()
    app.hook_host = host
    profile = app.current_profile()
    assert app.begin_hook_connection(101, profile)
    assert not app.begin_hook_connection(101, profile)
    assert [action for action, _ in host.commands] == ['settings', 'connect']

    stale = HookEvent('connected', 'old-host', 1, 1, 10, 999)
    assert app.accept_hook_connection_event(stale) is None
    assert not app.begin_hook_connection(101, profile)

    connected = HookEvent('connected', 'host-a', 7, 2, 20, 101)
    assert app.accept_hook_connection_event(connected) == 'changed'
    assert not app.begin_hook_connection(101, profile)
    current_output = HookEvent('output', 'host-a', 7, 3, 30, 101, source('T', 1), '本文')
    stale_output = HookEvent('output', 'host-a', 6, 4, 40, 101, source('T', 1), '旧本文')
    assert app.capture_event_matches_connection(current_output)
    assert not app.capture_event_matches_connection(stale_output)

    disconnected = HookEvent('disconnected', 'host-a', 7, 5, 50, 101)
    assert app.accept_hook_connection_event(disconnected) == 'disconnected'
    assert not app.capture_event_matches_connection(current_output)


def test_hook_host_restart_invalidates_connection_identity():
    class Child:
        def poll(self):
            return None
    class Host:
        def __init__(self):
            self.child = Child()
        def command(self, *_args, **_kwargs):
            pass
        def close(self):
            pass

    app, _ = app_with_game()
    app.hook_host = Host()
    app.begin_hook_connection(101, app.current_profile())
    app.accept_hook_connection_event(HookEvent('connected', 'host-a', 3, 1, 10, 101))
    output = HookEvent('output', 'host-a', 3, 2, 20, 101, source('T', 1), '本文')
    assert app.capture_event_matches_connection(output)
    app.close_hook_host()
    assert not app.capture_event_matches_connection(output)


def test_changing_body_provider_recomputes_final_preview_without_changing_source_formatting():
    app, _ = app_with_game()
    first = source('A', 1, address=1)
    second = source('B', 3, address=2)
    app.observe_capture(first, 'Aの台詞', sequence=1)
    app.observe_capture(second, 'Bの台詞', sequence=2)
    app.set_capture_extraction_preset(first.identity, first.address, 'body')
    app.set_capture_extraction_preset(second.identity, second.address, 'body')
    app.set_capture_field_source('body', first.identity, first.address)
    assert [item.body for item in app.capture_view().assembly] == ['Aの台詞']
    app.set_capture_field_source('body', second.identity, second.address)
    assert [item.body for item in app.capture_view().assembly] == ['Bの台詞']
    assert app.capture_candidate(first.identity, first.address).preset == 'body'


def test_saved_provider_readiness_requires_real_body_output_and_allows_fallback():
    app, _ = app_with_game()
    primary = source('A', 1, address=1)
    fallback = source('B', 3, address=2)
    profile = app.current_profile()
    profile.hooks = [HookSelection('A', 1, 2), HookSelection('B', 3, 4)]
    profile.body_sources = [hook.identity for hook in profile.hooks]
    app.select_profile(app.game_id, app.profile_id)
    app.observe_capture(primary, '', sequence=1)  # thread exists, but produced no dialogue
    assert not app.capture_saved_sources_ready(profile)
    app.observe_capture(fallback, 'fallback line', sequence=2)
    assert app.capture_saved_sources_ready(profile)
    assert app.capture_output_identities() == (fallback.identity,)
    app.begin_runtime(profile, Target(101, 500, hook_pid=101), 'host', 1,
                      available_sources=app.capture_output_identities())
    assert app.runtime_state().active_body_source == fallback.identity


def test_failed_recovery_hook_trial_can_be_retried():
    app, _ = app_with_game()
    assert app.begin_recovery_hook_trial('HCODE')
    app.recovery_hook_trial_failed('HCODE')
    assert app.begin_recovery_hook_trial('HCODE')


def test_runtime_target_identity_is_application_session_state():
    app, _ = app_with_game()
    state = app.select_primary_target(101, 0x1234)
    assert state.hook_pid == state.audio_pid == state.input_pid == 101
    assert state.hwnd == 0x1234
    app.select_audio_target(202)
    app.select_input_target(303, 0x5678)
    target = app.session_target()
    assert (target.hook_pid, target.audio_pid, target.pid, target.hwnd) == (101, 202, 303, 0x5678)
    app.clear_runtime_target()
    assert not app.runtime_target_state().selected


def test_profile_selection_discards_capture_batches_and_pending_runtime_transitions():
    app, _ = app_with_game()
    text = source('T', 1)
    event = HookEvent('output', 'host', 1, 1, 100, 101, text, 'old')
    assert app.queue_capture_output(event) == 100
    app.defer_capture_transition()
    app.request_saved_monitor_resume(app.current_profile())
    game_id, profile_id = app.game_id, app.profile_id
    app.select_profile(game_id, profile_id)
    assert not app.consume_capture_transition()
    assert app.capture_view().assembly == ()
    app.flush_capture_outputs()
    assert app.capture_view().assembly == ()


def test_capture_transition_boundary_is_application_owned():
    app, _ = app_with_game()
    text = source('T', 1)
    profile = app.current_profile()
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_sources = [profile.hooks[0].identity]
    app.begin_runtime(profile, Target(101, 500, hook_pid=101), 'host', 1,
                      available_sources=(text.identity,))
    first = runtime_hook(app, text, 'first', 1, received_ns=100)
    assert first is not None
    assert not app.defer_capture_transition()
    duplicate = HookEvent('output', 'host', 1, 2, 200, 101, text, 'first')
    changed = HookEvent('output', 'host', 1, 3, 300, 101, text, 'second')
    assert not app.capture_transition_boundary(duplicate, profile)
    assert app.capture_transition_boundary(changed, profile)
    assert app.consume_capture_transition()
    assert not app.consume_capture_transition()


def test_runtime_target_alternates_reset_to_primary_without_losing_primary_window():
    app, _ = app_with_game()
    app.select_primary_target(101, 0x1234)
    app.select_audio_target(202)
    app.select_input_target(303, 0x5678)
    state = app.select_audio_target(None)
    assert state.audio_pid == 101
    state = app.select_input_target(None, None)
    assert (state.input_pid, state.hwnd, state.hook_hwnd) == (101, 0x1234, 0x1234)
    target = app.session_target()
    assert (target.hook_pid, target.audio_pid, target.pid, target.hwnd) == (101, 101, 101, 0x1234)


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
        input_send=lambda *_: None, target_ready=lambda *_: True)
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
        input_send=lambda *_: None, target_ready=lambda *_: True)
    app.select_profile(game.id, game.default_profile)
    candidate = source('RECOVERY', 7)
    assert app.begin_recovery_hook_trial(candidate.hook_code)
    app.mark_recovery_hook_inserted(candidate.hook_code)
    app.observe_capture(candidate, 'きぬ「本文」', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    before = app.capture_profile_snapshot()
    import pytest
    with pytest.raises(OSError):
        app.commit_capture()
    after = app.capture_profile_snapshot()
    assert after == before
    assert app.capture_view().dirty
    assert app.current_profile().hooks == []
    assert app.current_profile().inserted_hooks == []


def test_import_catalog_rolls_back_application_source_when_persistence_fails():
    catalog = Catalog()
    game = catalog.create_game('Game')
    app = AutoAdvApplication(
        catalog, lambda: (_ for _ in ()).throw(OSError('disk full')),
        input_send=lambda *_: None, target_ready=lambda *_: True)
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


def test_profile_capture_restore_is_idempotent_and_connection_scoped():
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
    host = Host()
    app.hook_host = host
    profile = app.current_profile()
    profile.insert_pc_hooks = True
    profile.inserted_hooks = ['H1', 'H2']
    app.begin_hook_connection(101, profile)
    app.accept_hook_connection_event(HookEvent('connected', 'host-a', 3, 1, 10, 101))

    host.commands.clear()
    app.restore_profile_capture(profile)
    assert [action for action, _ in host.commands] == ['settings', 'insert_pc_hooks', 'insert_hook', 'insert_hook']
    host.commands.clear()
    app.restore_profile_capture(profile)
    assert [action for action, _ in host.commands] == ['settings']

    app.reset_hook_connection()
    app.hook_host = host
    app.begin_hook_connection(101, profile)
    app.accept_hook_connection_event(HookEvent('connected', 'host-b', 4, 2, 20, 101))
    host.commands.clear()
    app.restore_profile_capture(profile)
    assert [action for action, _ in host.commands] == ['settings', 'insert_pc_hooks', 'insert_hook', 'insert_hook']


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
        target_ready=lambda _target: True)
    profile = app.select_profile(game.id, game.default_profile)
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_sources = [profile.hooks[0].identity]
    profile.advance_mode = AdvanceMode.TEXT
    scheduled = []
    changed = []
    events = []
    app.configure_runtime_loop(
        schedule=lambda delay, callback: scheduled.append((delay, callback)),
        on_change=lambda: changed.append(app.runtime_state().pause_reason),
        on_event=lambda name, token, detail: events.append((name, token, detail)))
    app.begin_runtime(profile, Target(101, 500, hook_pid=101), 'host', 1,
                      available_sources=(text.identity,))
    token = runtime_hook(app, text, '本文', 1, received_ns=100)
    app.set_auto_advance(True)
    app.start_presentation(token)
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


def test_application_owns_uncertain_audio_fallback_policy():
    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_sources = [profile.hooks[0].identity]
    profile.advance_mode = AdvanceMode.AUDIO
    profile.uncertain_text_fallback = False
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    app.begin_runtime(profile, Target(101, 500, hook_pid=101), 'host', 1,
                      available_sources=(text.identity,))
    token = runtime_hook(app, text, '本文', 1)
    app.set_auto_advance(True)
    app.start_presentation(token)
    assert len(scheduled) == 1
    scheduled.pop(0)[1]()
    assert app.runtime_state().pause_reason == 'native_inconclusive_hold'
    assert not app.runtime_state().sent


def test_application_auto_advance_can_complete_without_gui_runtime_policy():
    app, _ = app_with_game()
    profile = app.current_profile()
    text = source('T', 1)
    profile.hooks = [HookSelection('T', 1, 2)]
    profile.body_sources = [profile.hooks[0].identity]
    scheduled = []
    app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
    app.begin_runtime(profile, Target(101, 500, hook_pid=101), 'host', 1,
                      available_sources=(text.identity,))
    token = runtime_hook(app, text, '本文', 1)
    app.start_presentation(token)
    scheduled.pop(0)[1]()
    assert app.runtime_state().completed
    assert not app.runtime_state().sent
    app.set_auto_advance(True)
    assert app.runtime_state().sent


def test_application_rebuilds_pending_hook_capture_before_admitting_first_new_line():
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
    profile.body_sources = [profile.hooks[0].identity]
    app.select_primary_target(101, 500)
    app.hook_host = Host()
    app.begin_hook_connection(101, profile)
    app.accept_hook_connection_event(HookEvent('connected', 'host', 1, 1, 10, 101))
    app.observe_capture(text, '一つ目', sequence=1)
    first_state = app.start_monitoring()
    first = app.handle_runtime_hook_output(
        HookEvent('output', 'host', 1, 2, 20, 101, text, '一つ目'))[0]
    assert first is not None
    first_session = first_state.session

    # A capture-affecting setting edit keeps the current utterance frozen until
    # the next distinct Hook output, then that same output must enter the new runtime.
    app.defer_capture_transition()
    second, delay = app.handle_runtime_hook_output(
        HookEvent('output', 'host', 1, 3, 30, 101, text, '二つ目'))
    assert delay is None
    assert second is not None
    assert second.session > first_session
    assert app.runtime_state().utterance.raw == '二つ目'
    assert not app.consume_capture_transition()


def test_application_owns_ocr_capture_transition_boundary():
    app, _ = app_with_game()
    profile = app.current_profile()
    profile.source = 'ocr'
    app.begin_runtime(profile, Target(101, 500), 'ocr', 1)
    app.defer_capture_transition()
    calls = []
    app.apply_capture_transition = lambda: calls.append('apply') or True

    result = app.handle_runtime_ocr_output(
        session=app.runtime_state().session, pid=101, sequence=1, text='新しい台詞')
    assert result is None
    assert calls == ['apply']


def test_activate_selected_target_owns_connection_request_and_saved_monitor_resume():
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
    profile.body_sources = [profile.hooks[0].identity]
    app.select_profile(app.game_id, app.profile_id)
    profile = app.current_profile()
    app.select_primary_target(101, 500)
    app.hook_host = Host()

    assert app.activate_selected_target() == 'connecting'
    assert [action for action, _ in app.hook_host.commands] == ['settings', 'connect']
    assert app.activate_selected_target() == 'connecting'

    transition = app.handle_hook_connection_event(
        HookEvent('connected', 'host', 1, 1, 10, process_id=101))
    assert transition == 'changed'
    assert app.activate_selected_target() == 'connected'
    assert app.runtime_state().profile is None

    app.observe_capture(text, '最初の台詞', sequence=2)
    state = app.try_resume_saved_monitor(profile)
    assert state is not None
    assert state.profile is not None
    assert state.active_body_source == text.identity



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
    app.handle_hook_connection_event(
        HookEvent('connected', 'host-a', 1, 1, 10, process_id=101))

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


def test_hook_error_is_translated_to_capture_problem_by_application():
    app, _ = app_with_game()
    events = []
    app.configure_runtime_loop(schedule=lambda *_: None, on_event=lambda name, token, detail: events.append((name, detail)))
    app.handle_hook_error('native failure')
    assert app.capture_view().problem is not None
    assert app.capture_view().problem.action == 'open_capture_recovery'
    assert events[-1][0] == 'hook_error'
    assert events[-1][1]['reason'] == 'native failure'

def test_capture_host_event_resumes_saved_monitor_before_same_first_output_enters_runtime():
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
    profile.body_sources = [profile.hooks[0].identity]
    app.select_profile(app.game_id, app.profile_id)
    profile = app.current_profile()
    app.select_primary_target(101, 500)
    app.hook_host = Host()
    assert app.activate_selected_target() == 'connecting'
    app.handle_hook_connection_event(HookEvent('connected', 'host', 1, 1, 10, process_id=101))

    first = HookEvent('output', 'host', 1, 2, 20, 101, text, '最初の台詞')
    outcome = app.handle_capture_host_event(first)
    assert outcome.monitor_started
    token, delay = app.handle_runtime_hook_output(first)
    assert delay is None
    assert token is not None
    assert app.runtime_state().utterance.raw == '最初の台詞'


def test_active_body_source_removal_automatically_falls_back_to_next_saved_provider():
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
    primary = source('A', 1, address=1)
    fallback = source('B', 3, address=2)
    profile.hooks = [
        HookSelection('A', 1, 2),
        HookSelection('B', 3, 4),
    ]
    profile.body_sources = [hook.identity for hook in profile.hooks]
    app.select_profile(app.game_id, app.profile_id)
    profile = app.current_profile()
    app.select_primary_target(101, 500)
    app.hook_host = Host()
    app.begin_hook_connection(101, profile)
    app.handle_hook_connection_event(HookEvent('connected', 'host', 1, 1, 10, process_id=101))
    app.observe_capture(primary, 'A', sequence=1)
    app.observe_capture(fallback, 'B', sequence=2)
    app.request_saved_monitor_resume(profile)
    app.try_resume_saved_monitor(profile)
    assert app.runtime_state().active_body_source == primary.identity

    removed = HookEvent('removed', 'host', 1, 3, 30, 101, primary)
    outcome = app.handle_capture_host_event(removed)
    assert outcome.active_source_removed
    assert outcome.monitor_started
    assert app.runtime_state().active_body_source == fallback.identity
    assert app.capture_view().problem is None


def test_public_profile_setting_api_cannot_bypass_capture_workspace_transaction():
    import pytest
    app, calls = app_with_game()
    assert not hasattr(app, 'update_profile')
    assert not hasattr(app, 'update_capture_recipe')
    with pytest.raises(ValueError, match='unsupported profile setting'):
        app.set_profile_setting('hooks', [HookSelection('T', 1, 2)])
    assert app.current_profile().hooks == []
    assert calls == []


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
    profile.body_sources = [profile.hooks[0].identity]
    app.select_profile(app.game_id, app.profile_id)
    profile = app.current_profile()
    app.select_primary_target(101, 500)
    app.hook_host = Host()
    app.begin_hook_connection(101, profile)
    app.accept_hook_connection_event(HookEvent('connected', 'host', 1, 1, 10, 101))
    app.observe_capture(text, '本文', sequence=1)
    events = []
    app.configure_runtime_loop(
        schedule=lambda *_: None,
        on_event=lambda name, token, detail: events.append((name, token, detail)))
    state = app.start_monitoring()
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
    app.observe_capture(candidate, '大和「台詞」', sequence=1)
    app.set_capture_field_source('body', candidate.identity, candidate.address)
    app.save_capture_source_rule(candidate.identity, candidate.address, None, rule)
    assert app.current_profile().source_rules == before
    assert app.capture_profile_snapshot().source_rules[candidate.identity][0] == rule
    assert calls == []
    app.commit_capture()
    assert app.current_profile().source_rules[candidate.identity][0] == rule
    assert calls == ['save']
