import importlib.util
import unittest
from types import SimpleNamespace

from autoadv.audio_match import TargetMatcher
from autoadv.g2p import HaqumeiG2P, PronunciationCandidate


class DictG2P:
    def __init__(self, values):
        self.values = values

    def candidates(self, text, *, max_candidates=4):
        return tuple(self.values[text][:max_candidates])


class HaqumeiAdapterTests(unittest.TestCase):
    def test_adapter_keeps_primary_and_bounded_reading_alternatives(self):
        primary = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['k', 'a', 'r', 'e']),
            SimpleNamespace(phonemes=['pau']),
        ])
        alternative = SimpleNamespace(words=[SimpleNamespace(phonemes=['a', 'n', 'o'])])
        engine = SimpleNamespace(
            g2p_candidates=lambda _: SimpleNamespace(candidates=[primary, alternative]),
            g2p=lambda _: ['unused'])
        got = HaqumeiG2P(engine).candidates('彼の', max_candidates=2)
        self.assertEqual(got, (
            PronunciationCandidate(('k', 'a', 'r', 'e'), 0),
            PronunciationCandidate(('a', 'n', 'o'), 1),
        ))

    def test_unknown_phoneme_does_not_become_shared_pronunciation_evidence(self):
        unknown = SimpleNamespace(words=[SimpleNamespace(phonemes=['unk'])])
        engine = SimpleNamespace(
            g2p_candidates=lambda _: SimpleNamespace(candidates=[unknown]))
        self.assertEqual(HaqumeiG2P(engine).candidates('未知語'), ())

    @unittest.skipUnless(importlib.util.find_spec('haqumei'), 'haqumei native extra not installed')
    def test_installed_haqumei_matches_kanji_and_hiragana_pronunciation(self):
        g2p = HaqumeiG2P()
        kanji = g2p.candidates('嘘', max_candidates=4)
        hiragana = g2p.candidates('うそ', max_candidates=4)
        self.assertTrue(kanji)
        self.assertTrue(hiragana)
        self.assertEqual(kanji[0].phonemes, hiragana[0].phonemes)


class PhoneticMatcherTests(unittest.TestCase):
    def test_kanji_hiragana_katakana_spelling_can_share_pronunciation(self):
        phones = ('t', 'o', 'o', 'k', 'y', 'o', 'o', 'e', 'i', 'k', 'u')
        g2p = DictG2P({
            '東京へ行く': [PronunciationCandidate(phones)],
            'とうきょうへいく': [PronunciationCandidate(phones)],
        })
        progress = TargetMatcher('東京へ行く', g2p=g2p).observe('とうきょうへいく')
        self.assertLess(progress.coverage, .5)  # surface spelling still differs
        self.assertEqual(progress.phonetic_coverage, 1)
        self.assertGreater(progress.attribution_score, .85)
        self.assertTrue(progress.terminal_candidate)

    def test_dictionary_alternative_handles_context_reading_mismatch_without_unbounded_onkun(self):
        shijou = ('sh', 'i', 'j', 'o', 'o', 'e', 'i', 'k', 'u')
        ichiba = ('i', 'ch', 'i', 'b', 'a', 'e', 'i', 'k', 'u')
        g2p = DictG2P({
            '市場へ行く': [PronunciationCandidate(shijou, 0), PronunciationCandidate(ichiba, 1)],
            'いちばへいく': [PronunciationCandidate(ichiba, 0)],
        })
        progress = TargetMatcher('市場へ行く', g2p=g2p).observe('いちばへいく')
        self.assertEqual(progress.reading_rank, 1)
        self.assertEqual(progress.phonetic_coverage, 1)
        self.assertTrue(progress.terminal_candidate)
        self.assertLess(progress.completion_score, 1)  # secondary reading is not free evidence

    def test_unrelated_lead_in_remains_contradictory_even_with_target_pronunciation(self):
        target = ('o', 'h', 'a', 'y', 'o', 'o')
        noisy = ('b', 'e', 't', 's', 'u', 'n', 'o') + target
        g2p = DictG2P({
            'おはよう': [PronunciationCandidate(target)],
            '別のおはよう': [PronunciationCandidate(noisy)],
        })
        progress = TargetMatcher('おはよう', g2p=g2p).observe('別のおはよう')
        self.assertFalse(progress.terminal_candidate)
        self.assertGreater(progress.contradiction_score, .2)

    def test_terminal_token_prefix_does_not_skip_unrelated_lead_in(self):
        matcher = TargetMatcher('えっ嘘でしょ')
        self.assertIsNone(matcher.terminal_token_prefix(tuple('別のえっ嘘でしょ')))

    def test_terminal_token_prefix_can_leave_trailing_background_outside_target(self):
        matcher = TargetMatcher('えっ嘘でしょ')
        tokens = tuple('えっ嘘でしょまだ話す')
        found = matcher.terminal_token_prefix(tokens)
        self.assertIsNotNone(found)
        count, progress = found
        self.assertLess(count, len(tokens))
        self.assertTrue(progress.terminal_candidate)
        self.assertGreaterEqual(progress.completion_score, .78)


if __name__ == '__main__':
    unittest.main()
