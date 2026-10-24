"""Native observations, monotonic knowledge and Controller policy stay separate."""
import unittest

from autoadv.audio_knowledge import (
    NativeAudioObservation, NativeVoiceKnowledge, alignment_associates_target,
    alignment_reaches_target_tail, reduce_native_voice_knowledge,
)
from autoadv.audio_match import TargetAlignment, TargetMatcher, surface_key
from autoadv.g2p import PronunciationCandidate
from autoadv.conversation import Controller, Target
from autoadv.model import Profile, AdvanceMode, HookSelection


class IdentityG2P:
    def candidates(self, text, *, max_candidates=4):
        units = tuple(surface_key(text))
        return (PronunciationCandidate(units),) if units and max_candidates else ()


IDENTITY_G2P = IdentityG2P()


def observation(alignment=TargetAlignment(), **kwargs):
    return NativeAudioObservation(kind=kwargs.pop('kind', 'asr'), alignment=alignment, **kwargs)


class ASRCues(unittest.TestCase):
    def test_corrected_partial_transcript_changes_raw_alignment(self):
        matcher = TargetMatcher('おはようございます', g2p=IDENTITY_G2P)
        first = matcher.align('おはおう')
        second = matcher.align('おはようござ')
        third = matcher.align('おはようございます')
        self.assertLess(first.phonetic_matches, second.phonetic_matches)
        self.assertLess(second.phonetic_matches, third.phonetic_matches)
        self.assertEqual(third.phonetic_prefix_units, third.phonetic_target_units)

    def test_single_omission_and_substitution_remain_raw_alignment_measurements(self):
        deletion = TargetMatcher('えっ嘘でしょ', g2p=IDENTITY_G2P).align('え嘘でしょ')
        self.assertEqual(deletion.phonetic_matches, 5)
        self.assertEqual(deletion.phonetic_edits, 1)
        substitution = TargetMatcher('ヒューズが飛んだ', g2p=IDENTITY_G2P).align('シューズが飛んだ')
        self.assertEqual(substitution.phonetic_edits, 1)

    def test_association_does_not_claim_target_end(self):
        alignment = TargetMatcher('おはようございます', g2p=IDENTITY_G2P).align('おはようございます')
        knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observation(alignment))
        self.assertTrue(knowledge.target_associated)
        self.assertFalse(knowledge.target_end_confirmed)

    def test_successor_token_start_is_not_an_acoustic_endpoint(self):
        alignment = TargetMatcher('おはようございます', g2p=IDENTITY_G2P).align('おはようございます')
        knowledge = reduce_native_voice_knowledge(
            NativeVoiceKnowledge(),
            observation(alignment, target_last_token_start_ms=800,
                        target_successor_token_start_ms=900))
        self.assertTrue(knowledge.target_associated)
        self.assertFalse(knowledge.target_end_confirmed)

    def test_vad_and_render_observations_do_not_manufacture_target_end(self):
        alignment = TargetMatcher('abcdefgh', g2p=IDENTITY_G2P).align('abcdefgh')
        first = reduce_native_voice_knowledge(NativeVoiceKnowledge(), observation(alignment))
        for item in (
            observation(kind='transport', pcm_last_frame_ms=1000, heartbeat_at_ms=1400),
            observation(vad_end_ms=1000, vad_tail_ms=400),
        ):
            with self.subTest(kind=item.kind):
                knowledge = reduce_native_voice_knowledge(first, item)
                self.assertTrue(knowledge.target_associated)
                self.assertFalse(knowledge.target_end_confirmed)

    def test_direct_target_tail_end_forms_end_knowledge_with_provenance(self):
        alignment = TargetMatcher('abcdefgh', g2p=IDENTITY_G2P).align('abcdefgh')
        item = observation(
            alignment,
            target_last_token_start_ms=800,
            target_acoustic_end_ms=940,
            target_acoustic_end_source='asr_token_duration')
        knowledge = reduce_native_voice_knowledge(NativeVoiceKnowledge(), item)
        self.assertTrue(alignment_reaches_target_tail(alignment))
        self.assertTrue(knowledge.target_end_confirmed)
        self.assertEqual(knowledge.end.estimated_end_ms, 940)
        self.assertEqual(knowledge.end.source, 'asr_token_duration')
        self.assertIs(knowledge.end.observation, item)

    def test_direct_end_does_not_excuse_unobserved_target_tail(self):
        alignment = TargetMatcher('abcdefgh', g2p=IDENTITY_G2P).align('abcdef')
        knowledge = reduce_native_voice_knowledge(
            NativeVoiceKnowledge(),
            observation(alignment, target_acoustic_end_ms=900,
                        target_acoustic_end_source='asr_token_duration'))
        self.assertTrue(alignment_associates_target(alignment))
        self.assertFalse(alignment_reaches_target_tail(alignment))
        self.assertFalse(knowledge.target_end_confirmed)

    def test_asr_regression_does_not_erase_prior_association(self):
        matcher = TargetMatcher('abcdefgh', g2p=IDENTITY_G2P)
        strong = observation(matcher.align('abcdef'))
        weak = observation(matcher.align('ab'))
        first = reduce_native_voice_knowledge(NativeVoiceKnowledge(), strong)
        second = reduce_native_voice_knowledge(first, weak)
        self.assertTrue(second.target_associated)
        self.assertIs(second.association.observation, strong)

    def test_unconfigured_reading_does_not_match_by_surface_fallback(self):
        matcher = TargetMatcher('東京へ行く', g2p=IDENTITY_G2P)
        alignment = matcher.align('とうきょうへいく')
        self.assertLess(alignment.phonetic_coverage, 1)
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
