"""Controller state-model contracts for presentation, authorization, and input."""
from __future__ import annotations

from copy import deepcopy

import pytest

from autoadv.conversation import Controller, Target
from autoadv.hook_protocol import HookEvent
from autoadv.model import (AdvanceMode, HookSelection, ReplacementRule, SpeakerStyle,
                           BODY_ONLY_RULE, SPEAKER_ONLY_RULE)
from autoadv.utterance_progress import UtteranceProgress, UtteranceProgressState
from tests.contracts_support import SOURCE, controller, observe, profile, source
from tests.g2p_support import TEST_G2P


def test_progress_mode_state_model_has_one_completion_reason_and_one_send():
    scenarios = (
        # mode, tts, fallback, events, native, expected_reason
        (AdvanceMode.TEXT, False, False, ('text_elapsed',), False, 'text'),
        (AdvanceMode.TEXT, True, False, ('tts_finished', 'text_elapsed'), False, 'tts_and_text'),
        (AdvanceMode.TEXT, True, False, ('text_elapsed', 'tts_finished'), False, 'tts_and_text'),
        (AdvanceMode.AUDIO, True, True, ('tts_finished',), True, 'tts'),
        (AdvanceMode.AUDIO, False, True, ('fallback_due',), False, 'uncertain_text_fallback'),
        (AdvanceMode.AUDIO, False, False, (), True, 'utterance_progress'),
    )
    for mode, tts, fallback, events, native, expected in scenarios:
        ctrl, sent, _ = controller(mode=mode, tts=tts, fallback=fallback)
        ctrl.permit(True)
        token = observe(ctrl, '「音声テスト」')
        assert token is not None
        for event in events:
            ctrl.presentation_event(token, event)
        if native:
            ctrl.apply_utterance_progress(token, UtteranceProgress(state=UtteranceProgressState.ENDED))
        assert ctrl.presentation_reason == expected
        assert ctrl.try_advance()
        assert not ctrl.try_advance()
        assert len(sent) == 1
        # Any late evidence for the same token is inert after reservation/send.
        assert not ctrl.presentation_event(token, 'text_elapsed')
        assert not ctrl.apply_utterance_progress(token, UtteranceProgress(state=UtteranceProgressState.ENDED))


def test_completion_evidence_is_mode_specific_and_uncertainty_policy_does_not_relabel_native():
    ctrl, _, _ = controller(mode=AdvanceMode.TEXT, tts=False, fallback=True)
    token = observe(ctrl, '「本文」')
    assert token is not None
    assert not ctrl.apply_utterance_progress(token, UtteranceProgress(state=UtteranceProgressState.ENDED))
    assert not ctrl.presentation_event(token, 'tts_finished')
    assert ctrl.presentation_event(token, 'text_elapsed')
    assert ctrl.presentation_reason == 'text'

    ctrl, _, _ = controller(mode=AdvanceMode.AUDIO, tts=False, fallback=False)
    token = observe(ctrl, '「本文」')
    assert token is not None
    # Strict audio mode has no fallback deadline at all; unresolved audio simply
    # remains incomplete until integrated progress reaches ENDED.
    assert not ctrl.presentation_event(token, 'fallback_due')
    assert not ctrl.fallback_due and ctrl.presentation_reason is None
    assert ctrl.apply_utterance_progress(token, UtteranceProgress(state=UtteranceProgressState.ENDED))
    assert ctrl.presentation_reason == 'utterance_progress'
    assert ctrl.pause_reason is None

    ctrl, _, _ = controller(mode=AdvanceMode.AUDIO, tts=False, fallback=True)
    token = observe(ctrl, '「本文」')
    assert token is not None
    assert not ctrl.apply_utterance_progress(
        token, UtteranceProgress(state=UtteranceProgressState.UNKNOWN))
    assert ctrl.pause_reason is None
    assert ctrl.presentation_event(token, 'fallback_due')
    assert ctrl.presentation_reason == 'uncertain_text_fallback'

    # The text deadline never cuts a target that the integrated audio judgment
    # still classifies as ACTIVE. If that judgment later becomes UNKNOWN, the
    # explicit uncertainty fallback may act without relabeling audio as ended.
    active, _, _ = controller(mode=AdvanceMode.AUDIO, tts=False, fallback=True)
    active_token = observe(active, '「短い本文」')
    assert active_token is not None
    # Audio progress is not a one-way latch.  Repeated acoustic evidence may
    # reopen and release cut risk any number of times before the fallback policy
    # boundary; none of those transitions completes the Presentation itself.
    for state in (
        UtteranceProgressState.ACTIVE,
        UtteranceProgressState.UNKNOWN,
        UtteranceProgressState.ACTIVE,
        UtteranceProgressState.UNKNOWN,
        UtteranceProgressState.ACTIVE,
    ):
        assert not active.apply_utterance_progress(
            active_token, UtteranceProgress(state=state))
    assert not active.presentation_event(active_token, 'fallback_due')
    assert active.presentation_reason is None and not active.completed
    # Once the explicit fallback deadline has elapsed, the next unresolved
    # audio judgment may complete by fallback.  This is the only time-driven
    # completion path; UNKNOWN itself is never an audio endpoint.
    assert active.apply_utterance_progress(active_token, UtteranceProgress(
        state=UtteranceProgressState.UNKNOWN))
    assert active.presentation_reason == 'uncertain_text_fallback'

    strict, _, _ = controller(mode=AdvanceMode.AUDIO, tts=False, fallback=False)
    strict_token = observe(strict, '「本文」')
    assert strict_token is not None
    assert not strict.apply_utterance_progress(
        strict_token, UtteranceProgress(state=UtteranceProgressState.UNKNOWN))
    assert strict.presentation_reason is None and not strict.completed
    assert strict.apply_utterance_progress(strict_token, UtteranceProgress(state=UtteranceProgressState.ENDED))
    assert strict.presentation_reason == 'utterance_progress'


