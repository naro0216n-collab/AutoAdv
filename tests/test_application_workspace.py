from autoadv.application import AutoAdvApplication
from autoadv.hook_protocol import HookEvent, ThreadSource
from autoadv.storage import Catalog


def source(code, context, address=1):
    return ThreadSource(101, address, context, context + 1, code, code)


def app_with_game():
    catalog = Catalog()
    game = catalog.create_game('Game')
    calls = []
    app = AutoAdvApplication(catalog, lambda: calls.append('save'))
    app.select_profile(game.id, game.default_profile)
    return app, calls


def test_capture_workspace_observes_real_output_without_manual_refresh():
    app, _ = app_with_game()
    a = source('A', 1)
    app.observe_capture(a, '大和「一つ目」', sequence=1)
    app.observe_capture(a, '大和「二つ目」', sequence=2)
    view = app.capture_view()
    assert len(view.candidates) == 1
    assert view.candidates[0].history == ('大和「一つ目」', '大和「二つ目」')
    assert view.candidates[0].role is None


def test_single_dialogue_and_speaker_sources_pair_without_a_separate_pair_step():
    app, calls = app_with_game()
    text = source('T', 1)
    speaker = source('S', 5, address=2)
    text_event = HookEvent('output', 'host', 1, 1, 100, 101, text, 'おはよう')
    speaker_event = HookEvent('output', 'host', 1, 2, 110, 101, speaker, '大和')
    app.observe_capture(text, 'おはよう', sequence=1)
    app.observe_capture(speaker, '大和', sequence=2)
    app.observe_capture_group((text_event, speaker_event))
    app.set_capture_role(text.identity, text.address, 'text')
    app.set_capture_role(speaker.identity, speaker.address, 'speaker')
    view = app.capture_view()
    assert view.can_commit
    assert view.previews[-1].speaker == '大和'
    assert view.previews[-1].text == 'おはよう'
    assert calls == []  # editing the recipe is a draft
    updated = app.commit_capture()
    assert calls == ['save']
    text_hook = next(h for h in updated.hooks if h.role == 'text')
    speaker_hook = next(h for h in updated.hooks if h.role == 'speaker')
    assert updated.speaker_links[text_hook.identity] == speaker_hook.identity


def test_capture_recipe_does_not_persist_until_committed():
    app, calls = app_with_game()
    candidate = source('T', 1)
    app.observe_capture(candidate, '大和「台詞」', sequence=1)
    app.set_capture_role(candidate.identity, candidate.address, 'text')
    assert app.current_profile().hooks == []
    assert app.capture_view().dirty
    assert calls == []
    app.commit_capture()
    assert [h.code for h in app.current_profile().hooks] == ['T']
    assert calls == ['save']


def test_preview_uses_product_interpreter_not_a_ui_copy():
    app, _ = app_with_game()
    candidate = source('T', 1)
    app.observe_capture(candidate, '大和「おはよう」', sequence=1)
    app.set_capture_role(candidate.identity, candidate.address, 'text')
    preview = app.capture_view().previews[-1]
    assert preview.speaker == '大和'
    assert preview.text == 'おはよう'
    assert preview.reading == 'おはよう'


def test_ambiguous_source_cannot_be_committed():
    app, _ = app_with_game()
    candidate = source('T', 1)
    app.observe_capture(candidate, '台詞', sequence=1)
    app.set_capture_role(candidate.identity, candidate.address, 'text')
    app.observe_capture(candidate, '', ambiguous=True)
    view = app.capture_view()
    assert not view.can_commit
    assert view.problem is not None


def test_multi_hook_preview_never_reuses_a_later_speaker_for_older_dialogue():
    app, _ = app_with_game()
    text = source('T', 1)
    speaker = source('S', 5, address=2)
    app.observe_capture(text, '一つ目', sequence=1)
    app.observe_capture(speaker, '大和', sequence=2)
    app.observe_capture_group((
        HookEvent('output', 'host', 1, 1, 100, 101, text, '一つ目'),
        HookEvent('output', 'host', 1, 2, 110, 101, speaker, '大和'),
    ))
    app.observe_capture(text, '二つ目', sequence=3)
    app.observe_capture(speaker, '京', sequence=4)
    app.observe_capture_group((
        HookEvent('output', 'host', 1, 3, 200, 101, text, '二つ目'),
        HookEvent('output', 'host', 1, 4, 210, 101, speaker, '京'),
    ))
    app.set_capture_role(text.identity, text.address, 'text')
    app.set_capture_role(speaker.identity, speaker.address, 'speaker')
    previews = app.capture_view().previews
    assert [(p.speaker, p.text) for p in previews] == [('大和', '一つ目'), ('京', '二つ目')]


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


def test_structure_rules_are_part_of_capture_transaction_not_independent_persistence():
    from autoadv.model import StructureRule
    app, calls = app_with_game()
    candidate = source('T', 1)
    app.observe_capture(candidate, '大和：台詞', sequence=1)
    app.set_capture_role(candidate.identity, candidate.address, 'text')
    original_rules = list(app.current_profile().structure_rules)
    rule = StructureRule(r'^(?P<speaker>[^：]+)：(?P<body>.+)$')
    app.update_capture_recipe(lambda profile: profile.structure_rules.insert(0, rule))
    assert calls == []
    assert app.current_profile().structure_rules == original_rules
    assert app.capture_view().previews[-1].speaker == '大和'
    assert app.capture_view().previews[-1].text == '台詞'
    app.commit_capture()
    assert calls == ['save']
    assert app.current_profile().structure_rules[0] == rule


def test_common_setting_is_persisted_by_application_without_form_reconstruction():
    app, calls = app_with_game()
    app.set_common_setting('reading_ms', 880)
    assert app.catalog.common.reading_ms == 880
    assert calls == ['save']
