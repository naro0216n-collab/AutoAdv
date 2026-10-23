"""Prepare and observe the ordinary AutoAdv GUI for live validation.

Normal progression is owned entirely by the product Controller.  In native
RealAudio collection only, after the product has already entered a recorded
hold/failure, the validation driver may send one explicitly traced recovery
input so later fixture cases remain observable.  Recovery is never product
success and never reimplements Hook, presentation, or audio judgement policy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

CALIBRATION = 'AutoAdvフック校正専用'
RECOVERABLE_VALIDATION_PAUSES = frozenset((
    'native_inconclusive_hold',
    'native_audio_failure',
    'input_failure_no_retry',
    'next_line_timeout',
))


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
    if ('force_continue_on_failure' in options and
            type(options['force_continue_on_failure']) is not bool):
        raise ValueError('force_continue_on_failure must be boolean')
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
    def __init__(self, window, qt_app, options, *, clock=time.monotonic,
                 recovery_send=None, recovery_confirm=None):
        self.w, self.qt_app, self.options = window, qt_app, options
        self.product = window.application
        self.clock = clock
        self.recovery_send = recovery_send or self._send_validation_recovery
        self.recovery_confirm = recovery_confirm or self._confirm_validation_recovery
        self.deadline = clock() + int(options['timeout_seconds'])
        self.state = 'select_target'
        self.done_idle = None
        self.audio_deadline = None
        self.recovery_pending = None
        self.collection_target_emitted = False

    def fail(self, why):
        candidates = self.product.capture_view().candidates
        runtime = self.product.runtime_state()
        details = dict(
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

    @staticmethod
    def _send_validation_recovery(runtime):
        """Advance only the validation fixture after the product has failed.

        This bypasses the normal AutoAdv input path and is never exposed as a
        product send.  The harness separately confirms only the known boundary
        so an identical next Hook can still be collected after forced progress.
        """
        from autoadv.windows_input import WindowsAdvancementInput
        if runtime.target is None or runtime.profile is None:
            raise RuntimeError('validation recovery has no bound game target')
        WindowsAdvancementInput(
            runtime.target.hwnd, virtual_key=runtime.profile.input_key).advance()

    def _confirm_validation_recovery(self, runtime):
        token = runtime.token
        if token is None or not self.product.confirm_external_advance(token):
            raise RuntimeError('validation recovery lost the active utterance boundary')

    def _recover_failed_line(self, runtime):
        token = runtime.token
        if token is None:
            return False
        key = (token.session, token.line)
        reason = runtime.pause_reason or ''
        if (not self.options.get('force_continue_on_failure') or
                reason not in RECOVERABLE_VALIDATION_PAUSES):
            return False
        # Give queued product diagnostics (handoff/capture summary) one GUI
        # turn to reach the trace before the validation-only recovery input.
        now = self.clock()
        pending = self.recovery_pending
        if pending is None or pending[:2] != key or pending[2] != reason:
            self.recovery_pending = (token.session, token.line, reason, now)
            return True
        if now - pending[3] < .25:
            return True
        self.w.validation_trace.emit(
            'validation_recovery_attempt', session=token.session, line=token.line,
            reason=reason)
        # Establish the known test-only boundary immediately before the forced
        # input, matching the product input/audio-boundary ordering.
        self.recovery_confirm(runtime)
        self.recovery_send(runtime)
        self.w.validation_trace.emit(
            'validation_recovery_input', session=token.session, line=token.line,
            reason=reason)
        self.recovery_pending = None
        return True

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
        return state

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
            self.fail('finite live-run timeout before requested validation collection completed')
            return
        try:
            if self.state == 'select_target':
                target = self._game_target()
                if target:
                    state = self._configure_product(target)
                    if state in ('connected', 'monitoring'):
                        self._release('.attached')
                        self.state = 'calibration'
                    else:
                        self.state = 'attach'
            elif self.state == 'attach':
                state = self.product.activate_selected_target()
                if state in ('connected', 'monitoring'):
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
                if not self.collection_target_emitted:
                    self.w.validation_trace.emit(
                        'validation_collection_target', phase=self.options['phase'],
                        dialogues=int(self.options.get('dialogues', 0)),
                        force_continue_on_failure=bool(
                            self.options.get('force_continue_on_failure', False)))
                    self.collection_target_emitted = True
                if self.options['phase'] != 'hook':
                    self.w.permit()
                    if not self.product.runtime_state().authorized:
                        raise RuntimeError('AutoAdv not authorized to send')
                self._release('.monitoring')
                self.state = 'running'
            elif self.state == 'running':
                runtime = self.product.runtime_state()
                target_count = int(self.options.get('dialogues', 0))
                line_no = runtime.token.line if runtime.token is not None else 0
                final_line = target_count > 0 and line_no >= target_count
                if runtime.token is not None and runtime.pause_reason:
                    # For RealAudio data collection, a product hold remains a
                    # failure but the harness may advance the fixture once so
                    # later cases are still observed in this same product
                    # session. Never do this after the final requested case.
                    if not final_line and self._recover_failed_line(runtime):
                        return
                    if not final_line:
                        self.w.close()
                        self.qt_app.quit()
                        return
                else:
                    self.recovery_pending = None
                if final_line and (runtime.sent or runtime.pause_reason):
                    if self.done_idle is None:
                        self.done_idle = self.clock()
                    if self.clock() - self.done_idle >= .6:
                        self.w.close()
                        self.qt_app.quit()
                elif not final_line:
                    self.done_idle = None
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
