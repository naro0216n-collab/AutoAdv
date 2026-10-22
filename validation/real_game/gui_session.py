"""Prepare and observe the ordinary AutoAdv GUI for live validation.

The driver owns fixture preparation only. It never sends game input and never
reimplements Hook, presentation, audio, or advancement policy. Runtime state is
read and changed through AutoAdvApplication, the same owner used by the GUI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

CALIBRATION = 'AutoAdvフック校正専用'


def calibration_candidates(candidates):
    """Use the product's extraction preview, not raw substring matching.

    The fixture deliberately emits calibration with a speaker.  A source is
    usable only when AutoAdv's ordinary ``auto`` extraction resolves its body
    to the calibration text.  This rejects speaker-only/noisy outputs while
    keeping both body-only and structured speaker+body Hook sources usable.
    """
    return [candidate for candidate in candidates
            if not candidate.ambiguous
            and any(sample.body == CALIBRATION for sample in candidate.samples)]


def choose_calibration_source(candidates):
    """Pick one product-previewed candidate deterministically.

    Prefer a source whose raw output already is the body.  If an engine only
    exposes speaker+body together, the product's structural extraction preview
    is sufficient and remains the recipe used for later dialogue.
    """
    matches = calibration_candidates(candidates)
    if not matches:
        return None
    return min(matches, key=lambda candidate: (
        not any(sample.body == CALIBRATION and sample.raw.strip() == CALIBRATION
                for sample in candidate.samples),
        candidate.identity,
        candidate.address,
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
    """Identify the fixture SDL window, not helper/console processes."""
    matches = [window for window in windows
               if class_name(window.hwnd).casefold() == 'sdl_app']
    if len(matches) > 1:
        names = [(w.title, class_name(w.hwnd)) for w in matches]
        raise ValueError(f'ambiguous RenPy GUI windows: {names}')
    return matches[0] if matches else None


class SessionDriver:
    def __init__(self, window, qt_app, options, *, clock=time.monotonic):
        self.w, self.qt_app, self.options = window, qt_app, options
        self.product = window.application
        self.clock = clock
        self.deadline = clock() + int(options['timeout_seconds'])
        self.state = 'select_target'
        self.done_idle = None
        self.audio_deadline = None

    def fail(self, why):
        candidates = self.product.capture_view().candidates
        connection = self.product.hook_connection_state()
        runtime = self.product.runtime_state()
        details = dict(
            luna_connected=connection.connected,
            candidate_hooks=len(candidates),
            sampled_hooks=sum(bool(candidate.history) for candidate in candidates),
            ambiguous_hooks=sum(candidate.ambiguous for candidate in candidates),
            calibration_matches=len(calibration_candidates(candidates)),
            luna_host_alive=self.product.hook_host_running,
            runtime_session=runtime.session,
            runtime_line=(runtime.token.line if runtime.token is not None else 0),
            runtime_pause=runtime.pause_reason or '',
        )
        print('AutoAdv live test failed [' + self.state + ']: ' + why
              + ' | product state: ' + json.dumps(details, ensure_ascii=False), flush=True)
        self.w.validation_trace.emit('validation_driver_failure', reason=why,
                                     stage=self.state, **details)
        self.w.close()
        self.qt_app.exit(3)

    def _game_target(self):
        from autoadv.game_windows import find_game_windows
        from ctypes import wintypes
        import ctypes
        get_class_name = ctypes.windll.user32.GetClassNameW
        get_class_name.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        get_class_name.restype = ctypes.c_int

        def window_class(hwnd):
            name = ctypes.create_unicode_buffer(256)
            if not get_class_name(hwnd, name, len(name)):
                raise OSError('cannot identify validation game window class')
            return name.value

        return select_game_window(find_game_windows(self.options['executable']), window_class)

    def _release(self, suffix):
        Path(self.options['gate'] + suffix).write_text('ready', encoding='ascii')

    def _configure_product(self, target):
        from autoadv.model import AdvanceMode

        game = self.product.create_game('RenPy AutoAdv validation', self.options['executable'])
        self.product.select_profile(game.id, game.default_profile)
        self.product.select_primary_target(target.pid, target.hwnd)

        self.product.set_profile_setting('source', 'hook')
        self.product.set_profile_setting(
            'advance_mode',
            AdvanceMode.TEXT if self.options['phase'] in ('hook', 'reading') else AdvanceMode.AUDIO)
        self.product.set_profile_setting('default_tts', self.options['phase'] == 'tts')
        self.product.set_profile_setting(
            'uncertain_text_fallback', bool(self.options.get('fallback', False)))
        self.product.set_profile_setting('repeat_rule', 'each_after_input')

        profile = self.product.current_profile()
        if profile is None:
            raise RuntimeError('validation profile was not created')
        self.product.set_capture_environment(
            ocr_executable=profile.ocr.executable,
            ocr_language=profile.ocr.language,
            ocr_region=profile.ocr.region,
            ocr_stable_samples=profile.ocr.stable_samples,
            insert_pc_hooks=profile.insert_pc_hooks,
            hook_flush_ms=200,
            hook_codepage=profile.hook_codepage,
            hook_max_buffer=profile.hook_max_buffer,
            hook_max_history=profile.hook_max_history,
        )

        if self.options['phase'] in ('native', 'mixed', 'stress'):
            for field in ('helper', 'model', 'tokens'):
                path = self.options.get(field, '')
                if not path or not Path(path).is_file():
                    raise ValueError('real native audio dependency unavailable: ' + field)
            self.product.set_profile_setting('native_helper', self.options['helper'])
            self.product.set_profile_setting('asr_model', self.options['model'])
            self.product.set_profile_setting('asr_tokens', self.options['tokens'])

        if self.options['phase'] == 'tts':
            voice = self.options.get('voice', '').strip()
            if not voice:
                raise ValueError('Irodori Voice ID required for a TTS test')
            self.product.set_profile_setting('default_voice', voice)
            self.product.set_irodori_environment(
                self.product.catalog.irodori_root,
                self.options.get('irodori_url', 'http://127.0.0.1:8088'))

        state = self.product.activate_selected_target()
        if state not in ('connecting', 'connected', 'monitoring'):
            raise RuntimeError('could not activate the selected validation game')

    def _select_calibration_source(self):
        candidate = choose_calibration_source(self.product.capture_view().candidates)
        if candidate is None:
            return False
        source = self.product.capture_source(
            candidate.identity, candidate.address, require_unique=False)
        if source is None:
            return False
        # Keep the same product extraction semantics that proved the calibration
        # body.  Forcing the raw string to ``body`` would later fold a speaker
        # line into the ASR target and invalidate native-audio attribution.
        self.product.set_capture_extraction_preset(source.identity, source.address, 'auto')
        self.product.set_capture_field_source('body', source.identity, source.address)
        self.product.set_capture_field_source('speaker', None)
        saved = self.product.commit_capture()
        if not saved.body_sources:
            raise RuntimeError('real selected Hook was not saved as the dialogue body source')
        if self.product.runtime_state().profile is None:
            raise RuntimeError('monitor could not start from the committed capture recipe')
        return True

    def tick(self):
        if self.clock() > self.deadline:
            self.fail('finite live-run timeout; no manual game input substituted')
            return
        try:
            if self.state == 'select_target':
                target = self._game_target()
                if target:
                    self._configure_product(target)
                    self.state = 'attach'
            elif self.state == 'attach':
                if self.product.hook_connection_state().connected:
                    self._release('.attached')
                    self.state = 'calibration'
            elif self.state == 'calibration':
                if self._select_calibration_source():
                    if self.options['phase'] in ('native', 'mixed', 'stress'):
                        self.audio_deadline = self.clock() + 30.0
                        self.state = 'await_audio'
                    else:
                        self.state = 'activate'
            elif self.state == 'await_audio':
                if self.product.native_audio_available:
                    self.state = 'activate'
                elif self.clock() >= self.audio_deadline:
                    raise RuntimeError('product process audio did not become ready within 30 seconds')
            elif self.state == 'activate':
                if self.options['phase'] != 'hook':
                    self.w.permit()
                    if not self.product.runtime_state().authorized:
                        raise RuntimeError('AutoAdv not authorized to send')
                self._release('.monitoring')
                self.state = 'running'
            elif self.state == 'running':
                runtime = self.product.runtime_state()
                if runtime.token is not None and runtime.pause_reason:
                    self.w.close()
                    self.qt_app.quit()
                    return
                target_count = int(self.options.get('dialogues', 0))
                line_no = runtime.token.line if runtime.token is not None else 0
                if target_count > 0 and line_no >= target_count:
                    if self.done_idle is None:
                        self.done_idle = self.clock()
                    if self.clock() - self.done_idle >= .6:
                        self.w.close()
                        self.qt_app.quit()
        except Exception as error:
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
