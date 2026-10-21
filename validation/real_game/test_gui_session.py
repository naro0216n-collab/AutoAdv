"""Launcher contract only: these tests never claim to run a Windows game."""
import json
import unittest
from types import SimpleNamespace

from validation.real_game.gui_session import CALIBRATION, calibration_candidates, load_session


class LiveGuiSessionContract(unittest.TestCase):
    def test_selects_only_unique_real_observed_hook_output(self):
        a = SimpleNamespace(identity='a')
        b = SimpleNamespace(identity='b')
        sources = {'a': a, 'b': b}
        samples = {'a': ['別の台詞', CALIBRATION], 'b': ['無関係']}
        self.assertEqual(calibration_candidates(sources, samples, set()), [a])
        self.assertEqual(calibration_candidates(sources, samples, {'a'}), [])
        self.assertEqual(calibration_candidates(sources, {'a': [], 'b': []}, set()), [])
        self.assertEqual(calibration_candidates(sources, {'a': [CALIBRATION], 'b': [CALIBRATION]}, set()), [a, b])

    def test_requires_bound_game_target_and_finite_timeout(self):
        valid = dict(oracle='game.jsonl', gate='session-gate', executable='renpy.exe',
                     phase='native', timeout_seconds=120)
        self.assertEqual(load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(valid)})['phase'], 'native')
        for key in valid:
            missing = dict(valid)
            del missing[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(missing)})
        invalid = dict(valid, timeout_seconds=0)
        with self.assertRaises(ValueError):
            load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(invalid)})
        invalid = dict(valid, phase='simulated')
        with self.assertRaises(ValueError):
            load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(invalid)})
