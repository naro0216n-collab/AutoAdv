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


class RuleG2P:
    """Small deterministic pronunciation oracle for matcher contract tests."""
    _kata = str.maketrans(
        'アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン',
        'あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをん')

    def candidates(self, text, *, max_candidates=4):
        normalized = text.translate(self._kata).replace('嘘', 'うそ')
        phones = tuple(c for c in normalized if c.isalnum() or '\u3040' <= c <= '\u309f')
        return (PronunciationCandidate(phones),) if phones and max_candidates else ()


class HaqumeiAdapterTests(unittest.TestCase):
    def test_adapter_keeps_primary_and_bounded_reading_alternatives(self):
        primary = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['k', 'a', 'r', 'e']),
            SimpleNamespace(phonemes=['pau']),
        ])
        alternative = SimpleNamespace(words=[SimpleNamespace(phonemes=['a', 'n', 'o'])])
        engine = SimpleNamespace(
            g2p_candidates=lambda _: SimpleNamespace(candidates=[primary, alternative]))
        got = HaqumeiG2P(engine).candidates('彼の', max_candidates=2)
        self.assertEqual(got, (
            PronunciationCandidate(('k', 'a', 'r', 'e'), 0),
            PronunciationCandidate(('a', 'n', 'o'), 1),
        ))

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
        self.assertLess(progress.coverage, .5)
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
        self.assertLess(progress.completion_score, 1)

    def test_trailing_background_is_excluded_by_asr_token_endpoint(self):
        g2p = RuleG2P()
        tokens = tuple('えっうそでしょ背景音')
        matcher = TargetMatcher('えっ嘘でしょ', g2p=g2p)
        progress = matcher.observe(''.join(tokens), tokens=tokens)
        self.assertTrue(progress.terminal_candidate)
        self.assertEqual(progress.observed_token_count, len('えっうそでしょ'))
        self.assertGreater(progress.phonetic_coverage, .9)

    def test_unrelated_lead_in_remains_contradictory(self):
        g2p = RuleG2P()
        tokens = tuple('別のえっうそでしょ')
        progress = TargetMatcher('えっ嘘でしょ', g2p=g2p).observe(''.join(tokens), tokens=tokens)
        self.assertFalse(progress.terminal_candidate)
        self.assertGreater(progress.contradiction_score, .15)



    def test_haqumei_unknown_reading_keeps_surface_identity(self):
        class Result:
            def __init__(self, word):
                self.candidates = [SimpleNamespace(words=[
                    SimpleNamespace(word=word, phonemes=['unk'], is_unknown=True)
                ])]
        class Engine:
            def g2p_candidates(self, text):
                return Result(text)
        backend = HaqumeiG2P(engine=Engine())
        a = backend.candidates('霧羅')[0].phonemes
        b = backend.candidates('綺良')[0].phonemes
        self.assertNotEqual(a, b)
        self.assertEqual(a, ('unk:霧羅',))

if __name__ == '__main__':
    unittest.main()
