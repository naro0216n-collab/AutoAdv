"""Alignment observations remain separate from product voice assessment."""
import unittest

from autoadv.audio_evidence import (VoiceAssessmentPolicy, VoiceEvidence, VoiceState,
                                    assess_voice)
from autoadv.audio_match import TargetMatcher, surface_key
from autoadv.conversation import Controller, Target
from autoadv.model import Profile, AdvanceMode, HookSelection


def evidence(alignment, *, audio_ms=1000, capture_end_ms=1000,
             target_last_token_ms=None, target_boundary_ms=None,
             post_target_pcm_ms=0, vad_tail_ms=0, stable=1, progress_delta=None):
    return VoiceEvidence(
        alignment=alignment, audio_ms=audio_ms, capture_end_ms=capture_end_ms,
        target_last_token_ms=target_last_token_ms,
        target_boundary_ms=target_boundary_ms,
        post_target_pcm_ms=post_target_pcm_ms,
        vad_active=False, vad_contains_speech=False, vad_onset_ms=None, vad_tail_ms=vad_tail_ms,
        vad_end_ms=(capture_end_ms - vad_tail_ms if vad_tail_ms else None),
        stable_alignment_observations=stable, phonetic_progress_delta=progress_delta)


class ASRCues(unittest.TestCase):
    def test_corrected_partial_transcript_changes_alignment_without_hidden_history(self):
        matcher = TargetMatcher('おはようございます', g2p=None)
        first = matcher.align('おはおう')
        second = matcher.align('おはようござ')
        third = matcher.align('おはようございます')
        fourth = matcher.align('おはようございます')
        self.assertLess(first.coverage, second.coverage)
        self.assertLess(second.coverage, third.coverage)
        self.assertEqual(third, fourth)
        self.assertGreaterEqual(third.coverage, .9)

    def test_single_omission_and_substitution_remain_raw_alignment_measurements(self):
        deletion = TargetMatcher('えっ嘘でしょ', g2p=None).align('え嘘でしょ')
        self.assertEqual(deletion.matched_chars, 5)
        self.assertEqual(deletion.edit_count, 1)
        substitution = TargetMatcher('ヒューズが飛んだ', g2p=None).align('シューズが飛んだ')
        self.assertEqual(substitution.edit_count, 1)

    def test_prefix_only_and_background_lead_in_are_not_relabelled_by_matcher(self):
        partial = TargetMatcher('今日は晴れです', g2p=None).align('今日は')
        self.assertLess(partial.coverage, .8)
        noise = TargetMatcher('おはようございます', g2p=None).align('別の台詞ですおはようございます')
        self.assertLess(noise.onset_score, .8)
        self.assertGreater(noise.edit_count, 0)

    def test_short_alignment_without_endpoint_evidence_is_inconclusive(self):
        alignment = TargetMatcher('はい', g2p=None).align('はい')
        assessed = assess_voice(evidence(alignment))
        self.assertEqual(assessed.state, VoiceState.INCONCLUSIVE)

    def test_same_observation_can_be_reassessed_with_different_policy(self):
        alignment = TargetMatcher('おはようございます', g2p=None).align('おはようございます')
        observed = evidence(
            alignment, capture_end_ms=2000, target_last_token_ms=1200,
            target_boundary_ms=1900, post_target_pcm_ms=100)
        self.assertEqual(assess_voice(observed).state, VoiceState.INCONCLUSIVE)
        relaxed = VoiceAssessmentPolicy(endpoint_min=.30)
        self.assertEqual(assess_voice(observed, relaxed).state, VoiceState.ENDED)

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
