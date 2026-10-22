"""Validation-driver contract tests; these never claim to run a Windows game."""
from pathlib import Path
import json
import unittest
from types import SimpleNamespace

from validation.real_game.gui_session import (
    CALIBRATION, SessionDriver, calibration_candidates, choose_calibration_source,
    load_session, select_game_window,
)


class LiveGuiSessionContract(unittest.TestCase):
    def test_selects_only_real_observed_hook_output(self):
        a = SimpleNamespace(identity='a')
        b = SimpleNamespace(identity='b')
        sources = {'a': a, 'b': b}
        samples = {'a': ['別の台詞', CALIBRATION], 'b': ['無関係']}
        self.assertEqual(calibration_candidates(sources, samples, set()), [a])
        self.assertEqual(calibration_candidates(sources, samples, {'a'}), [])
        self.assertEqual(calibration_candidates(sources, {'a': [], 'b': []}, set()), [])
        self.assertEqual(calibration_candidates(
            sources, {'a': [CALIBRATION], 'b': [CALIBRATION]}, set()), [a, b])

    def test_multiple_actual_calibration_hooks_use_deterministic_real_source(self):
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

    def test_calibration_recipe_is_committed_through_application_owner(self):
        source = SimpleNamespace(identity='hook-a', address=91)
        candidate = SimpleNamespace(identity='hook-a', address=91, ambiguous=False,
                                    history=(CALIBRATION,))
        calls = []
        product = SimpleNamespace(
            capture_view=lambda: SimpleNamespace(candidates=(candidate,)),
            capture_source=lambda identity, address, require_unique=False: source,
            set_capture_extraction_preset=lambda *args: calls.append(('preset', args)),
            set_capture_field_source=lambda *args: calls.append(('field', args)),
            commit_capture=lambda: SimpleNamespace(body_sources=['hook-a']),
            runtime_state=lambda: SimpleNamespace(profile=SimpleNamespace(id='p')),
        )
        window = SimpleNamespace(application=product)
        driver = SessionDriver(window, SimpleNamespace(),
                               {'timeout_seconds': 30, 'phase': 'reading'})
        self.assertTrue(driver._select_calibration_source())
        self.assertEqual(calls, [
            ('preset', ('hook-a', 91, 'body')),
            ('field', ('body', 'hook-a', 91)),
            ('field', ('speaker', None)),
        ])

    def test_focus_activation_uses_gui_authorization_action_and_application_state(self):
        clock = [0.0]
        runtime = SimpleNamespace(authorized=False)
        product = SimpleNamespace(runtime_state=lambda: runtime)
        actions = []
        def permit():
            actions.append('permit')
            runtime.authorized = True
        window = SimpleNamespace(application=product, permit=permit)
        driver = SessionDriver(window, SimpleNamespace(),
                               {'timeout_seconds': 30, 'phase': 'native'},
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
        self.assertEqual(actions, ['permit', '.monitoring'])

    def test_actual_product_handoff_ends_run_without_fake_game_input(self):
        actions = []
        runtime = SimpleNamespace(token=SimpleNamespace(session=1, line=1),
                                  pause_reason='native_inconclusive_manual_handoff')
        product = SimpleNamespace(runtime_state=lambda: runtime)
        window = SimpleNamespace(application=product, close=lambda: actions.append('close'))
        qt_app = SimpleNamespace(quit=lambda: actions.append('quit'))
        driver = SessionDriver(window, qt_app, {'timeout_seconds': 30, 'phase': 'native',
                                                'dialogues': 3}, clock=lambda: 1)
        driver.state = 'running'
        driver.tick()
        self.assertEqual(actions, ['close', 'quit'])

    def test_failure_diagnostics_read_application_state_not_retired_gui_state(self):
        recorded = []
        closed = []
        source = SimpleNamespace(identity='hook-a')
        candidate = SimpleNamespace(identity='hook-a', address=7, ambiguous=False,
                                    history=('unrelated text',))
        runtime = SimpleNamespace(session=4, token=None, pause_reason=None)
        product = SimpleNamespace(
            capture_view=lambda: SimpleNamespace(candidates=(candidate,)),
            capture_source=lambda identity, address, require_unique=False: source,
            hook_connection_state=lambda: SimpleNamespace(connected=True),
            hook_host_running=True,
            runtime_state=lambda: runtime,
        )
        window = SimpleNamespace(
            application=product,
            validation_trace=SimpleNamespace(emit=lambda name, **kwargs: recorded.append((name, kwargs))),
            close=lambda: closed.append('window'))
        qt_app = SimpleNamespace(exit=lambda code: closed.append(code))
        driver = SessionDriver(window, qt_app, {'timeout_seconds': 30, 'phase': 'native'})
        driver.state = 'calibration'
        driver.fail('finite live-run timeout; no manual game input substituted')
        self.assertEqual(closed, ['window', 3])
        name, fields = recorded[0]
        self.assertEqual(name, 'validation_driver_failure')
        self.assertEqual(fields['stage'], 'calibration')
        self.assertEqual(fields['candidate_hooks'], 1)
        self.assertEqual(fields['sampled_hooks'], 1)
        self.assertEqual(fields['calibration_matches'], 0)
        self.assertTrue(fields['luna_connected'])
        self.assertTrue(fields['luna_host_alive'])

    def test_driver_has_no_retired_gui_runtime_ownership_dependencies(self):
        source = Path('validation/real_game/gui_session.py').read_text(encoding='utf-8')
        retired = (
            'self.w.pid', 'self.w.hwnd', 'self.w.controller', 'self.w.sources',
            'self.w.source_samples', 'self.w.ambiguous_sources', 'self.w.host_generation',
            'self.w.connection_generation', 'self.w.hook_host', 'self.w.native_audio',
            'self.w.native_audio_setup_error', 'self.w.begin_monitor', 'self.w.select_hook',
            'self.w.save_profile', 'self.w.inherit_controls',
        )
        for expression in retired:
            self.assertNotIn(expression, source)
        for api in (
            'self.product.select_primary_target(', 'self.product.activate_selected_target()',
            'self.product.hook_connection_state()', 'self.product.capture_view()',
            'self.product.set_capture_field_source(', 'self.product.commit_capture()',
            'self.product.runtime_state()', 'self.w.permit()',
        ):
            self.assertIn(api, source)

    def test_requires_bound_game_target_and_finite_timeout(self):
        valid = dict(gate='session-gate', executable='validation-game.exe',
                     phase='native', timeout_seconds=120)
        self.assertEqual(load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(valid)})['phase'], 'native')
        for phase in ('hook', 'reading', 'tts'):
            options = dict(valid, phase=phase)
            self.assertEqual(load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(options)})['phase'], phase)
        for key in valid:
            missing = dict(valid)
            del missing[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(missing)})
        with self.assertRaises(ValueError):
            load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(dict(valid, timeout_seconds=0))})
        with self.assertRaises(ValueError):
            load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(dict(valid, phase='simulated'))})


if __name__ == '__main__':
    unittest.main()
