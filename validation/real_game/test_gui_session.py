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
    def test_selects_only_product_previewed_calibration_body(self):
        exact = SimpleNamespace(
            identity='a', address=20, ambiguous=False, history=(CALIBRATION,),
            samples=(SimpleNamespace(raw=CALIBRATION, speaker='', body=CALIBRATION),))
        structured = SimpleNamespace(
            identity='b', address=10, ambiguous=False,
            history=('明日香\n' + CALIBRATION,),
            samples=(SimpleNamespace(raw='明日香\n' + CALIBRATION,
                                     speaker='明日香', body=CALIBRATION),))
        speaker_only = SimpleNamespace(
            identity='c', address=30, ambiguous=False, history=('明日香',),
            samples=(SimpleNamespace(raw='明日香', speaker='', body='明日香'),))
        noisy = SimpleNamespace(
            identity='d', address=35, ambiguous=False, history=('prefix ' + CALIBRATION,),
            samples=(SimpleNamespace(raw='prefix ' + CALIBRATION, speaker='',
                                     body='prefix ' + CALIBRATION),))
        ambiguous = SimpleNamespace(
            identity='e', address=40, ambiguous=True, history=(CALIBRATION,),
            samples=(SimpleNamespace(raw=CALIBRATION, speaker='', body=CALIBRATION),))
        self.assertEqual(calibration_candidates(
            (exact, structured, speaker_only, noisy, ambiguous)), [exact, structured])
        self.assertEqual(calibration_candidates((speaker_only, noisy, ambiguous)), [])

    def test_calibration_prefers_body_only_then_structured_product_preview(self):
        exact = SimpleNamespace(
            identity='z-body', address=20, ambiguous=False, history=(CALIBRATION,),
            samples=(SimpleNamespace(raw=CALIBRATION, speaker='', body=CALIBRATION),))
        structured = SimpleNamespace(
            identity='a-structured', address=10, ambiguous=False,
            history=('明日香\n' + CALIBRATION,),
            samples=(SimpleNamespace(raw='明日香\n' + CALIBRATION,
                                     speaker='明日香', body=CALIBRATION),))
        self.assertIs(choose_calibration_source((structured, exact)), exact)
        self.assertIs(choose_calibration_source((structured,)), structured)
        self.assertIsNone(choose_calibration_source(()))


    def test_fixture_calibration_uses_real_speaker_structure(self):
        script = Path('validation/real_game/renpy/game/script.rpy').read_text(encoding='utf-8')
        self.assertIn('renpy.say(a, "' + CALIBRATION + '", interact=False)', script)
        self.assertNotIn('renpy.say(None, "' + CALIBRATION + '", interact=False)', script)

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
        candidate = SimpleNamespace(
            identity='hook-a', address=91, ambiguous=False, history=(CALIBRATION,),
            samples=(SimpleNamespace(raw=CALIBRATION, speaker='', body=CALIBRATION),))
        calls = []
        product = SimpleNamespace(
            capture_view=lambda: SimpleNamespace(candidates=(candidate,)),
            capture_source=lambda identity, address, require_unique=False: source,
            set_capture_extraction_preset=lambda *args: calls.append(('preset', args)),
            set_capture_field_source=lambda *args: calls.append(('field', args)),
            commit_capture=lambda: SimpleNamespace(body_source='hook-a'),
            runtime_state=lambda: SimpleNamespace(profile=SimpleNamespace(id='p')),
        )
        window = SimpleNamespace(application=product)
        trace = SimpleNamespace(emit=lambda *args, **kwargs: None)
        driver = SessionDriver(window, SimpleNamespace(),
                               {'timeout_seconds': 30, 'phase': 'reading'}, trace)
        self.assertTrue(driver._select_calibration_source())
        self.assertEqual(calls, [
            ('preset', ('hook-a', 91, 'auto')),
            ('field', ('body', 'hook-a', 91)),
            ('field', ('speaker', None)),
        ])

    def test_activation_authorizes_without_stealing_game_focus(self):
        runtime = SimpleNamespace(authorized=False)
        product = SimpleNamespace(runtime_state=lambda: runtime)
        actions = []
        def permit():
            actions.append('permit')
            runtime.authorized = True
        window = SimpleNamespace(application=product, permit=permit)
        trace = SimpleNamespace(emit=lambda name, **fields:
                                actions.append(('trace', name, fields)))
        driver = SessionDriver(window, SimpleNamespace(),
                               {'timeout_seconds': 30, 'phase': 'accuracy', 'dialogues': 24,
                                'continue_collection': True}, trace,
                               clock=lambda: 0.0)
        driver.state = 'activate'
        driver._release = actions.append
        driver.tick()
        self.assertEqual(driver.state, 'running')
        self.assertEqual(actions[0][0:2], ('trace', 'validation_collection_target'))
        self.assertEqual(actions[1:], ['permit', '.monitoring'])

    def test_accuracy_configures_real_hook_and_managed_native_defaults_without_path_override(self):
        settings = []
        target = SimpleNamespace(pid=321, hwnd=654)
        profile = SimpleNamespace(
            ocr=SimpleNamespace(executable='', language='jpn', region=None, stable_samples=2),
            insert_pc_hooks=False, hook_codepage=932, hook_max_buffer=4096,
            hook_max_history=32,
        )
        product = SimpleNamespace(
            create_game=lambda *_: SimpleNamespace(id='g', default_profile='default'),
            select_profile=lambda *_: None,
            select_primary_target=lambda *_: None,
            set_profile_setting=lambda name, value: settings.append((name, value)),
            current_profile=lambda: profile,
            set_capture_environment=lambda **_: None,
            activate_selected_target=lambda: 'connecting',
        )
        driver = SessionDriver(
            SimpleNamespace(application=product), SimpleNamespace(),
            {'timeout_seconds': 30, 'phase': 'accuracy', 'fallback': True,
             'executable': 'validation-game.exe'},
            SimpleNamespace(emit=lambda *args, **kwargs: None),
        )

        state = driver._configure_product(target)

        self.assertEqual(state, 'connecting')
        self.assertIn(('source', 'hook'), settings)
        self.assertIn(('uncertain_text_fallback', True), settings)
        self.assertEqual(next(value for name, value in settings if name == 'advance_mode').value,
                         'audio')
        self.assertFalse(any(name in {'native_helper', 'asr_model', 'asr_tokens'}
                             for name, _ in settings),
                         'accuracy must exercise Application managed-runtime resolution')

    def test_native_product_handoff_forces_one_recovery_and_keeps_run_alive(self):
        actions, traces, recoveries, boundaries = [], [], [], []
        clock = [1.0]
        runtime = SimpleNamespace(token=SimpleNamespace(session=1, line=1),
                                  pause_reason='target_unavailable', sent=False,
                                  target=SimpleNamespace(hwnd=77),
                                  profile=SimpleNamespace(input_key=13))
        product = SimpleNamespace(runtime_state=lambda: runtime)
        window = SimpleNamespace(application=product, close=lambda: actions.append('close'))
        trace = SimpleNamespace(emit=lambda name, **fields: traces.append((name, fields)))
        qt_app = SimpleNamespace(quit=lambda: actions.append('quit'))
        driver = SessionDriver(
            window, qt_app,
            {'timeout_seconds': 30, 'phase': 'accuracy', 'dialogues': 3,
             'continue_collection': True}, trace,
            clock=lambda: clock[0], recovery_send=lambda state: recoveries.append(state.token.line),
            recovery_confirm=lambda state: (
                boundaries.append(state.token.line), setattr(runtime, 'pause_reason', None)))
        driver.state = 'running'
        driver.tick()
        self.assertEqual(recoveries, [])
        clock[0] = 1.3
        driver.tick()
        self.assertEqual(recoveries, [1])
        self.assertEqual(boundaries, [1])
        self.assertEqual(actions, [])
        self.assertEqual(traces[-1][0], 'validation_recovery_input')
        self.assertEqual(traces[-1][1]['reason'], 'target_unavailable')
        clock[0] = 1.8
        driver.tick()
        self.assertEqual(recoveries, [1])
        self.assertEqual(actions, [])
        runtime.token = SimpleNamespace(session=1, line=2)
        clock[0] = 2.0
        driver.tick()
        self.assertEqual(actions, [])

    def test_final_native_handoff_finishes_collection_without_extra_input(self):
        actions, recoveries = [], []
        clock = [1.0]
        runtime = SimpleNamespace(token=SimpleNamespace(session=1, line=3),
                                  pause_reason='native_inconclusive_hold', sent=False)
        product = SimpleNamespace(runtime_state=lambda: runtime)
        window = SimpleNamespace(application=product, close=lambda: actions.append('close'))
        trace = SimpleNamespace(emit=lambda *args, **kwargs: None)
        qt_app = SimpleNamespace(quit=lambda: actions.append('quit'))
        driver = SessionDriver(
            window, qt_app,
            {'timeout_seconds': 30, 'phase': 'accuracy', 'dialogues': 3,
             'continue_collection': True}, trace,
            clock=lambda: clock[0], recovery_send=lambda state: recoveries.append(state.token.line),
            recovery_confirm=lambda state: None)
        driver.state = 'running'
        driver.tick()
        self.assertEqual(recoveries, [])
        self.assertEqual(actions, [])
        clock[0] = 1.7
        driver.tick()
        self.assertEqual(actions, ['close', 'quit'])

    def test_dialogue_after_collection_target_cannot_extend_collection_or_recovery(self):
        actions, recoveries = [], []
        clock = [1.0]
        runtime = SimpleNamespace(token=SimpleNamespace(session=1, line=4),
                                  pause_reason=None, sent=False)
        product = SimpleNamespace(runtime_state=lambda: runtime)
        window = SimpleNamespace(application=product, close=lambda: actions.append('close'))
        trace = SimpleNamespace(emit=lambda *args, **kwargs: None)
        qt_app = SimpleNamespace(quit=lambda: actions.append('quit'))
        driver = SessionDriver(
            window, qt_app,
            {'timeout_seconds': 30, 'phase': 'accuracy', 'dialogues': 3,
             'continue_collection': True}, trace,
            clock=lambda: clock[0],
            recovery_send=lambda state: recoveries.append(state.token.line),
            recovery_confirm=lambda state: None)
        driver.state = 'running'
        driver.tick()
        self.assertEqual(recoveries, [])
        self.assertEqual(actions, [])
        runtime.pause_reason = 'native_inconclusive_hold'
        clock[0] = 1.7
        driver.tick()
        self.assertEqual(recoveries, [])
        self.assertEqual(actions, ['close', 'quit'])

    def test_failure_diagnostics_read_application_state_not_retired_gui_state(self):
        recorded = []
        closed = []
        source = SimpleNamespace(identity='hook-a')
        candidate = SimpleNamespace(
            identity='hook-a', address=7, ambiguous=False, history=('unrelated text',),
            samples=(SimpleNamespace(raw='unrelated text', speaker='', body='unrelated text'),))
        runtime = SimpleNamespace(session=4, token=None, pause_reason=None)
        product = SimpleNamespace(
            capture_view=lambda: SimpleNamespace(candidates=(candidate,)),
            capture_source=lambda identity, address, require_unique=False: source,
            hook_host_running=True,
            runtime_state=lambda: runtime,
        )
        window = SimpleNamespace(application=product, close=lambda: closed.append('window'))
        trace = SimpleNamespace(emit=lambda name, **kwargs: recorded.append((name, kwargs)))
        qt_app = SimpleNamespace(exit=lambda code: closed.append(code))
        driver = SessionDriver(window, qt_app, {'timeout_seconds': 30, 'phase': 'accuracy'}, trace)
        driver.state = 'calibration'
        driver.fail('finite live-run timeout before requested validation collection completed')
        self.assertEqual(closed, ['window', 3])
        name, fields = recorded[0]
        self.assertEqual(name, 'validation_driver_failure')
        self.assertEqual(fields['stage'], 'calibration')
        self.assertEqual(fields['candidate_hooks'], 1)
        self.assertEqual(fields['sampled_hooks'], 1)
        self.assertEqual(fields['calibration_matches'], 0)
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
            'self.product.capture_view()',
            'self.product.set_capture_field_source(', 'self.product.commit_capture()',
            'self.product.runtime_state()', 'self.w.permit()',
        ):
            self.assertIn(api, source)

    def test_requires_bound_game_target_and_finite_timeout(self):
        valid = dict(gate='session-gate', executable='validation-game.exe',
                     phase='accuracy', timeout_seconds=120)
        self.assertEqual(load_session({'AUTOADV_VALIDATION_SESSION': json.dumps(valid)})['phase'], 'accuracy')
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
