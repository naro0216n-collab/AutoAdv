"""Lexical matching reports cues; acceptance needs audio timing and live evidence."""
import unittest
from autoadv.audio_match import TargetMatcher, phonetic_key
from autoadv.conversation import Controller, Target
from autoadv.model import Profile, AdvanceMode, HookSelection


class ASRCues(unittest.TestCase):
    def test_corrected_partial_transcript_can_recover(self):
        matcher = TargetMatcher('おはようございます')
        first = matcher.observe('おはおう')
        self.assertLess(first.coverage, .9)
        second = matcher.observe('おはようござ')
        self.assertLess(second.tail_confidence, .9)
        third = matcher.observe('おはようございます')
        self.assertLess(third.tail_confidence, .9)
        fourth = matcher.observe('おはようございます')
        self.assertGreaterEqual(fourth.coverage, .9)
        self.assertGreaterEqual(fourth.tail_confidence, .9)

    def test_single_omission_and_substitution_preserve_later_evidence(self):
        deletion = TargetMatcher('えっ嘘でしょ')
        first = deletion.observe('え嘘でしょ')
        self.assertEqual(first.matched_chars, 5)
        self.assertEqual(first.edit_count, 1)
        self.assertTrue(first.terminal_candidate)
        self.assertEqual(first.tail_confidence, 0)
        self.assertEqual(deletion.observe('え嘘でしょ').tail_confidence, 1)
        substitution = TargetMatcher('ヒューズが飛んだ').observe('シューズが飛んだ')
        self.assertEqual(substitution.edit_count, 1)
        self.assertTrue(substitution.terminal_candidate)

    def test_prefix_only_and_background_lead_in_do_not_complete(self):
        partial = TargetMatcher('今日は晴れです')
        partial.observe('今日は')
        self.assertFalse(partial.observe('今日は').terminal_candidate)
        self.assertEqual(partial.observe('今日は').tail_confidence, 0)
        noise = TargetMatcher('おはようございます')
        first = noise.observe('別の台詞ですおはようございます')
        self.assertFalse(first.terminal_candidate)
        self.assertLess(first.onset_score, .8)

    def test_short_ambiguous_voice_is_not_treated_as_proven_end(self):
        short = TargetMatcher('はい')
        short.observe('はい')
        self.assertFalse(short.observe('はい').terminal_candidate)
        self.assertEqual(short.observe('はい').tail_confidence, 0)

    def test_kana_kanji_difference_not_promoted_to_certain_end(self):
        matcher = TargetMatcher('東京へ行く')
        progress = matcher.observe('とうきょうへいく')
        self.assertLess(progress.coverage, 1)
        self.assertEqual(phonetic_key('「……」'), '')
        # Unlike fabricated fixtures, a phonetic mismatch is inconclusive;
        # the explicit option permits time-based continuation at the deadline.
        p = Profile('p', 'P', advance_mode=AdvanceMode.AUDIO,
                    uncertain_text_fallback=True,
                    hooks=[HookSelection('X', 1, 2)], body_sources=['X|1|2'])
        ctrl = Controller(lambda *_: None, lambda *_: True)
        ctrl.begin(p, Target(1, 1), 'host', 1)
        from types import SimpleNamespace
        token = ctrl.observe(generation='host', connection=1, sequence=1,
                             source=SimpleNamespace(hook_code='X', context=1, context2=2),
                             text='東京へ行く', pid=1)
        self.assertTrue(ctrl.presentation_event(token, 'fallback_elapsed'))
        self.assertEqual(ctrl.presentation_reason, 'uncertain_text_fallback')


if __name__ == '__main__':
    unittest.main()
