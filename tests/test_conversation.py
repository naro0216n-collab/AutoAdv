import unittest
from types import SimpleNamespace
from tests.g2p_support import TEST_G2P, FakeG2P

from autoadv.conversation import Controller, Target, Token
from autoadv.model import Profile, HookSelection, AdvanceMode, interpret, ReplacementRule


SOURCE = SimpleNamespace(hook_code='H/A', context=3, context2=4, identity='H/A|3|4')


def profile():
    return Profile('profile-1', 'Default', hooks=[HookSelection('H/A', 3, 4)],
                   body_source='H/A|3|4', advance_mode=AdvanceMode.TEXT, reading_ms=10)


class DialogueContractTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.target_ready = True
        self.controller = Controller(lambda t, k: self.sent.append((t, k)),
                                     lambda t: self.target_ready, TEST_G2P)
        self.controller.begin(profile(), Target(101, 202), 'host-a', 1)

    def output(self, text, seq, *, source=SOURCE, gen='host-a', pid=101, conn=1):
        return self.controller.observe(generation=gen, connection=conn, sequence=seq,
                                       source=source, text=text, pid=pid)

    def test_requires_permission_and_actual_presentation_end(self):
        ticket = self.output('京「おはよう」', 1)
        self.assertEqual(self.controller.utterance.speaker, '京')
        self.assertFalse(self.controller.try_advance())
        self.controller.permit(True)
        self.assertFalse(self.controller.try_advance())
        self.assertTrue(self.controller.presentation_event(ticket, 'text_elapsed'))
        self.assertTrue(self.controller.try_advance())
        self.assertFalse(self.controller.try_advance())
        self.assertEqual(len(self.sent), 1)

    def test_can_enable_after_completion(self):
        token = self.output('同じ台詞', 1)
        self.controller.presentation_event(token, 'text_elapsed')
        self.controller.permit(True)
        self.assertTrue(self.controller.try_advance())

    def test_same_raw_line_requires_explicit_profile_rule(self):
        self.controller.permit(True)
        first = self.output('同じ台詞', 1)
        self.controller.presentation_event(first, 'text_elapsed')
        self.controller.try_advance()
        self.assertIsNone(self.output('同じ台詞', 2))
        self.assertEqual(self.controller.pause_reason, 'ambiguous_identical_line')
        self.assertTrue(self.controller.authorized)
        second = self.output('違う台詞', 3)
        self.assertNotEqual(second, first)
        self.controller.presentation_event(second, 'text_elapsed')
        self.controller.permit(True)
        self.assertTrue(self.controller.try_advance())
        self.assertEqual(len(self.sent), 2)

    def test_each_flush_rule_accepts_same_text_after_external_progression(self):
        p = profile()
        p.repeat_rule = 'each_flush'
        self.controller.begin(p, Target(101, 202), 'host-a', 1)
        self.controller.permit(True)
        first = self.output('同じ台詞', 1)
        self.controller.pause('native_acoustic_unconfirmed_hold')
        second = self.output('同じ台詞', 2)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertIsNone(self.controller.pause_reason)
        self.assertFalse(self.controller.completed)
        self.assertTrue(self.controller.authorized)

    def test_partial_update_invalidates_previous_presentation(self):
        a = self.output('これ', 1)
        b = self.output('これは全文', 2)
        self.assertNotEqual(a, b)
        self.assertFalse(self.controller.presentation_event(a, 'text_elapsed'))
        self.controller.permit(True)
        self.assertFalse(self.controller.try_advance())
        self.controller.presentation_event(b, 'text_elapsed')
        self.assertTrue(self.controller.try_advance())

    def test_user_authorization_is_independent_of_runtime_session_and_holds(self):
        controller = Controller(lambda *_: None, lambda _: True, TEST_G2P)
        controller.permit(True)
        self.assertTrue(controller.authorized)
        controller.begin(profile(), Target(101, 202), 'host-a', 1)
        token = controller.observe(generation='host-a', connection=1, sequence=1,
                                   source=SOURCE, text='古い', pid=101)
        self.assertTrue(controller.authorized)

        controller.begin(profile(), Target(102, 203), 'host-b', 1)
        self.assertFalse(controller.presentation_event(token, 'text_elapsed'))
        self.assertTrue(controller.authorized)
        self.assertFalse(controller.try_advance())

        self.assertIsNone(controller.observe(generation='wrong-host', connection=1, sequence=2,
                                              source=SOURCE, text='wrong gen', pid=102))
        self.assertEqual(controller.pause_reason, 'host_changed')
        self.assertTrue(controller.authorized)

        controller.close('session_changed')
        self.assertTrue(controller.authorized)
        self.assertEqual(controller.pause_reason, 'session_changed')
        controller.permit(False)
        self.assertFalse(controller.authorized)
        self.assertEqual(controller.pause_reason, 'session_changed')

    def test_runtime_hold_preserves_auto_mode_and_next_line_resumes(self):
        token = self.output('まだ読む', 1)
        self.controller.permit(True)
        self.controller.pause('native_acoustic_unconfirmed_hold')
        self.controller.presentation_event(token, 'text_elapsed')
        self.assertFalse(self.controller.try_advance())
        self.assertTrue(self.controller.authorized)
        self.assertIsNotNone(self.output('次', 2))
        self.assertTrue(self.controller.authorized)
        self.assertIsNone(self.controller.pause_reason)
        self.assertFalse(self.controller.completed)

    def test_other_pid_and_stale_sequence_not_accepted(self):
        self.assertIsNone(self.output('他ゲーム', 1, pid=9, gen='host-b'))
        token = self.output('OK', 1)
        self.assertIsNotNone(token)
        self.assertIsNone(self.output('stale', 1))
        self.assertEqual(self.controller.utterance.raw, 'OK')

    def test_target_readiness_hold_can_resume_without_rewriting_auto_mode(self):
        self.controller.permit(True)
        token = self.output('台詞', 1)
        self.controller.presentation_event(token, 'text_elapsed')
        self.target_ready = False
        self.assertFalse(self.controller.try_advance())
        self.target_ready = True
        self.controller.permit(True)
        self.assertTrue(self.controller.try_advance())
        self.assertEqual(len(self.sent), 1)

    def test_failed_input_is_not_retried(self):
        self.controller.input_send = lambda *_: (_ for _ in ()).throw(OSError('input error'))
        self.controller.permit(True)
        self.controller.presentation_event(self.output('台詞', 1), 'text_elapsed')
        self.assertFalse(self.controller.try_advance())
        self.controller.permit(True)
        self.assertFalse(self.controller.try_advance())
        self.assertTrue(self.controller.sent)

    def test_text_transform_is_single_utterance(self):
        p = profile()
        p.default_tts = True
        p.default_voice = 'narrator'
        p.replacements = [ReplacementRule('東京', 'とうきょう')]
        p.speaker_tts = {'京': False}
        result = interpret('京「東京へ行く(いく)」', p, session=2, line=3, source='x', sequence=6, g2p=TEST_G2P)
        self.assertEqual(result.speech.tts_text, 'とうきょうへ行く(いく)')
        self.assertEqual(result.use_tts, False)
        self.assertEqual(result.speaker, '京')


    def test_duplicate_selected_hook_is_rejected(self):
        p = profile()
        p.hooks.append(HookSelection('H/A', 3, 4))
        with self.assertRaises(ValueError):
            p.validate()


