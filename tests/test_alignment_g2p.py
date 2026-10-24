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

    def test_unknown_marking_preserves_usable_phones_or_surface_identity(self):
        cases = (
            ('固有名詞', ['unk'], 0, None, PronunciationCandidate(('unk:固有名詞',), 0, 4)),
            ('京', ['k', 'y', 'o', 'o'], 2, True, PronunciationCandidate(('k', 'y', 'o', 'o'), 0, 2)),
        )
        for text, phones, mora_count, is_unknown, expected in cases:
            with self.subTest(text=text):
                word = SimpleNamespace(phonemes=phones, word=text, mora_count=mora_count)
                if is_unknown is not None:
                    word.is_unknown = is_unknown
                engine = SimpleNamespace(g2p_candidates_detailed=lambda _text, word=word: SimpleNamespace(
                    candidates=[SimpleNamespace(words=[word])]))
                self.assertEqual(HaqumeiG2P(engine).candidates(text), (expected,))

    def test_punctuation_pause_is_not_a_required_target_phone(self):
        candidate = SimpleNamespace(words=[
            SimpleNamespace(phonemes=['e'], word='え', mora_count=1),
            SimpleNamespace(phonemes=['pau'], word='。', mora_count=0),
        ])
        engine = SimpleNamespace(
            g2p_candidates_detailed=lambda _: SimpleNamespace(candidates=[candidate]))
        self.assertEqual(HaqumeiG2P(engine).candidates('え。')[0].phonemes, ('e',))

    def test_word_span_projection_uses_canonical_coordinates_and_fails_closed(self):
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

        mismatched = SimpleNamespace(g2p_mapping_detailed=lambda _: [
            SimpleNamespace(word='京', char_span=(0, 1)),
        ])
        with self.assertRaisesRegex(ValueError, 'cannot be projected'):
            HaqumeiG2P(mismatched).word_spans('東京')

    def test_reading_unit_count_is_defined_by_mora_bearing_or_unknown_surface_units(self):
        cases = (
            (('g', 'a', 'cl', 'k', 'o', 'o', 'N'), 5),
            (('ky', 'o', 'o'), 2),
            (('pau', 'sil', 'k'), 0),
            (('unk:固有名詞',), 4),
            (('unk:abc12',), 5),
        )
        for phones, expected in cases:
            with self.subTest(phones=phones):
                self.assertEqual(count_reading_units(phones), expected)

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
    def test_detailed_mora_metadata_and_unknown_surface_both_produce_nonzero_timing(self):
        cases = (
            ('音', ['a', 'i', 'u'], 2, False, ('a', 'i', 'u'), 2),
            ('未知語', ['unk'], 0, True, ('unk:未知語',), 3),
        )
        for text, phones, mora_count, is_unknown, expected_phones, expected_units in cases:
            with self.subTest(text=text):
                detail = SimpleNamespace(phonemes=phones, word=text, mora_count=mora_count,
                                         is_unknown=is_unknown)
                engine = SimpleNamespace(g2p_candidates_detailed=lambda _text, detail=detail: SimpleNamespace(
                    candidates=[SimpleNamespace(words=[detail])]))
                candidates = HaqumeiG2P(engine).candidates(text, max_candidates=1)
                self.assertEqual(candidates[0].phonemes, expected_phones)
                self.assertEqual(reading_units_from_candidates(text, candidates), expected_units)
