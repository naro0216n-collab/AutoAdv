import importlib.util
import unittest
from types import SimpleNamespace

from autoadv.g2p import (HaqumeiG2P, PronunciationCandidate, count_reading_units,
                         primary_reading_units)


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


    def test_mora_like_timing_units_count_nuclei_nasal_and_geminate(self):
        self.assertEqual(
            count_reading_units(('g', 'a', 'cl', 'k', 'o', 'o', 'N')),
            5,
        )
        self.assertEqual(count_reading_units(('ky', 'o', 'o')), 2)
        self.assertEqual(count_reading_units(('pau', 'sil', 'k')), 0)

    def test_unknown_surface_remains_a_timing_quantity(self):
        self.assertEqual(count_reading_units(('unk:固有名詞',)), 4)
        self.assertEqual(count_reading_units(('unk:abc12',)), 5)

    def test_readable_text_without_primary_pronunciation_fails_closed(self):
        class EmptyBackend:
            def candidates(self, text, *, max_candidates=4):
                return ()
        with self.assertRaisesRegex(ValueError, 'no primary pronunciation'):
            primary_reading_units('東京', EmptyBackend())
        self.assertEqual(primary_reading_units('……!?', EmptyBackend()), ((), 0))

    def test_primary_timing_ignores_bounded_alternatives(self):
        class Backend:
            def candidates(self, text, *, max_candidates=4):
                self.max_candidates = max_candidates
                return (PronunciationCandidate(('ky', 'o', 'o'), 0),
                        PronunciationCandidate(('a', 'i', 'u', 'e', 'o'), 1))
        backend = Backend()
        phones, units = primary_reading_units('今日', backend)
        self.assertEqual((phones, units), (('ky', 'o', 'o'), 2))
        self.assertEqual(backend.max_candidates, 1)

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