def test_user_authorization_runtime_holds_and_input_failure_are_independent_state():
    ctrl, sent, readiness = controller(mode=AdvanceMode.TEXT, tts=False)
    token = observe(ctrl, '「一つ目」')
    assert token is not None
    assert ctrl.presentation_event(token, 'text_elapsed')
    assert not ctrl.try_advance()  # AUTO remains an explicit user choice.

    ctrl.permit(True)
    readiness['ready'] = False
    assert not ctrl.try_advance()
    assert ctrl.authorized and ctrl.pause_reason == 'target_unavailable'
    readiness['ready'] = True
    assert ctrl.try_advance()
    assert ctrl.authorized and len(sent) == 1

    # Runtime/session transitions never silently rewrite the user's AUTO state.
    ctrl.close('session_changed')
    assert ctrl.authorized and ctrl.pause_reason == 'session_changed'
    ctrl.permit(False)
    assert not ctrl.authorized and ctrl.pause_reason == 'session_changed'

    # I/O failure reserves the utterance and is never automatically retried.
    failures = []
    failing = Controller(lambda *_: failures.append(1) or (_ for _ in ()).throw(OSError('boom')),
                         lambda _target: True, TEST_G2P)
    failing.begin(profile(), Target(101, 202), 'host', 1)
    failing.permit(True)
    token = failing.observe(generation='host', connection=1, sequence=1,
                            source=SOURCE, text='「失敗」', pid=101)
    assert token is not None and failing.presentation_event(token, 'text_elapsed')
    assert not failing.try_advance()
    assert failing.sent and failing.pause_reason == 'input_failure_no_retry'
    assert not failing.try_advance() and len(failures) == 1


def test_external_input_observed_during_target_preflight_prevents_operation_reservation():
    sent = []
    ctrl = None

    def ready(_target):
        assert ctrl is not None
        assert ctrl.record_external_input(ctrl.session, 1200)
        return True

    ctrl = Controller(lambda _target, key: sent.append(key), ready, TEST_G2P)
    ctrl.begin(profile(), Target(101, 202), 'host', 1)
    ctrl.permit(True)
    token = observe(ctrl, '「一つ目」')
    assert token is not None and ctrl.presentation_event(token, 'text_elapsed')

    assert not ctrl.try_advance()
    assert not ctrl.sent and ctrl.completed and ctrl.authorized and sent == []



