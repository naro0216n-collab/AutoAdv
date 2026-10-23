from pathlib import Path
import unittest

from validation.real_game.case_matrix import real_audio_cases


class RealAudioCaseMatrixTests(unittest.TestCase):
    def test_all_foreground_native_cases_are_independent_matrix_entries(self):
        cases = real_audio_cases()
        self.assertEqual(len(cases), 16)
        self.assertEqual(cases[0].id, 'normal-001')
        self.assertEqual(cases[0].start_line, 6)
        self.assertEqual(cases[-1].id, 'normal-035')
        self.assertEqual(cases[-1].start_line, 25)
        ids = {case.id for case in cases}
        for required in ('normal-026-bgm', 'normal-028-bgm', 'whisper-001',
                         'normal-020-first', 'normal-020-second', 'normal-012'):
            self.assertIn(required, ids)

    def test_fixture_has_an_entry_label_for_every_matrix_case(self):
        source = Path('validation/real_game/renpy/game/script.rpy').read_text(encoding='utf-8')
        for case in real_audio_cases():
            label = 'autoadv_case_' + case.id.replace('-', '_')
            self.assertIn(f'label {label}:', source, case.id)


if __name__ == '__main__':
    unittest.main()
