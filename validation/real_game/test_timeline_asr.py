"""Timing suite registration and evidence shape, not fake ASR quality claims."""
import unittest

from validation.real_game.timeline_asr import recorded_cases


class TimelineSources(unittest.TestCase):
    def test_timeline_covers_pinned_normal_and_whisper_recordings(self):
        rows = list(recorded_cases('corpus'))
        self.assertEqual(len(rows), 14)
        self.assertEqual({row[3] for row in rows}, {'recorded_voice'})
        self.assertEqual({row[0].split('-')[0] for row in rows}, {'normal', 'whisper'})
        self.assertTrue(all(len(row[2]) > 0 and row[1].strip() for row in rows))

    def test_timeline_stress_is_explicitly_observational(self):
        rows = list(recorded_cases('stress'))
        self.assertEqual(len(rows), 8)
        self.assertIn('nonlexical', {row[3] for row in rows})
        self.assertIn('unverified_same_text', {row[3] for row in rows})
        self.assertTrue(all(len(row[2]) > 0 for row in rows))


if __name__ == '__main__':
    unittest.main()
