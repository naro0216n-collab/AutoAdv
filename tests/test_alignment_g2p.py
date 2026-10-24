import importlib.util
import unittest
from types import SimpleNamespace

from autoadv.g2p import (HaqumeiG2P, PronunciationCandidate, count_reading_units,
                         reading_units_from_candidates)


class HaqumeiAdapterTests(unittest.TestCase):
    def test_adapter_keeps_primary_and_bounded_reading_alternatives(self):
        primary = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['k', 'a', 'r', 'e'], word='彼', mora_count=2),
            SimpleNamespace(phonemes=['pau'], word='。', mora_count=0),
        ])
        alternative = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['a', 'n', 'o'], word='彼の', mora_count=3)])
        engine = SimpleNamespace(
            g2p_candidates_detailed=lambda _: SimpleNamespace(candidates=[primary, alternative]))
        got = HaqumeiG2P(engine).candidates('彼の', max_candidates=2)
        self.assertEqual(got, (
            PronunciationCandidate(('k', 'a', 'r', 'e'), 0, 2),
            PronunciationCandidate(('a', 'n', 'o'), 1, 3),
        ))

    def test_unknown_word_is_not_collapsed_to_a_generic_phone(self):
        unknown = SimpleNamespace(words=[SimpleNamespace(phonemes=['unk'], word='固有名詞', mora_count=0)])
        engine = SimpleNamespace(
            g2p_candidates_detailed=lambda _: SimpleNamespace(candidates=[unknown]))
        got = HaqumeiG2P(engine).candidates('固有名詞')
        self.assertEqual(got, (PronunciationCandidate(('unk:固有名詞',), 0, 4),))

    def test_usable_pronunciation_is_kept_even_if_engine_marks_word_unknown(self):
        word = SimpleNamespace(phonemes=['k', 'y', 'o', 'o'], word='京', is_unknown=True, mora_count=2)
        engine = SimpleNamespace(
            g2p_candidates_detailed=lambda _: SimpleNamespace(
                candidates=[SimpleNamespace(words=[word])]))
        self.assertEqual(
            HaqumeiG2P(engine).candidates('京'),
            (PronunciationCandidate(('k', 'y', 'o', 'o'), 0, 2),),
        )

    def test_punctuation_pause_is_not_a_required_target_phone(self):
        candidate = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['e'], word='え', mora_count=1),
            SimpleNamespace(phonemes=['pau'], word='。', mora_count=0),
        ])
        engine = SimpleNamespace(
            g2p_candidates_detailed=lambda _: SimpleNamespace(candidates=[candidate]))
        self.assertEqual(HaqumeiG2P(engine).candidates('え。')[0].phonemes, ('e',))

    def test_word_spans_use_verified_g2p_canonical_coordinates(self):
        seen = []
        words = [
            SimpleNamespace(word='パ', char_span=(0, 1)),
            SimpleNamespace(word='京', char_span=(1, 2)),
        ]
        engine = SimpleNamespace(g2p_mapping_detailed=lambda text: seen.append(text) or words)
        spans = HaqumeiG2P(engine).word_spans('ﾊﾟ京')
        self.assertEqual(seen, ['パ京'])
        self.assertEqual([(span.start, span.end, span.surface) for span in spans],
                         [(0, 1, 'パ'), (1, 2, '京')])

    def test_word_span_projection_fails_closed_when_haqumei_coordinates_do_not_match(self):
        engine = SimpleNamespace(g2p_mapping_detailed=lambda _: [
            SimpleNamespace(word='京', char_span=(0, 1)),
        ])
        with self.assertRaisesRegex(ValueError, 'cannot be projected'):
            HaqumeiG2P(engine).word_spans('東京')


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
            reading_units_from_candidates('東京', EmptyBackend().candidates('東京', max_candidates=1))
        self.assertEqual(reading_units_from_candidates('……!?', ()), 0)

    def test_primary_timing_ignores_bounded_alternatives(self):
        class Backend:
            def candidates(self, text, *, max_candidates=4):
                self.max_candidates = max_candidates
                return (PronunciationCandidate(('ky', 'o', 'o'), 0),
                        PronunciationCandidate(('a', 'i', 'u', 'e', 'o'), 1))
        backend = Backend()
        candidates = backend.candidates('今日', max_candidates=1)
        units = reading_units_from_candidates('今日', candidates)
        self.assertEqual((candidates[0].phonemes, units), (('ky', 'o', 'o'), 2))
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

class DetailedMoraContract(unittest.TestCase):
    def test_known_mora_count_is_used_instead_of_phone_recount(self):
        detail = SimpleNamespace(phonemes=['a', 'i', 'u'], word='音', mora_count=2,
                                 is_unknown=False)
        engine = SimpleNamespace(g2p_candidates_detailed=lambda _: SimpleNamespace(
            candidates=[SimpleNamespace(words=[detail])]))
        backend = HaqumeiG2P(engine)
        candidates = backend.candidates('音', max_candidates=1)
        assert candidates[0].phonemes == ('a', 'i', 'u')
        assert reading_units_from_candidates('音', candidates) == 2

    def test_unknown_is_not_silently_counted_as_zero(self):
        detail = SimpleNamespace(phonemes=['unk'], word='未知語', mora_count=0,
                                 is_unknown=True)
        engine = SimpleNamespace(g2p_candidates_detailed=lambda _: SimpleNamespace(
            candidates=[SimpleNamespace(words=[detail])]))
        backend = HaqumeiG2P(engine)
        candidates = backend.candidates('未知語', max_candidates=1)
        assert candidates[0].phonemes == ('unk:未知語',)
        assert reading_units_from_candidates('未知語', candidates) == 3
