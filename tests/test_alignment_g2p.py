import importlib.util
import unittest
from types import SimpleNamespace

from autoadv.g2p import HaqumeiG2P, PronunciationCandidate


class HaqumeiAdapterTests(unittest.TestCase):
    def test_adapter_keeps_primary_and_bounded_reading_alternatives(self):
        primary = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['k', 'a', 'r', 'e'], word='彼'),
            SimpleNamespace(phonemes=['pau'], word='。'),
        ])
        alternative = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['a', 'n', 'o'], word='彼の')])
        engine = SimpleNamespace(
            g2p_candidates=lambda _: SimpleNamespace(candidates=[primary, alternative]))
        got = HaqumeiG2P(engine).candidates('彼の', max_candidates=2)
        self.assertEqual(got, (
            PronunciationCandidate(('k', 'a', 'r', 'e'), 0),
            PronunciationCandidate(('a', 'n', 'o'), 1),
        ))

    def test_unknown_word_is_not_collapsed_to_a_generic_phone(self):
        unknown = SimpleNamespace(words=[SimpleNamespace(phonemes=['unk'], word='固有名詞')])
        engine = SimpleNamespace(
            g2p_candidates=lambda _: SimpleNamespace(candidates=[unknown]))
        got = HaqumeiG2P(engine).candidates('固有名詞')
        self.assertEqual(got, (PronunciationCandidate(('unk:固有名詞',), 0),))

    def test_usable_pronunciation_is_kept_even_if_engine_marks_word_unknown(self):
        word = SimpleNamespace(phonemes=['k', 'y', 'o', 'o'], word='京', is_unknown=True)
        engine = SimpleNamespace(
            g2p_candidates=lambda _: SimpleNamespace(
                candidates=[SimpleNamespace(words=[word])]))
        self.assertEqual(
            HaqumeiG2P(engine).candidates('京'),
            (PronunciationCandidate(('k', 'y', 'o', 'o'), 0),),
        )

    def test_punctuation_pause_is_not_a_required_target_phone(self):
        candidate = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['e'], word='え'),
            SimpleNamespace(phonemes=['pau'], word='。'),
        ])
        engine = SimpleNamespace(
            g2p_candidates=lambda _: SimpleNamespace(candidates=[candidate]))
        self.assertEqual(HaqumeiG2P(engine).candidates('え。')[0].phonemes, ('e',))

    @unittest.skipUnless(importlib.util.find_spec('haqumei'), 'haqumei native extra not installed')
    def test_installed_haqumei_matches_kanji_and_hiragana_pronunciation(self):
        g2p = HaqumeiG2P()
        kanji = g2p.candidates('嘘', max_candidates=4)
        hiragana = g2p.candidates('うそ', max_candidates=4)
        self.assertTrue(kanji)
        self.assertTrue(hiragana)
        self.assertEqual(kanji[0].phonemes, hiragana[0].phonemes)


if __name__ == '__main__':
    unittest.main()