def test_session_sequence_and_line_identity_reject_stale_or_ambiguous_events_without_guessing():
    ctrl, sent, _ = controller()
    ctrl.permit(True)
    first = observe(ctrl, '同じ台詞', 1)
    assert first is not None and ctrl.presentation_event(first, 'text_elapsed') and ctrl.try_advance()

    assert observe(ctrl, '同じ台詞', 2) is None
    assert ctrl.pause_reason == 'ambiguous_identical_line'
    assert ctrl.authorized

    second = observe(ctrl, '違う台詞', 3)
    assert second is not None and second != first
    assert ctrl.presentation_event(second, 'text_elapsed') and ctrl.try_advance()
    assert len(sent) == 2

    # Wrong PID/generation and stale sequence cannot mutate the active line.
    active = ctrl.token
    assert observe(ctrl, '他ゲーム', 4, pid=999) is None
    assert observe(ctrl, '旧接続', 4, generation='old') is None
    assert observe(ctrl, 'stale', 3) is None
    assert ctrl.token == active

    # Explicit each-flush policy makes repeated finalized outputs distinct.
    p = profile()
    p.repeat_rule = 'each_flush'
    ctrl.begin(p, Target(101, 202), 'host2', 2)
    ctrl.permit(True)
    a = ctrl.observe(generation='host2', connection=2, sequence=1, source=SOURCE, text='同じ', pid=101)
    b = ctrl.observe(generation='host2', connection=2, sequence=2, source=SOURCE, text='同じ', pid=101)
    assert a is not None and b is not None and a != b


def test_profile_changes_are_staged_at_utterance_boundaries_and_capture_ownership_cannot_mutate_in_place():
    p = profile(tts=True)
    p.speaker_tts['京'] = True
    p.speaker_voices['京'] = 'voice-1'
    p.speaker_styles['京'] = SpeakerStyle(timbre='soft')
    ctrl = Controller(lambda *_: None, lambda _target: True, TEST_G2P)
    ctrl.begin(p, Target(101, 202), 'host', 1)
    first = ctrl.observe(generation='host', connection=1, sequence=1,
                         source=SOURCE, text='京「一つ目」', pid=101)
    assert first is not None and (ctrl.utterance.voice, ctrl.utterance.tts_caption) == (
        'voice-1', '柔らかい声。')

    staged = deepcopy(p)
    staged.speaker_voices['京'] = 'voice-2'
    staged.speaker_styles['京'] = SpeakerStyle(demeanor='calm', pace='slightly_slow')
    ctrl.stage_profile(staged)
    assert (ctrl.utterance.voice, ctrl.utterance.tts_caption) == ('voice-1', '柔らかい声。')
    second = ctrl.observe(generation='host', connection=1, sequence=2,
                          source=SOURCE, text='京「二つ目」', pid=101)
    assert second is not None and (ctrl.utterance.voice, ctrl.utterance.tts_caption) == (
        'voice-2', '穏やかな雰囲気で、ややゆっくり話す。')

    capture_change = deepcopy(staged)
    capture_change.hooks.append(HookSelection('OTHER', 3, 4))
    with pytest.raises(ValueError, match='capture ownership'):
        ctrl.stage_profile(capture_change)


def test_active_utterance_is_immutable_under_setting_changes():
    p = profile(tts=True)
    p.replacements = [ReplacementRule('東京', 'とうきょう', target='both')]
    ctrl = Controller(lambda *_: None, lambda _target: True, TEST_G2P)
    ctrl.begin(p, Target(101, 202), 'host', 1)
    token = ctrl.observe(generation='host', connection=1, sequence=1,
                         source=SOURCE, text='「東京」', pid=101)
    before = deepcopy(ctrl.utterance)
    staged = deepcopy(p)
    staged.replacements = [ReplacementRule('東京', 'TOKYO', target='both')]
    ctrl.stage_profile(staged)
    assert ctrl.token == token and ctrl.utterance == before


def test_multisource_dialogue_is_composed_only_inside_one_synchronized_group():
    p = profile()
    body = HookSelection('T', 1, 2)
    unused = HookSelection('S1', 3, 4)
    speaker = HookSelection('S2', 5, 6)
    p.hooks = [body, unused, speaker]
    p.body_source = body.identity
    p.speaker_source = speaker.identity
    p.source_rules[body.identity] = [BODY_ONLY_RULE]
    p.source_rules[speaker.identity] = [SPEAKER_ONLY_RULE]
    ctrl = Controller(lambda *_: None, lambda _target: True, TEST_G2P)
    ctrl.begin(p, Target(101, 202), 'host', 1)

    def event(hook, sequence, text):
        return HookEvent('output', 'host', 1, sequence, sequence * 10, 101,
                         source(hook.code, hook.context, address=sequence), text)

    # The selected body and speaker providers may be distinct; an unselected
    # source in the same batch cannot override either field.
    token = ctrl.observe_group((event(unused, 1, 'wrong'),
                                event(speaker, 2, '花子'),
                                event(body, 3, '台詞')))
    assert token is not None
    assert (ctrl.utterance.speaker, ctrl.utterance.text) == ('花子', '台詞')

    ctrl.permit(True)
    assert ctrl.observe_group((event(speaker, 4, '古い'),)) is None
    assert ctrl.authorized
    assert ctrl.observe_group((event(body, 5, '次'),)) is not None
    assert (ctrl.utterance.speaker, ctrl.utterance.text) == ('', '次')


