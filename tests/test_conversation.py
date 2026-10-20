import unittest
from types import SimpleNamespace
from autoadv.conversation import Controller, Target, Token
from autoadv.model import Profile, HookSelection, AudioMode, interpret, ReplacementRule


SOURCE = SimpleNamespace(hook_code='H/A', context=3, context2=4, identity='H/A|3|4')


def profile():
    return Profile('profile-1', 'Default', hooks=[HookSelection('H/A', 3, 4)],
                   default_mode=AudioMode.READING, reading_ms=10)


class DialogueContractTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.foreground = True
        self.controller = Controller(lambda t, k: self.sent.append((t, k)),
                                     lambda t: self.foreground)
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
        self.assertTrue(self.controller.mark_complete(ticket))
        self.assertTrue(self.controller.try_advance())
        self.assertFalse(self.controller.try_advance())
        self.assertEqual(len(self.sent), 1)

    def test_can_enable_after_completion(self):
        token = self.output('同じ台詞', 1)
        self.controller.mark_complete(token)
        self.controller.permit(True)
        self.assertTrue(self.controller.try_advance())

    def test_same_raw_line_requires_explicit_profile_rule(self):
        self.controller.permit(True)
        first = self.output('同じ台詞', 1)
        self.controller.mark_complete(first)
        self.controller.try_advance()
        self.assertIsNone(self.output('同じ台詞', 2))
        self.assertEqual(self.controller.reason, 'ambiguous_identical_line')
        self.assertFalse(self.controller.authorized)
        second = self.output('違う台詞', 3)
        self.assertNotEqual(second, first)
        self.controller.mark_complete(second)
        self.controller.permit(True)
        self.assertTrue(self.controller.try_advance())
        self.assertEqual(len(self.sent), 2)

    def test_same_text_new_notification_can_be_distinct_when_configured(self):
        p = profile()
        p.repeat_rule = 'each_after_input'
        self.controller.begin(p, Target(101, 202), 'host-a', 1)
        self.controller.permit(True)
        for seq in (1, 2):
            token = self.output('同じ台詞', seq)
            self.assertIsNotNone(token)
            self.controller.mark_complete(token)
            self.assertTrue(self.controller.try_advance())
        self.assertEqual(len(self.sent), 2)

    def test_partial_update_invalidates_previous_presentation(self):
        a = self.output('これ', 1)
        b = self.output('これは全文', 2)
        self.assertNotEqual(a, b)
        self.assertFalse(self.controller.mark_complete(a))
        self.controller.permit(True)
        self.assertFalse(self.controller.try_advance())
        self.controller.mark_complete(b)
        self.assertTrue(self.controller.try_advance())

    def test_session_switch_invalidates_old_audio_and_permission(self):
        self.controller.permit(True)
        old = self.output('古い', 1)
        self.controller.begin(profile(), Target(102, 203), 'host-b', 1)
        self.assertFalse(self.controller.mark_complete(old))
        self.assertFalse(self.controller.authorized)
        self.assertFalse(self.controller.try_advance())

    def test_manual_intervention_preserves_monitoring_but_revokes_input(self):
        token = self.output('まだ読む', 1)
        self.controller.permit(True)
        self.controller.pause('manual_intervention')
        self.controller.mark_complete(token)
        self.assertFalse(self.controller.try_advance())
        self.assertIsNotNone(self.output('次', 2))
        self.assertIsNotNone(self.controller.profile)

    def test_other_pid_and_stale_sequence_not_accepted(self):
        self.assertIsNone(self.output('他ゲーム', 1, pid=9, gen='host-b'))
        token = self.output('OK', 1)
        self.assertIsNotNone(token)
        self.assertIsNone(self.output('stale', 1))
        self.assertEqual(self.controller.utterance.raw, 'OK')

    def test_host_generation_change_revokes_authority(self):
        self.controller.permit(True)
        self.assertIsNone(self.output('wrong gen', 1, gen='host-b'))
        self.assertEqual(self.controller.reason, 'host_changed')
        self.assertFalse(self.controller.authorized)

    def test_foreground_check_and_failure_do_not_retry(self):
        self.controller.permit(True)
        token = self.output('台詞', 1)
        self.controller.mark_complete(token)
        self.foreground = False
        self.assertFalse(self.controller.try_advance())
        self.foreground = True
        self.controller.permit(True)
        self.assertTrue(self.controller.try_advance())
        self.assertEqual(len(self.sent), 1)

    def test_failed_input_is_not_retried(self):
        self.controller.input_send = lambda *_: (_ for _ in ()).throw(OSError('input error'))
        self.controller.permit(True)
        self.controller.mark_complete(self.output('台詞', 1))
        self.assertFalse(self.controller.try_advance())
        self.controller.permit(True)
        self.assertFalse(self.controller.try_advance())
        self.assertTrue(self.controller.sent)

    def test_text_transform_is_single_utterance(self):
        p = profile()
        p.default_mode = AudioMode.TTS
        p.default_voice = 'narrator'
        p.replacements = [ReplacementRule('東京', 'とうきょう')]
        p.speaker_modes = {'京': AudioMode.READING}
        result = interpret('京「東京へ行く(いく)」', p, session=2, line=3, source='x', sequence=6)
        self.assertEqual(result.reading, 'とうきょうへ行く(いく)')
        self.assertEqual(result.mode, AudioMode.READING)
        self.assertEqual(result.speaker, '京')

    def test_unique_hook_role(self):
        p = profile()
        p.hooks.append(HookSelection('H/A', 3, 4, 'speaker'))
        with self.assertRaises(ValueError):
            p.validate()


