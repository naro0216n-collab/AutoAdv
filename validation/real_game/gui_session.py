"""Drive existing GUI actions for the existing Ren'Py fixture, never game input.

The game only receives advance input from the production Controller and its
WindowsAdvancementInput. A source is selected from *actual* LunaHook outputs
of an unmeasured calibration line; continuation is judged by the product path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

CALIBRATION = 'AutoAdvフック校正専用'


def calibration_candidates(sources, samples, ambiguous):
    """Choose from actual Luna observations; no invented Hook identity."""
    return [source for identity, source in sources.items()
            if identity not in ambiguous
            and any(CALIBRATION in sample for sample in samples.get(identity, ()))]


def choose_calibration_source(sources, samples, ambiguous):
    """Pick one *observed* GUI Hook candidate, as a user would for a game.

    Multiple real Luna threads can emit the same line. They are distinct Hook
    candidates, not a reason to refuse to start the product. Favor complete
    calibration output, then stable Hook identity; the subsequent live trace
    must still establish actual dialogue progression and cannot be synthesized.
    """
    candidates = calibration_candidates(sources, samples, ambiguous)
    if not candidates:
        return None
    return min(candidates, key=lambda source: (
        not any(line.strip() == CALIBRATION for line in samples.get(source.identity, ())),
        source.identity,
        source.address,
    ))


def load_session(environment=None):
    env = os.environ if environment is None else environment
    options = json.loads(env['AUTOADV_VALIDATION_SESSION'])
    mandatory = ('gate', 'executable', 'phase', 'timeout_seconds')
    for key in mandatory:
        if not options.get(key):
            raise ValueError('missing validation session option: ' + key)
    if options['phase'] not in ('hook', 'reading', 'native', 'tts', 'mixed', 'stress'):
        raise ValueError('invalid validation phase')
    if int(options['timeout_seconds']) < 10:
        raise ValueError('validation session must have a finite reasonable timeout')
    return options


def select_game_window(windows, class_name):
    """Identify the existing Ren'Py SDL window, not Python console hosts.

    Executable/PID are already constrained by the ordinary window discovery.
    The Ren'Py test fixture uses SDL_app; no game's UI is driven or inspected.
    """
    matches = [window for window in windows
               if class_name(window.hwnd).casefold() == 'sdl_app']
    if len(matches) > 1:
        names = [(w.title, class_name(w.hwnd)) for w in matches]
        raise ValueError(f'ambiguous RenPy GUI windows: {names}')
    return matches[0] if matches else None


class SessionDriver:
    def __init__(self, window, app, options, *, clock=time.monotonic):
        self.w, self.app, self.options = window, app, options
        self.clock = clock
        self.deadline = clock() + int(options['timeout_seconds'])
        self.state = 'launch_host'
        self.done_idle = None
        self.focus_deadline = None

    def fail(self, why):
        print('AutoAdv live test failed [' + self.state + ']: ' + why, flush=True)
        self.w.validation_trace.emit('validation_driver_failure', reason=why, stage=self.state)
        self.w.close()
        self.app.exit(3)

    def _game_target(self):
        # Detect only the known Ren'Py SDL surface from the configured
        # executable. Game-side playback observations are not required for
        # hooking, text advancement or TTS playback acceptance.
        from autoadv.game_windows import find_game_windows
        from ctypes import wintypes
        import ctypes
        get_class_name = ctypes.windll.user32.GetClassNameW
        get_class_name.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        get_class_name.restype = ctypes.c_int
        def window_class(hwnd):
            name = ctypes.create_unicode_buffer(256)
            if not get_class_name(hwnd, name, len(name)):
                raise OSError('cannot identify RenPy window class')
            return name.value
        return select_game_window(find_game_windows(self.options['executable']), window_class)

    def _release(self, suffix):
        Path(self.options['gate'] + suffix).write_text('ready', encoding='ascii')

    def _configure(self, target):
        from autoadv.model import AdvanceMode
        w = self.w
        game = w.catalog.create_game('RenPy AutoAdv validation', self.options['executable'])
        w.fill_games()
        w.game_choice.setCurrentIndex(w.game_choice.findData(game.id))
        w.pid.setValue(target.pid)
        w.hwnd.setText(hex(target.hwnd))
        # The GUI saves to the isolated APPDATA location provisioned by runner.
        # No product processing is replaced and no validation outcome injected.
        for box in w.inherit_controls.values():
            box.setChecked(False)
        w.source_mode.setCurrentText('hook')
        w.advance_mode.setCurrentIndex(w.advance_mode.findData(
            AdvanceMode.TEXT.value if self.options['phase'] in ('hook', 'reading') else AdvanceMode.AUDIO.value))
        w.default_tts.setChecked(self.options['phase'] == 'tts')
        w.uncertain_text_fallback.setChecked(bool(self.options.get('fallback', False)))
        w.repeat.setCurrentText('each_after_input')
        w.hook_flush.setValue(200)
        if self.options['phase'] in ('native', 'mixed', 'stress'):
            for field, widget in (('helper', w.native_helper), ('model', w.asr_model), ('tokens', w.asr_tokens)):
                path = self.options.get(field, '')
                if not path or not Path(path).is_file():
                    raise ValueError('real native audio dependency unavailable: ' + field)
                widget.setText(path)
        if self.options['phase'] == 'tts':
            voice = self.options.get('voice', '')
            if not voice:
                raise ValueError('Irodori Voice ID required for a TTS test')
            w.default_voice.setEditText(voice)
            w.irodori_url.setText(self.options.get('irodori_url', 'http://127.0.0.1:8088'))
        if not w.save_profile():
            raise ValueError('could not save actual GUI profile')
        w.start_host()
        if not w.hook_host:
            raise RuntimeError('adopted LunaHost failed to start')
        w.connect_game()

    def _focus_game(self):
        # Foreground focus is an ordinary Windows input prerequisite, not
        # in-game GUI automation. Activation is asynchronous on Windows.
        import ctypes
        hwnd = int(self.w.hwnd.text(), 0)
        ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        self.focus_deadline = self.clock() + 3.0

    def _game_is_focused(self):
        import ctypes
        hwnd = int(self.w.hwnd.text(), 0)
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return user32.GetForegroundWindow() == hwnd


    def tick(self):
        if self.clock() > self.deadline:
            self.fail('finite live-run timeout; no manual game input substituted')
            return
        try:
            if self.state == 'launch_host':
                target = self._game_target()
                if target:
                    self._configure(target)
                    self.state = 'attach'
            elif self.state == 'attach':
                if self.w.host_generation and self.w.connection_generation:
                    self._release('.attached')
                    self.state = 'calibration'
            elif self.state == 'calibration':
                src = choose_calibration_source(self.w.sources, self.w.source_samples,
                                                 self.w.ambiguous_sources)
                if src:
                    row = self.w.source_rows[(src.identity, src.address)]
                    self.w.source_table.selectRow(row)
                    self.w.select_hook('text')
                    if not self.w.current_profile().hooks:
                        raise RuntimeError('real selected Hook not saved')
                    self.w.begin_monitor()
                    if not self.w.controller.profile or self.w.controller.reason == 'session_changed':
                        raise RuntimeError('monitor could not start')
                    self.state = 'await_audio' if self.options['phase'] in ('native', 'mixed', 'stress') else 'activate'
            elif self.state == 'await_audio':
                if self.w.native_audio:
                    self.state = 'activate'
                elif self.w.native_audio_setup_error:
                    raise RuntimeError('product process audio failed to start: ' + self.w.native_audio_setup_error)
            elif self.state == 'activate':
                if self.options['phase'] != 'hook':
                    self.w.permit()
                    if not self.w.controller.authorized:
                        raise RuntimeError('AutoAdv not authorized to send')
                self._focus_game()
                self.state = 'await_focus'
            elif self.state == 'await_focus':
                if self._game_is_focused():
                    self._release('.monitoring')
                    self.state = 'running'
                elif self.clock() >= self.focus_deadline:
                    raise RuntimeError('Windows runner cannot focus the game for product SendInput')
            elif self.state == 'running':
                # An AutoAdv handoff is a completed product outcome, not a
                # reason to force the test game through later dialogues. Exit
                # now and let the independent verifier classify this line.
                c = self.w.controller
                if c.token is not None and c.pause_reason:
                    self.w.close()
                    self.app.quit()
                    return
                # Stop once actual selected-Hook outputs demonstrate the
                # required continuation. No test-owned input is generated.
                target_count = int(self.options.get('dialogues', 0))
                if target_count > 0 and self.w.controller.line_no >= target_count:
                    if self.done_idle is None:
                        self.done_idle = self.clock()
                    if self.clock() - self.done_idle >= .6:
                        self.w.close()
                        self.app.quit()
        except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
            self.fail(str(error))


def install(window, app):
    from PySide6.QtCore import QTimer
    driver = SessionDriver(window, app, load_session())
    timer = QTimer(window)
    timer.setInterval(150)
    timer.timeout.connect(driver.tick)
    timer.start()
    window._validation_driver = driver
    window._validation_timer = timer
    driver.tick()