def test_external_progress_and_stale_presentation_evidence_preserve_state_ownership():
    ctrl, sent, _ = controller()
    ctrl.permit(True)
    first = ctrl.observe(
        generation='host', connection=1, sequence=1, source=SOURCE,
        text='「一つ目」', pid=101, received_ns=1_000_000_000)
    assert first is not None

    # External game input is only a timestamp fact, not a dialogue boundary or
    # an AUTO mutation.  It does make the already-admitted token stale for any
    # later AutoAdv operation, even if that token subsequently completes.
    assert ctrl.record_external_input(ctrl.session, 1200)
    assert ctrl.presentation_event(first, 'text_elapsed')
    assert ctrl.completed and ctrl.authorized and ctrl.pause_reason is None
    assert not ctrl.try_advance() and sent == []

    # A later admitted body consumes the input as timing evidence.  The same
    # body text can therefore represent a new Presentation without inventing a
    # boundary from the input alone.
    second = ctrl.observe(
        generation='host', connection=1, sequence=2, source=SOURCE,
        text='「一つ目」', pid=101, received_ns=1_500_000_000)
    assert second is not None and second != first
    timing = ctrl.presentation_timing(second)
    assert timing is not None and timing.input_candidates_ms == (1200,)
    assert timing.causal_lower_bound_ms is None
    assert ctrl.authorized and ctrl.presentation_reason is None
    assert not ctrl.presentation_event(first, 'text_elapsed')

    # Only evidence for the active token can reserve an input. After one send,
    # no new Presentation state exists until another body is actually admitted.
    assert ctrl.presentation_event(second, 'text_elapsed')
    assert ctrl.try_advance() and len(sent) == 1
    assert ctrl.authorized and ctrl.pause_reason is None
    assert not ctrl.presentation_event(second, 'text_elapsed')
    assert not ctrl.apply_utterance_progress(
        second, UtteranceProgress(state=UtteranceProgressState.ENDED))
    assert not ctrl.try_advance() and len(sent) == 1

    # Presentation failure is likewise a runtime hold, not an AUTO mutation.
    tts, _, _ = controller(tts=True)
    tts.permit(True)
    token = observe(tts, '「音声」')
    assert token is not None
    assert not tts.presentation_event(token, 'tts_finished', success=False)
    assert tts.authorized and tts.pause_reason == 'tts_playback_failed'


def test_stable_ocr_body_changes_share_the_normal_utterance_admission_contract():
    p = profile()
    p.source = 'ocr'
    ctrl = Controller(lambda *_: None, lambda _target: True, TEST_G2P)
    ctrl.begin(p, Target(101, 202), 'ocr', 1)
    first = ctrl.observe_ocr(sequence=1, text='一つ目', pid=101, session=ctrl.session)
    assert first is not None

    # OCRWorker has already stabilized this changed body.  A blank frame is not
    # a second Presentation-boundary requirement.
    second = ctrl.observe_ocr(sequence=2, text='二つ目', pid=101, session=ctrl.session)
    assert second is not None and second != first
    assert ctrl.utterance.text == '二つ目'

    # An unchanged stable OCR body is still the same Presentation.  A stable
    # blank remains useful only for distinguishing the otherwise identical next
    # body.
    assert ctrl.observe_ocr(sequence=3, text='二つ目', pid=101, session=ctrl.session) is None
    assert ctrl.observe_ocr(sequence=4, text='', pid=101, session=ctrl.session) is None
    third = ctrl.observe_ocr(sequence=5, text='二つ目', pid=101, session=ctrl.session)
    assert third is not None and third != second
