"""Raw alignment observations and the minimal product voice assessment stay separate."""
import unittest

from autoadv.audio_evidence import VoiceEvidence, VoiceState, assess_voice
from autoadv.audio_match import TargetMatcher, surface_key
from autoadv.conversation import Controller, Target
from autoadv.model import Profile, AdvanceMode, HookSelection


def evidence(alignment, *, target_last_token_ms=None, target_boundary_ms=None,
             vad_tail_ms=0, target_progressed=False):
    return VoiceEvidence(
        alignment=alignment,
        target_last_token_ms=target_last_token_ms,
        target_boundary_ms=target_boundary_ms,
        vad_onset_ms=None,
        vad_tail_ms=vad_tail_ms,
        vad_end_ms=(1000 - vad_tail_ms if vad_tail_ms else None),
        target_progressed=target_progressed,
    )


class ASRCues(unittest.TestCase):
    def test_corrected_partial_transcript_changes_raw_alignment(self):
        matcher = TargetMatcher('おはようございます', g2p=None)
        first = matcher.align('おはおう')
        second = matcher.align('おはようござ')
        third = matcher.align('おはようございます')
        self.assertLess(first.phonetic_matches, second.phonetic_matches)
        self.assertLess(second.phonetic_matches, third.phonetic_matches)
        self.assertEqual(third.phonetic_prefix_units, third.phonetic_target_units)

    def test_single_omission_and_substitution_remain_raw_alignment_measurements(self):
        deletion = TargetMatcher('えっ嘘でしょ', g2p=None).align('え嘘でしょ')
        self.assertEqual(deletion.matched_chars, 5)
        self.assertEqual(deletion.edit_count, 1)
        substitution = TargetMatcher('ヒューズが飛んだ', g2p=None).align('シューズが飛んだ')
        self.assertEqual(substitution.edit_count, 1)

    def test_prefix_only_and_background_lead_in_remain_distinct_alignments(self):
        partial = TargetMatcher('今日は晴れです', g2p=None).align('今日は')
        self.assertLess(partial.phonetic_prefix_units, partial.phonetic_target_units)
        noise = TargetMatcher('おはようございます', g2p=None).align('別の台詞ですおはようございます')
        self.assertGreater(noise.phonetic_edit_count, 0)

    def test_complete_target_without_endpoint_is_inconclusive(self):
        alignment = TargetMatcher('はい', g2p=None).align('はい')
        assessed = assess_voice(evidence(alignment))
        self.assertEqual(assessed.state, VoiceState.INCONCLUSIVE)
        self.assertTrue(assessed.target_complete)

    def test_following_token_bounds_completed_target(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようございます')
        assessed = assess_voice(evidence(
            alignment, target_last_token_ms=800, target_boundary_ms=900))
        self.assertEqual(assessed.state, VoiceState.ENDED)
        self.assertEqual(assessed.estimated_end_ms, 900)

    def test_target_progress_without_endpoint_is_playing(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようござ')
        assessed = assess_voice(evidence(
            alignment, target_last_token_ms=800, target_progressed=True))
        self.assertEqual(assessed.state, VoiceState.PLAYING)

    def test_surface_only_fallback_does_not_invent_kanji_reading(self):
        matcher = TargetMatcher('東京へ行く', g2p=None)
        alignment = matcher.align('とうきょうへいく')
        self.assertLess(alignment.coverage, 1)
        self.assertEqual(surface_key('「……」'), '')
        p = Profile('p', 'P', advance_mode=AdvanceMode.AUDIO,
                    uncertain_text_fallback=True,
                    hooks=[HookSelection('X', 1, 2)], body_source='X|1|2')
        ctrl = Controller(lambda *_: None, lambda *_: True)
        ctrl.begin(p, Target(1, 1), 'host', 1)
        from types import SimpleNamespace
        token = ctrl.observe(generation='host', connection=1, sequence=1,
                             source=SimpleNamespace(hook_code='X', context=1, context2=2),
                             text='東京へ行く', pid=1)
        self.assertTrue(ctrl.presentation_event(token, 'audio_deadline_elapsed'))
        self.assertEqual(ctrl.presentation_reason, 'uncertain_text_fallback')


if __name__ == '__main__':
    unittest.main()
