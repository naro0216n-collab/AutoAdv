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

    def test_kana_kanji_difference_not_promoted_to_certain_end(self):
        matcher = TargetMatcher('東京へ行く')
        progress = matcher.observe('とうきょうへいく')
        self.assertLess(progress.coverage, 1)
        self.assertEqual(phonetic_key('「……」'), '')
        # Unlike fabricated fixtures, a phonetic mismatch is inconclusive;
        # the explicit option permits time-based continuation at the deadline.
        p = Profile('p', 'P', advance_mode=AdvanceMode.AUDIO,
                    uncertain_text_fallback=True,
                    hooks=[HookSelection('X', 1, 2)])
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
