"""Drive existing GUI actions for the existing Ren'Py fixture, never game input.

The game only receives advance input from the production Controller and its
WindowsAdvancementInput. A unique *actual* LunaHook source is selected using
an unmeasured calibration line; absence/ambiguity is a test failure.
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


def load_session(environment=None):
    env = os.environ if environment is None else environment
    options = json.loads(env['AUTOADV_VALIDATION_SESSION'])
    mandatory = ('oracle', 'gate', 'executable', 'phase', 'timeout_seconds')
    for key in mandatory:
        if not options.get(key):
            raise ValueError('missing validation session option: ' + key)
    if options['phase'] not in ('hook', 'reading', 'native', 'tts', 'mixed', 'stress'):
        raise ValueError('invalid validation phase')
    if int(options['timeout_seconds']) < 10:
        raise ValueError('validation session must have a finite reasonable timeout')
    return options


class SessionDriver:
    def __init__(self, window, app, options, *, clock=time.monotonic):
        self.w, self.app, self.options = window, app, options
        self.clock = clock
        self.deadline = clock() + int(options['timeout_seconds'])
        self.state = 'launch_host'
        self.game_pid = None
        self.done_idle = None

    def fail(self, why):
        print('AutoAdv live test failed [' + self.state + ']: ' + why, flush=True)
        self.w.validation_trace.emit('validation_driver_failure', reason=why, stage=self.state)
        self.w.close()
        self.app.exit(3)

    def _game_target(self):
        from autoadv.game_windows import find_game_windows
        oracle = Path(self.options['oracle'])
        if not oracle.is_file():
            return None
        lines = oracle.read_text(encoding='utf-8').splitlines()
        if not lines:
            return None
        start = json.loads(lines[0])
        if start.get('event') != 'oracle_started' or type(start.get('pid')) is not int:
            raise ValueError('invalid game launch evidence')
        self.game_pid = start['pid']
        matches = [w for w in find_game_windows(self.options['executable']) if w.pid == self.game_pid]
        if len(matches) > 1:
            raise ValueError('multiple game windows for the same PID')
        return matches[0] if matches else None

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
        w.native_vad_only.setChecked(False)
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
        import ctypes
        hwnd = int(self.w.hwnd.text(), 0)
        user32 = ctypes.windll.user32
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        if user32.GetForegroundWindow() != hwnd:
            raise RuntimeError('game did not gain input focus; auto-advance cannot be tested')

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
                candidates = calibration_candidates(self.w.sources, self.w.source_samples,
                                                    self.w.ambiguous_sources)
                if candidates:
                    if len(candidates) != 1:
                        raise RuntimeError('ambiguous actual LunaHook calibration outputs')
                    src = candidates[0]
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
                elif self.w.controller.reason == 'native_audio_unavailable_manual_handoff':
                    raise RuntimeError('real native audio setup failed')
            elif self.state == 'activate':
                if self.options['phase'] != 'hook':
                    self.w.permit()
                    if not self.w.controller.authorized:
                        raise RuntimeError('AutoAdv not authorized to send')
                self._focus_game()
                self._release('.monitoring')
                self.state = 'running' 
            elif self.state == 'running':
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
