"""Launcher contract only: these tests never claim to run a Windows game."""
import json
import unittest
from types import SimpleNamespace
from validation.real_game.gui_session import SessionDriver

from validation.real_game.gui_session import (CALIBRATION, calibration_candidates,
                                              choose_calibration_source, load_session, select_game_window)


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

    def test_multiple_actual_calibration_hooks_do_not_block_product_evaluation(self):
        # Duplicate *real* Hook outputs are routine. Select one through the
        # existing GUI; the product must still capture the next real dialogue.
        a = SimpleNamespace(identity='a', address=20)
        b = SimpleNamespace(identity='b', address=10)
        sources = {'a': a, 'b': b}
        samples = {'a': ['prefix ' + CALIBRATION], 'b': [CALIBRATION]}
        self.assertIs(choose_calibration_source(sources, samples, set()), b)
        samples['a'] = [CALIBRATION]
        self.assertIs(choose_calibration_source(sources, samples, set()), a)
        self.assertIs(choose_calibration_source(sources, samples, {'a'}), b)
        self.assertIsNone(choose_calibration_source(sources, samples, {'a', 'b'}))

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

    def test_focus_activation_does_not_fail_before_windows_updates_foreground(self):
        clock = [0.0]
        control = SimpleNamespace(authorized=False)
        actions = []
        window = SimpleNamespace(controller=control, permit=lambda: setattr(control, 'authorized', True))
        driver = SessionDriver(window, SimpleNamespace(), {'timeout_seconds': 30, 'phase': 'native'},
                               clock=lambda: clock[0])
        driver.state = 'activate'
        driver._focus_game = lambda: setattr(driver, 'focus_deadline', clock[0] + 3)
        driver._game_is_focused = lambda: clock[0] >= 0.3
        driver._release = actions.append
        driver.tick()
        self.assertEqual(driver.state, 'await_focus')
        clock[0] = 0.15
        driver.tick()
        self.assertEqual(driver.state, 'await_focus')
        clock[0] = 0.3
        driver.tick()
        self.assertEqual(driver.state, 'running')
        self.assertEqual(actions, ['.monitoring'])

    def test_actual_product_handoff_ends_run_without_fake_game_input(self):
        actions = []
        control = SimpleNamespace(token=SimpleNamespace(session=1, line=1),
                                  pause_reason='native_inconclusive_manual_handoff',
                                  line_no=1)
        window = SimpleNamespace(controller=control, close=lambda: actions.append('close'))
        app = SimpleNamespace(quit=lambda: actions.append('quit'))
        driver = SessionDriver(window, app, {'timeout_seconds': 30, 'phase': 'native',
                                             'dialogues': 3}, clock=lambda: 1)
        driver.state = 'running'
        driver.tick()
        self.assertEqual(actions, ['close', 'quit'])

    def test_requires_bound_game_target_and_finite_timeout(self):
        valid = dict(gate='session-gate', executable='renpy.exe',
                     phase='native', timeout_seconds=120)
        self.assertEqual(load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(valid)})['phase'], 'native')
        # Game-side Oracle is needed only when independently grading audio end;
        # it is not an AutoAdv GUI launcher dependency.
        for phase in ('hook', 'reading', 'tts'):
            self.assertEqual(load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(dict(valid, phase=phase))})['phase'], phase)
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