if __name__ == '__main__':
    unittest.main()

class HookBoundaryContractTests(unittest.TestCase):
    """Source event boundaries, not a post-input clock, own the next line."""

    def setUp(self):
        self.sent = []
        self.controller = Controller(lambda *_: self.sent.append(1), lambda _: True, TEST_G2P)
        self.controller.begin(profile(), Target(101, 202), 'host-a', 1)
        self.controller.permit(True)

    def out(self, text, seq):
        return self.controller.observe(generation='host-a', connection=1, sequence=seq,
                                       source=SOURCE, text=text, pid=101)

    def sent_line(self, text):
        token = self.out(text, 1)
        self.assertTrue(self.controller.presentation_event(token, 'text_elapsed'))
        self.assertTrue(self.controller.try_advance())
        return token

    def test_luna_finalized_outputs_replace_prefix_guessing(self):
        before = self.sent_line('あ')
        # Luna has already flushed the buffer; a new notification is not
        # classified solely by a string prefix relationship.
        after = self.out('あい', 2)
        self.assertNotEqual(before, after)
        self.assertFalse(self.controller.completed)
        self.assertTrue(self.controller.authorized)
        self.assertTrue(self.controller.presentation_event(after, 'text_elapsed'))
        self.assertTrue(self.controller.try_advance())
        self.assertEqual(len(self.sent), 2)

    def test_empty_hook_notification_does_not_invent_boundary(self):
        first = self.sent_line('同じ')
        self.assertIsNone(self.out('', 2))
        self.assertIsNone(self.out('同じ', 3))
        self.assertEqual(first, self.controller.token)
        self.assertEqual(self.controller.pause_reason, 'ambiguous_identical_line')
        self.assertTrue(self.controller.authorized)

    def test_same_text_as_two_flushed_lines_when_configured(self):
        p = profile()
        p.repeat_rule = 'each_flush'
        self.controller.begin(p, Target(101, 202), 'host-b', 2)
        self.controller.permit(True)
        first = self.controller.observe(generation='host-b', connection=2, sequence=1,
                                        source=SOURCE, text='同じ', pid=101)
        self.assertTrue(self.controller.presentation_event(first, 'text_elapsed'))
        self.assertTrue(self.controller.try_advance())
        second = self.controller.observe(generation='host-b', connection=2, sequence=2,
                                         source=SOURCE, text='同じ', pid=101)
        self.assertNotEqual(first, second)
        self.assertTrue(self.controller.presentation_event(second, 'text_elapsed'))
        self.assertTrue(self.controller.try_advance())

    def test_source_gap_does_not_come_from_unselected_hook(self):
        before = self.sent_line('同じ')
        other = SimpleNamespace(hook_code='other', context=3, context2=4, identity='other|3|4')
        self.assertIsNone(self.controller.observe(generation='host-a', connection=1, sequence=2,
                source=other, text='', pid=101))
        self.assertIsNone(self.out('同じ', 3))
        self.assertEqual(self.controller.token, before)
        self.assertTrue(self.controller.authorized)

    def test_old_session_clear_preserves_existing_user_authorization(self):
        first = self.sent_line('同じ')
        self.out('', 2)
        self.controller.begin(profile(), Target(102, 203), 'host-b', 2)
        self.assertFalse(self.controller.presentation_event(first, 'text_elapsed'))
        self.assertTrue(self.controller.authorized)
        self.assertIsNone(self.out('同じ', 3))