if __name__ == '__main__':
    unittest.main()

class HookBoundaryContractTests(unittest.TestCase):
    """Source event boundaries, not a post-input clock, own the next line."""

    def setUp(self):
        self.sent = []
        self.controller = Controller(lambda *_: self.sent.append(1), lambda _: True)
        self.controller.begin(profile(), Target(101, 202), 'host-a', 1)
        self.controller.permit(True)

    def out(self, text, seq):
        return self.controller.observe(generation='host-a', connection=1, sequence=seq,
                                       source=SOURCE, text=text, pid=101)

    def sent_line(self, text):
        token = self.out(text, 1)
        self.assertTrue(self.controller.mark_complete(token))
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
        self.assertTrue(self.controller.mark_complete(after))
        self.assertTrue(self.controller.try_advance())
        self.assertEqual(len(self.sent), 2)

    def test_empty_hook_notification_does_not_invent_boundary(self):
        first = self.sent_line('同じ')
        self.assertIsNone(self.out('', 2))
        self.assertIsNone(self.out('同じ', 3))
        self.assertEqual(first, self.controller.token)
        self.assertEqual(self.controller.reason, 'ambiguous_identical_line')
        self.assertFalse(self.controller.authorized)

    def test_same_text_as_two_flushed_lines_when_configured(self):
        p = profile()
        p.repeat_rule = 'each_after_input'
        self.controller.begin(p, Target(101, 202), 'host-b', 2)
        self.controller.permit(True)
        first = self.controller.observe(generation='host-b', connection=2, sequence=1,
                                        source=SOURCE, text='同じ', pid=101)
        self.assertTrue(self.controller.mark_complete(first))
        self.assertTrue(self.controller.try_advance())
        second = self.controller.observe(generation='host-b', connection=2, sequence=2,
                                         source=SOURCE, text='同じ', pid=101)
        self.assertNotEqual(first, second)
        self.assertTrue(self.controller.mark_complete(second))
        self.assertTrue(self.controller.try_advance())

    def test_source_gap_does_not_come_from_unselected_hook(self):
        before = self.sent_line('同じ')
        other = SimpleNamespace(hook_code='other', context=3, context2=4, identity='other|3|4')
        self.assertIsNone(self.controller.observe(generation='host-a', connection=1, sequence=2,
                source=other, text='', pid=101))
        self.assertIsNone(self.out('同じ', 3))
        self.assertEqual(self.controller.token, before)
        self.assertFalse(self.controller.authorized)

    def test_old_session_clear_does_not_authorize_new_game(self):
        first = self.sent_line('同じ')
        self.out('', 2)
        self.controller.begin(profile(), Target(102, 203), 'host-b', 2)
        self.assertFalse(self.controller.mark_complete(first))
        self.assertFalse(self.controller.authorized)
        self.assertIsNone(self.out('同じ', 3))
