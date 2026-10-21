"""Launcher contract only: these tests never claim to run a Windows game."""
import json
import unittest
from types import SimpleNamespace

from validation.real_game.gui_session import CALIBRATION, calibration_candidates, load_session, select_game_window


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

    def test_console_window_is_not_the_renpy_game(self):
        console = SimpleNamespace(hwnd=1, title='Python console')
        game = SimpleNamespace(hwnd=2, title='RenPy validation game')
        classes = {1: 'ConsoleWindowClass', 2: 'SDL_app', 3: 'SDL_app', 4: 'PseudoConsoleWindow'}
        choose = lambda rows: select_game_window(rows, lambda hwnd: classes[hwnd])
        self.assertIs(choose([console, game]), game)
        pseudo = SimpleNamespace(hwnd=4, title='')
        self.assertIs(choose([console, pseudo, game]), game)
        self.assertIsNone(choose([console, pseudo]))
        with self.assertRaises(ValueError):
            choose([game, SimpleNamespace(hwnd=3, title='another RenPy window')])

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
