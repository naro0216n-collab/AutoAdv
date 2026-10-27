"""Prepare and observe the ordinary AutoAdv GUI for live validation.

Normal progression is owned entirely by the product Controller. The validation
driver does not interpret product state and never advances the fixture to
recover a stalled Presentation. The outer live-run timeout only terminates a
collection that the product failed to progress.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time

CALIBRATION = 'AutoAdvフック校正専用'
# The pinned Ren'Py fixture exposes stable Luna sources for body text and the
# speaker-bearing aggregate. This is fixture configuration, not a
# validation-side Hook inference rule: if either known source is absent, the
# fixture/runtime contract is broken and the run must fail rather than select a
# different candidate.
FIXTURE_BODY_HOOK_IDENTITY = 'EXHQXFN0@E81D0:libpython3.12.dll|0|0'
FIXTURE_SPEAKER_HOOK_IDENTITY = 'ENHQXFN-1C:-24@53680:LunaHook64.dll|0|2'
FIXTURE_SPEAKER_RULE = (
    r'^\s*(?P<speaker>[^\n]{1,40})\n?(?P<body>.+)\n(?P=body)\s*$'
)
ACCURACY_SCENARIO = json.loads(
    (Path(__file__).with_name('ADVANCEMENT_ACCURACY_SCENARIO.json')).read_text(encoding='utf-8'))['lines']
ACCURACY_GAME_INPUTS_MS = {
    str(line['id']): int(line['game_input_after_ms'])
    for line in ACCURACY_SCENARIO if 'game_input_after_ms' in line
}
def fixture_source(candidates, identity):
    """Return one preconfigured Hook source of the bundled fixture."""
    matches = [candidate for candidate in candidates
               if not candidate.ambiguous
               and candidate.identity == identity]
    return matches[0] if len(matches) == 1 else None


def load_session(environment=None):
    env = os.environ if environment is None else environment
    options = json.loads(env['AUTOADV_VALIDATION_SESSION'])
    mandatory = ('gate', 'executable', 'phase', 'timeout_seconds')
    for key in mandatory:
        if not options.get(key):
            raise ValueError('missing validation session option: ' + key)
    if options['phase'] not in ('hook', 'reading', 'accuracy', 'tts', 'mixed', 'stress'):
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
    def __init__(self, window, qt_app, options, trace, *, clock=time.monotonic,
                 game_input_send=None):
        self.w, self.qt_app, self.options, self.trace = window, qt_app, options, trace
        self.product = window.application
        self.clock = clock
        self.game_input_send = game_input_send or self._send_validation_game_input
        self.deadline = clock() + int(options['timeout_seconds'])
        self.state = 'select_target'
        self.audio_deadline = None
        evidence = str(options.get('evidence') or '').strip()
        self.evidence_path = Path(evidence) if evidence else None
        self.final_observation_seconds = float(options.get('final_observation_seconds', 12.0))
        self.current_case = None
        self.current_case_seen_at = None
        self.collection_target_seen_at = None
        self.game_input_cases = set()
        self.collection_target_emitted = False
        self.non_evidence_done_at = None

    def fail(self, why):
        print('AutoAdv live measurement stopped [' + self.state + ']: ' + why, flush=True)
        self.trace.emit('validation_driver_failure', reason=why, stage=self.state)
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
    def _send_validation_game_input(runtime):
        """Inject one ordinary external keyboard stimulus into the active game.

        This does not call the product's targeted PostMessage adapter or inject
        Controller state. WindowsGameInputObserver must observe the low-level
        key event while the selected game is the Windows foreground target, and
        the fixture independently records whether the game actually advanced.
        """
        import ctypes
        from ctypes import wintypes

        if runtime.target is None or runtime.profile is None:
            raise RuntimeError('validation game input has no bound game target or key')
        virtual_key = int(runtime.profile.input_key)
        if not 1 <= virtual_key <= 254:
            raise RuntimeError('validation game input key is invalid')
        user32 = ctypes.windll.user32

        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.SetForegroundWindow(wintypes.HWND(int(runtime.target.hwnd)))
        # Foreground activation is part of the authored external stimulus.  Do
        # not fall back to targeted messages: that would bypass the product's
        # low-level keyboard observation path we are validating.
        time.sleep(0.05)
        foreground = user32.GetForegroundWindow()
        foreground_pid = wintypes.DWORD()
        if (not foreground
                or not user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
                or int(foreground_pid.value) != int(runtime.target.pid)):
            raise RuntimeError('validation game could not become the foreground input target')

        user32.MapVirtualKeyW.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
        user32.MapVirtualKeyW.restype = ctypes.c_uint32
        scan = int(user32.MapVirtualKeyW(virtual_key, 0))
        if not 1 <= scan <= 0xFF:
            raise RuntimeError('validation game input key has no Windows scancode')

        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ('dx', wintypes.LONG),
                ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD),
                ('dwExtraInfo', ULONG_PTR),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ('wVk', wintypes.WORD),
                ('wScan', wintypes.WORD),
                ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD),
                ('dwExtraInfo', ULONG_PTR),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ('uMsg', wintypes.DWORD),
                ('wParamL', wintypes.WORD),
                ('wParamH', wintypes.WORD),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ('data',)
            _fields_ = [('type', wintypes.DWORD), ('data', INPUT_UNION)]

        INPUT_KEYBOARD = 1
        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002
        extended = KEYEVENTF_EXTENDEDKEY if virtual_key in {
            0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
            0x2D, 0x2E, 0x6F, 0x90, 0x91,
        } else 0
        inputs = (INPUT * 2)(
            INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(
                wVk=virtual_key, wScan=scan, dwFlags=extended, time=0, dwExtraInfo=0)),
            INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(
                wVk=virtual_key, wScan=scan, dwFlags=extended | KEYEVENTF_KEYUP,
                time=0, dwExtraInfo=0)),
        )
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        if int(user32.SendInput(len(inputs), inputs, ctypes.sizeof(INPUT))) != len(inputs):
            raise RuntimeError('validation low-level keyboard input was not injected')

    def _send_authored_game_input(self, runtime, case_id, now):
        delay_ms = ACCURACY_GAME_INPUTS_MS.get(case_id)
        if delay_ms is None or case_id in self.game_input_cases or self.current_case_seen_at is None:
            return False
        if (now - self.current_case_seen_at) * 1000.0 < delay_ms:
            return False
        self.game_input_send(runtime)
        self.trace.emit('validation_game_input', case=case_id, scheduled_after_ms=delay_ms)
        self.game_input_cases.add(case_id)
        return True

    def _fixture_cases(self):
        """Return authored fixture-case facts without interpreting game state."""
        if self.evidence_path is None or not self.evidence_path.is_file():
            return []
        records = []
        with self.evidence_path.open('r', encoding='utf-8') as stream:
            for raw_line in stream:
                if not raw_line.endswith('\n'):
                    continue
                try:
                    item = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if item.get('event') == 'fixture_case':
                    records.append(item)
        return records

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
        fallback = True if self.options['phase'] == 'accuracy' else bool(self.options.get('fallback', False))
        self.product.set_profile_setting('uncertain_text_fallback', fallback)
        if self.options['phase'] == 'accuracy':
            force_seconds = int(self.options.get('force_advance_seconds', 12))
            if force_seconds <= 0:
                raise ValueError('accuracy force_advance_seconds must be positive')
            self.product.set_profile_setting('force_advance_seconds', force_seconds)
        self.product.set_profile_setting('repeat_rule', 'each_flush')

        profile = self.product.current_profile()
        if profile is None:
            raise RuntimeError('validation profile was not created')
        self.product.set_capture_environment(
            ocr_executable=profile.ocr.executable,
            ocr_language=profile.ocr.language,
            ocr_region=profile.ocr.region,
            ocr_stable_samples=profile.ocr.stable_samples,
            insert_pc_hooks=profile.insert_pc_hooks,
            hook_flush_ms=profile.hook_flush_ms,
            hook_codepage=profile.hook_codepage,
            hook_max_buffer=profile.hook_max_buffer,
            hook_max_history=profile.hook_max_history,
        )

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
        candidates = self.product.capture_view().candidates
        body_candidate = fixture_source(candidates, FIXTURE_BODY_HOOK_IDENTITY)
        speaker_candidate = fixture_source(candidates, FIXTURE_SPEAKER_HOOK_IDENTITY)
        if body_candidate is None or speaker_candidate is None:
            return False
        body_source = self.product.capture_source(
            body_candidate.identity, body_candidate.address, require_unique=False)
        speaker_source = self.product.capture_source(
            speaker_candidate.identity, speaker_candidate.address, require_unique=False)
        if body_source is None or speaker_source is None:
            return False
        from autoadv.model import StructureRule
        self.product.set_capture_extraction_preset(
            body_source.identity, body_source.address, 'body')
        self.product.save_capture_source_rule(
            speaker_source.identity, speaker_source.address, 0,
            StructureRule(FIXTURE_SPEAKER_RULE))
        body_preview = self.product.capture_candidate(body_source.identity, body_source.address)
        speaker_preview = self.product.capture_candidate(speaker_source.identity, speaker_source.address)
        if (body_preview is None
                or not any(sample.body == CALIBRATION for sample in body_preview.samples)
                or speaker_preview is None
                or not any(sample.body == CALIBRATION and bool(sample.speaker.strip())
                           for sample in speaker_preview.samples)):
            raise RuntimeError('preconfigured RenPy Hooks did not expose calibration body and speaker')
        self.product.set_capture_field_source('body', body_source.identity, body_source.address)
        self.product.set_capture_field_source('speaker', speaker_source.identity, speaker_source.address)
        saved = self.product.commit_capture()
        if (saved.body_source != FIXTURE_BODY_HOOK_IDENTITY
                or saved.speaker_source != FIXTURE_SPEAKER_HOOK_IDENTITY):
            raise RuntimeError('preconfigured RenPy body/speaker Hooks were not saved')
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
                    if self.options['phase'] in ('accuracy', 'mixed', 'stress'):
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
                    self.trace.emit(
                        'validation_collection_target', phase=self.options['phase'],
                        dialogues=int(self.options.get('dialogues', 0)),
                        force_advance_seconds=(
                            int(self.options.get('force_advance_seconds', 12))
                            if self.options['phase'] == 'accuracy' else None),
                        final_observation_seconds=self.final_observation_seconds)
                    self.collection_target_emitted = True
                if self.options['phase'] != 'hook':
                    self.w.permit()
                    if not self.product.runtime_state().authorized:
                        raise RuntimeError('AutoAdv not authorized to send')
                self._release('.monitoring')
                self.state = 'running'
            elif self.state == 'running':
                now = self.clock()
                if self.evidence_path is None:
                    # Non-accuracy phases retain only the minimum driver control
                    # needed to stop after their requested product dialogue count.
                    runtime = self.product.runtime_state()
                    target_count = int(self.options.get('dialogues', 0))
                    line_no = runtime.token.line if runtime.token is not None else 0
                    if target_count > 0 and line_no >= target_count:
                        if self.non_evidence_done_at is None:
                            self.non_evidence_done_at = now
                        elif now - self.non_evidence_done_at >= .6:
                            self.w.close()
                            self.qt_app.quit()
                    return
                cases = self._fixture_cases()
                if not cases:
                    return
                latest = cases[-1]
                case_id = str(latest.get('case') or '')
                if case_id != self.current_case:
                    self.current_case = case_id
                    self.current_case_seen_at = now
                runtime = self.product.runtime_state()
                self._send_authored_game_input(runtime, case_id, now)
                target_count = int(self.options.get('dialogues', 0))
                if target_count > 0 and len(cases) > target_count:
                    raise RuntimeError(
                        'fixture emitted additional case records after the requested single pass: '
                        f'observed={len(cases)} requested={target_count}')
                if target_count > 0 and len(cases) == target_count:
                    # The requested authored pass is a one-shot collection boundary.
                    # Later product/fixture activity must not move this boundary.
                    if self.collection_target_seen_at is None:
                        self.collection_target_seen_at = now
                    if now - self.collection_target_seen_at >= self.final_observation_seconds:
                        self.w.close()
                        self.qt_app.quit()
                    return
        except Exception as error:
            self.fail(str(error))


def install(window, app, trace):
    from PySide6.QtCore import QTimer
    driver = SessionDriver(window, app, load_session(), trace)
    timer = QTimer(window)
    timer.setInterval(150)
    timer.timeout.connect(driver.tick)
    timer.start()
    window._validation_driver = driver
    window._validation_timer = timer
    driver.tick()
