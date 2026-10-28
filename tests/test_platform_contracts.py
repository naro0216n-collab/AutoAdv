"""Narrow contracts at external runtime, persistence, Luna, GUI, and Windows boundaries."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import wave
import zipfile

import pytest

from autoadv import luna_runtime, native_runtime
from autoadv.audio_buffer import AudioHistory
from autoadv.application import AutoAdvApplication
from autoadv.conversation import Target
from autoadv.game_windows import ProcessCandidate, WindowCandidate, find_game_processes, find_game_windows
from autoadv.hook_host import HookHost, parse_host_event
from autoadv.hook_host_worker import NativeHost, ipc_json
from autoadv.import_luna import inspect_luna, add_luna_games
from autoadv.luna_abi import ThreadParam, load_exports, validate_abi
from autoadv.model import HookSelection, InputMethod, ReplacementRule, SpeakerStyle
from autoadv.process_audio import ProcessCapture
from autoadv.running_target_dialog import available_targets
from autoadv.storage import Catalog, CommonSettings, load, save
from autoadv.voice import synthesize
from autoadv.windows_input import (
    AUTOADV_INPUT_MARKER, INPUT_FOREGROUND_REQUIRED, LLKHF_INJECTED,
    WindowsAdvancementInput, WindowsGameInputObserver, WM_KEYDOWN, WM_KEYUP,
    WM_SYSKEYDOWN, WM_SYSKEYUP, key_message_parameters,
)
from scripts import adopt_luna_release, prepare_windows_runtime
from tests.g2p_support import TEST_G2P

ROOT = Path(__file__).resolve().parents[1]


def test_process_capture_preserves_helper_timeline_when_python_reader_is_delayed(monkeypatch, tmp_path):
    import autoadv.process_audio as process_audio_module

    packet = b''.join(
        process_audio_module.HEADER.pack(8, helper_ms, 1) + (b'\x00' * 8)
        for helper_ms in (5_000, 5_100, 7_500))

    class HelperProcess:
        def __init__(self):
            self.stdout = io.BytesIO(packet)
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

    class InlineThread:
        def __init__(self, *, target, daemon, name):
            self.target = target

        def start(self):
            self.target()

        def join(self, timeout=None):
            pass

    helper = tmp_path / 'process-loopback.dll'
    helper.write_bytes(b'helper')
    process = HelperProcess()
    monkeypatch.setattr(process_audio_module.sys, 'platform', 'win32')
    monkeypatch.setattr(process_audio_module.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)
    monkeypatch.setattr(process_audio_module.subprocess, 'Popen', lambda *args, **kwargs: process)
    monkeypatch.setattr(process_audio_module, 'Thread', InlineThread)

    chunks = []
    readings = iter((10_000, 25_000, 40_000))
    capture = ProcessCapture(
        42, helper, AudioHistory(), chunks.append, lambda _status: None,
        clock=lambda: next(readings))
    capture.start()
    try:
        # The first packet establishes the clock-domain offset. Later Python
        # receive latency cannot compress or stretch the helper chronology.
        assert [chunk.at_ms for chunk in chunks] == [10_000, 10_100, 12_500]
    finally:
        capture.close()


def test_irodori_request_omits_unset_caption_and_sends_explicit_caption():
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
        wav.writeframes(b'\x00\x00' * 8)
    audio = output.getvalue()
    requests = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read(self): return audio

    def open_request(req, timeout):
        requests.append((req, timeout))
        return Response()

    with patch('autoadv.voice.request.urlopen', side_effect=open_request):
        synthesize('http://127.0.0.1:8088', '本文', 'voice-a')
        synthesize('http://127.0.0.1:8088', '本文', 'voice-a', '柔らかい声。')

    first = json.loads(requests[0][0].data)
    second = json.loads(requests[1][0].data)
    assert 'irodori' not in first
    assert second['irodori'] == {'caption': '柔らかい声。'}


class _Win32Function:
    def __init__(self, fn): self.fn = fn
    def __call__(self, *args): return self.fn(*args)


class _User32:
    def __init__(self, *, results=(1, 1), pid=101, foreground=77, send_result=2):
        self.calls = []
        self.results = iter(results)
        self.pid = pid
        self.foreground = foreground
        self.send_result = send_result
        self.send_calls = []
        self.IsWindow = _Win32Function(lambda hwnd: int(hwnd.value == 77))
        self.GetAncestor = _Win32Function(lambda hwnd, _kind: hwnd.value)
        self.GetForegroundWindow = _Win32Function(lambda: self.foreground)
        def get_pid(hwnd, out):
            out._obj.value = self.pid if hwnd.value == 77 else 999
            return 1
        self.GetWindowThreadProcessId = _Win32Function(get_pid)
        self.MapVirtualKeyW = _Win32Function(lambda key, kind: 0x1c if key == 0x0d else 0x38)
        def post(hwnd, message, wparam, lparam):
            self.calls.append((hwnd.value, message, wparam, lparam))
            return next(self.results)
        self.PostMessageW = _Win32Function(post)
        def send(count, inputs, size):
            self.send_calls.append((count, inputs, size))
            return self.send_result
        self.SendInput = _Win32Function(send)


class _TargetUser32:
    def __init__(self, *, hwnd_valid=True, pid=101):
        self.IsWindow = _Win32Function(lambda hwnd: int(hwnd_valid))
        self.GetAncestor = _Win32Function(lambda hwnd, _kind: hwnd.value if hwnd_valid else 0)
        def get_pid(hwnd, out):
            out._obj.value = pid
            return 1 if hwnd_valid else 0
        self.GetWindowThreadProcessId = _Win32Function(get_pid)


def test_luna_event_boundary_preserves_process_and_source_provenance():
    packet = dict(kind='output', host_generation='h', connection_generation=1,
                  sequence=2, received_ns=5, process_id=31,
                  source=dict(process_id=31, address=5, context=3, context2=4,
                              hook_code='AA', hook_name='text'), text='Hello', detail='')
    assert parse_host_event(packet).source.identity == 'AA|3|4'
    with pytest.raises(ValueError):
        parse_host_event({**packet, 'process_id': 32})


def test_native_luna_output_removes_only_the_upstream_full_string_separator():
    packets = []
    host = NativeHost.__new__(NativeHost)
    host.send = packets.append
    host.generation = 'host'
    host.connection_generation = 1
    host.sequence = 0
    host.lock = __import__('threading').Lock()
    host.callbacks = []
    host.connected = set()
    host.managed = set()
    host.find_callbacks = []
    host.dll = SimpleNamespace(Luna_Start=Mock(), Luna_Settings=Mock())
    host.start()
    output = host.dll.Luna_Start.call_args.args[4]
    thread = ThreadParam(31, 5, 3, 4)
    for code, text, expected in (
        ('HQF0@1', '話者\n本文\n', '話者\n本文'),
        ('EXHQXFN0@1', '本文\n\n', '本文\n'),
        ('ENHQXFN0@1', '  本文  \n', '  本文  '),
        ('HQF0@1', '\n', '\n'),
        ('HQ0@1', '本文\n', '本文\n'),
    ):
        output(code, b'text', thread, text)
        assert packets[-1]['text'] == expected


def test_luna_ipc_and_native_exports_follow_the_pinned_upstream_abi():
    message = {'kind': 'diagnostic', 'detail': '已经注入—日本語🎮'}
    encoded = ipc_json(message)
    assert json.loads(encoded) == message
    assert encoded.encode('cp932').decode('cp932') == encoded
    validate_abi()

    dll = SimpleNamespace(**{name: Mock() for name in (
        'Luna_Start', 'Luna_ConnectProcess', 'Luna_DetachProcess',
        'Luna_CheckIfNeedInject', 'Luna_InsertHookCode', 'Luna_InsertPCHooks',
        'Luna_RemoveHook', 'Luna_Settings', 'Luna_FindHooks', 'Luna_SyncThread')})
    load_exports(dll)
    assert len(dll.Luna_InsertPCHooks.argtypes) == 2


def test_native_host_delegates_attach_discovery_and_pc_hook_insertion_to_luna():
    host = NativeHost.__new__(NativeHost)
    host.managed = set()
    host.connected = set()
    host.dll = SimpleNamespace(Luna_CheckIfNeedInject=Mock(return_value=False),
                               Luna_ConnectProcess=Mock())
    host.emit = Mock()
    assert host.command({'action': 'connect', 'pid': 31})
    host.dll.Luna_ConnectProcess.assert_not_called()
    assert host.managed == set()

    host = NativeHost.__new__(NativeHost)
    host.connected = {31}
    host.codepage = 932
    host.find_callbacks = []
    host.dll = SimpleNamespace(Luna_FindHooks=Mock(), Luna_InsertPCHooks=Mock())
    host.emit = Mock()
    for target_64bit, expected, max_address in (
            (False, b'\x55\x8b\xec', 0xffffffff),
            (True, b'\xcc\xcc\x48\x89', 0xffffffffffffffff)):
        with patch('autoadv.hook_host_worker.is_process_64bit', return_value=target_64bit):
            assert host.command({'action': 'find_hooks', 'pid': 31,
                                 'boundary_module': 'Game.exe', 'parameters': {}, 'addresses': ''})
            _pid, search, _callback, _addresses = host.dll.Luna_FindHooks.call_args.args
            assert bytes(search.pattern[:search.length]) == expected
            assert search.maxAddress == max_address and search.codepage == 932
            assert search.boundaryModule == 'Game.exe'
    assert host.command({'action': 'insert_pc_hooks', 'pid': 31})
    assert host.dll.Luna_InsertPCHooks.call_args_list == [
        __import__('unittest').mock.call(31, 0), __import__('unittest').mock.call(31, 1)]

def test_luna_runtime_is_pinned_integrity_checked_and_architecture_matched():
    root = ROOT / 'vendor' / 'luna'
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['revision'] == luna_runtime.COMMIT and manifest['file_count'] > 900
    for name in ('src/NativeImpl/LunaHook/LunaHost/LunaHostDll.cpp', 'LICENSE'):
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == manifest['sha256'][name]
    luna_runtime.adopted_runtime().verify()

    with tempfile.TemporaryDirectory() as temp:
        destination = Path(temp) / 'luna_assets'
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            for name in luna_runtime.FILES:
                z.writestr('files/' + name, b'dummy-' + name.encode('ascii'))
        with pytest.raises(ValueError):
            adopt_luna_release.adopt(archive.getvalue(), destination)
        assert not destination.exists()

        expected = hashlib.sha256(archive.getvalue()).hexdigest()
        with patch.object(adopt_luna_release, 'ZIP_SHA256', expected):
            adopt_luna_release.adopt(archive.getvalue(), destination)
            luna_runtime.LunaRuntime(destination).verify()

    with tempfile.TemporaryDirectory() as temp:
        runtime = luna_runtime.LunaRuntime(Path(temp))
        class Completed: returncode = 1
        with patch.object(luna_runtime, 'is_process_64bit', return_value=False), \
             patch('subprocess.run', return_value=Completed()) as run:
            luna_runtime.inject(123, runtime)
            args = run.call_args.args[0]
            assert args[0].endswith('LunaSubProcess32.exe') and args[3].endswith('LunaHook32.dll')


def test_windows_input_targets_selected_hwnd_once_without_foreground_dependency(monkeypatch):
    from tests.contracts_support import connect_hook, runtime_hook, source

    down, down_lparam, up, up_lparam = key_message_parameters(0x0D, 0x1C)
    assert (down, up) == (WM_KEYDOWN, WM_KEYUP)
    assert ((down_lparam >> 16) & 0xFF, (up_lparam >> 30) & 0x3) == (0x1C, 0x3)
    assert key_message_parameters(0x12, 0x38)[::2] == (WM_SYSKEYDOWN, WM_SYSKEYUP)

    def advance_with_window_owner(target_pid, window_pid):
        sent = []
        catalog = Catalog()
        game = catalog.create_game('Game')
        app = AutoAdvApplication(
            catalog, lambda: None,
            input_send=lambda target, method, key: sent.append((target.pid, target.hwnd, method, key)),
            input_unavailable=(
                lambda target, _method: None if target.pid == window_pid else 'target_unavailable'),
            g2p=TEST_G2P,
        )
        app.select_profile(game.id, game.default_profile)
        selected = source('WINDOW', 1, pid=target_pid)
        profile = app.current_profile()
        profile.hooks = [HookSelection('WINDOW', 1, 2)]
        profile.body_source = selected.identity
        scheduled = []
        app.configure_runtime_loop(schedule=lambda delay, callback: scheduled.append((delay, callback)))
        connect_hook(app, pid=target_pid, hwnd=77)
        token = runtime_hook(app, selected, '本文', 2, pid=target_pid)
        assert token is not None
        app.set_auto_advance(True)
        scheduled.pop(0)[1]()
        return sent, app.runtime_state()

    class NoopInputObserver:
        def __init__(self, *_args, **_kwargs): pass
        def start(self): pass
        def close(self): pass

    monkeypatch.setattr('autoadv.application.sys.platform', 'win32')
    monkeypatch.setattr('autoadv.application.WindowsGameInputObserver', NoopInputObserver)
    sent, state = advance_with_window_owner(101, 101)
    assert sent == [(101, 77, InputMethod.WINDOW_MESSAGE, 13)] and state.sent
    sent, state = advance_with_window_owner(202, 101)
    assert sent == [] and state.pause_reason == 'target_unavailable'

    for results, expected_calls, raises in (((1, 1), 2, False), ((0,), 1, True), ((1, 0), 2, True)):
        fake = _User32(results=results)
        with patch('autoadv.windows_input.sys.platform', 'win32'), patch('autoadv.windows_input.ctypes.windll', create=True) as dll:
            dll.user32 = fake
            if raises:
                with pytest.raises(RuntimeError, match='no retry'):
                    WindowsAdvancementInput(101, 77).advance()
            else:
                WindowsAdvancementInput(101, 77).advance()
        assert len(fake.calls) == expected_calls

    # Window-targeted input remains available without foreground focus. The
    # system-keyboard method requires the selected game window to be foreground
    # and tags both injected events so AutoAdv's observer can ignore only itself.
    fake = _User32(foreground=88)
    with patch('autoadv.windows_input.sys.platform', 'win32'), patch('autoadv.windows_input.ctypes.windll', create=True) as dll:
        dll.user32 = fake
        assert WindowsAdvancementInput.availability_reason(
            101, 77, InputMethod.WINDOW_MESSAGE) is None
        assert WindowsAdvancementInput.availability_reason(
            101, 77, InputMethod.SEND_INPUT) == INPUT_FOREGROUND_REQUIRED
        fake.foreground = 77
        WindowsAdvancementInput(101, 77, method=InputMethod.SEND_INPUT).advance()
        count, inputs, _ = fake.send_calls[-1]
        assert count == 2
        assert inputs[0].ki.dwExtraInfo == AUTOADV_INPUT_MARKER
        assert inputs[1].ki.dwExtraInfo == AUTOADV_INPUT_MARKER


def test_windows_game_input_observer_relay_preserves_target_and_callback_boundary(monkeypatch):
    import autoadv.windows_input as windows_input_module
    import ctypes

    class HookUser32(_TargetUser32):
        def __init__(self, pid):
            super().__init__(pid=pid)
            self.pid = pid
            self.keyboard_proc = self.mouse_proc = None
            self.in_hook_callback = False
            self._sent_inputs = False
            self.queue = []
            self.WindowFromPoint = _Win32Function(self._from_point)
            self.GetWindowThreadProcessId = _Win32Function(self._get_pid)
            self.SetWindowsHookExW = _Win32Function(self._set_hook)
            self.CallNextHookEx = _Win32Function(lambda *_: 0)
            self.UnhookWindowsHookEx = _Win32Function(lambda *_: 1)
            self.GetForegroundWindow = _Win32Function(lambda: 77)
            self.GetMessageW = _Win32Function(self._message)
            self.PeekMessageW = _Win32Function(lambda *_: 0)
            self.PostThreadMessageW = _Win32Function(self._post)
            self.TranslateMessage = _Win32Function(lambda *_: 1)
            self.DispatchMessageW = _Win32Function(lambda *_: 1)

        def _set_hook(self, kind, callback, _module, _thread):
            if kind == windows_input_module.WH_KEYBOARD_LL:
                self.keyboard_proc = callback
            else:
                self.mouse_proc = callback
            return kind

        def _get_pid(self, hwnd, out):
            assert not self.in_hook_callback
            out._obj.value = self.pid if hwnd.value == 77 else 999
            return 1

        def _from_point(self, point):
            assert not self.in_hook_callback  # WindowFromPoint sends WM_NCHITTEST
            return 77 if point.x == 10 else 88  # game vs. AutoAdv

        def _post(self, _thread, message, _wparam, _lparam):
            self.queue.append(message)
            return 1

        def _message(self, message, *_args):
            if not self._sent_inputs:
                self._sent_inputs = True
                # Both target and unrelated clicks hit the global hook; only
                # target input may be admitted after leaving the Win32 callback.
                mouse_buffer = ctypes.create_string_buffer(64)
                struct = self.observer_mouse_struct
                for point in (10, 20):
                    packet = struct.from_buffer(mouse_buffer)
                    packet.pt.x, packet.pt.y = point, 0
                    self.in_hook_callback = True
                    self.mouse_proc(0, windows_input_module.WM_LBUTTONDOWN,
                                    ctypes.addressof(mouse_buffer))
                    self.in_hook_callback = False
                keyboard_buffer = ctypes.create_string_buffer(64)
                keyboard_struct = self.observer_keyboard_struct
                for flags, extra in (
                        (LLKHF_INJECTED, AUTOADV_INPUT_MARKER),
                        (LLKHF_INJECTED, 12345),
                        (0, 0)):
                    packet = keyboard_struct.from_buffer(keyboard_buffer)
                    packet.flags, packet.dwExtraInfo = flags, extra
                    self.in_hook_callback = True
                    self.keyboard_proc(0, WM_KEYDOWN, ctypes.addressof(keyboard_buffer))
                    self.in_hook_callback = False
            if self.queue:
                message._obj.message = self.queue.pop(0)
                return 1
            return 0

    class Kernel32:
        def __init__(self):
            self.GetCurrentThreadId = _Win32Function(lambda: 1)
            self.GetModuleHandleW = _Win32Function(lambda _name: 1)

    # ctypes.Structure is normally local to the Windows hook worker; match
    # the native POINT and MSLLHOOKSTRUCT layout for a 64-bit host.
    from ctypes import wintypes
    class POINT(ctypes.Structure):
        _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]
    class MOUSE(ctypes.Structure):
        _fields_ = [('pt', POINT), ('mouseData', wintypes.DWORD),
                    ('flags', wintypes.DWORD), ('time', wintypes.DWORD),
                    ('dwExtraInfo', ctypes.c_size_t)]
    class KBD(ctypes.Structure):
        _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD),
                    ('flags', wintypes.DWORD), ('time', wintypes.DWORD),
                    ('dwExtraInfo', ctypes.c_size_t)]

    def observed(expected_pid, actual_pid):
        events = []
        user32 = HookUser32(actual_pid)
        user32.observer_mouse_struct = MOUSE
        user32.observer_keyboard_struct = KBD
        with patch.object(windows_input_module.sys, 'platform', 'win32'), \
             patch.object(windows_input_module.ctypes, 'windll',
                          SimpleNamespace(user32=user32, kernel32=Kernel32()), create=True), \
             patch.object(windows_input_module.ctypes, 'WINFUNCTYPE',
                          lambda *_types: (lambda fn: fn), create=True):
            observer = WindowsGameInputObserver(expected_pid, events.append, clock=lambda: 4321)
            observer.start()
            observer.close()
        return events

    assert observed(101, 101) == [4321, 4321, 4321]  # click + other injected key + physical key
    assert observed(202, 101) == []


def test_settings_roundtrip_preserves_product_configuration_but_not_runtime_state():
    assert CommonSettings().uncertain_text_fallback
    assert CommonSettings().force_advance_seconds == 30
    assert CommonSettings().input_method == InputMethod.WINDOW_MESSAGE
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'settings.json'
        c = Catalog(irodori_root='C:/tts', host_dll='C:/luna/LunaHost.dll')
        c.common.input_method = InputMethod.SEND_INPUT
        game = c.create_game('ゲーム1', 'C:/games/a.exe')
        c.create_game('ゲーム2', 'C:/games/b.exe')
        p = c.add_profile(game.id, 'Route B')
        p.hooks = [HookSelection('HX', 1, 2), HookSelection('HY', 5, 6)]
        p.body_source, p.speaker_source = p.hooks[0].identity, p.hooks[1].identity
        p.unwrap_speaker_enclosed_body = True
        p.hook_flush_ms = 850
        p.hook_codepage = 932
        p.hook_max_buffer = 7500
        p.hook_max_history = 500000
        p.insert_pc_hooks = True
        p.speaker_tts = {'大和': True}
        p.speaker_voices = {'大和': 'voice-m'}
        p.speaker_styles = {'大和': SpeakerStyle(age='young', timbre='soft', pace='slightly_slow')}
        p.replacements = [ReplacementRule('東京', 'とうきょう')]
        p.input_method = InputMethod.SEND_INPUT
        game.default_profile = p.id
        save(c, path)
        loaded = load(path)
        q = loaded.games[game.id].profiles[p.id]
        assert loaded.common.input_method == InputMethod.SEND_INPUT
        assert q.input_method == InputMethod.SEND_INPUT
        assert (q.hooks, q.body_source, q.speaker_source) == (p.hooks, p.body_source, p.speaker_source)
        assert (q.speaker_tts, q.speaker_voices, q.speaker_styles) == (
            p.speaker_tts, p.speaker_voices, p.speaker_styles)
        assert q.replacements == p.replacements and q.unwrap_speaker_enclosed_body
        assert (q.hook_flush_ms, q.hook_codepage, q.hook_max_buffer,
                q.hook_max_history, q.insert_pc_hooks) == (850, 932, 7500, 500000, True)
        assert 'pid' not in path.read_text(encoding='utf-8')
        raw = json.loads(path.read_text(encoding='utf-8'))
        assert 'next_line_timeout_ms' not in raw['common']
        assert 'next_line_timeout_ms' not in raw['games'][game.id]['profiles'][p.id]
        assert raw['common']['force_advance_seconds'] == 30
        assert raw['games'][game.id]['profiles'][p.id]['force_advance_seconds'] == 30
        path.write_text(json.dumps({**raw, 'schema_version': 3}), encoding='utf-8')
        with pytest.raises(ValueError, match='unsupported settings schema'):
            load(path)


def test_running_target_discovery_uses_real_windows_and_exact_executable_identity():
    w = WindowCandidate(24, 1234, 'ゲーム', r'C:\Games\Novel.exe')
    processes = (ProcessCandidate(24, w.executable), ProcessCandidate(88, r'C:\Games\Renderer.exe'))
    with patch('autoadv.running_target_dialog.list_running_windows', return_value=(w,)), \
         patch('autoadv.running_target_dialog.list_running_processes', return_value=processes):
        assert available_targets(False) == (w,)
        assert available_targets(True) == (w, WindowCandidate(88, 0, '', processes[1].executable))
    with patch('autoadv.game_windows.list_running_windows', return_value=(w,)):
        assert find_game_windows(r'c:\games\NOVEL.exe') == (w,)
        assert find_game_windows(r'C:\Other\Novel.exe') == ()
    with patch('autoadv.game_windows.sys.platform', 'linux'):
        from autoadv.game_windows import list_running_processes, list_running_windows
        with pytest.raises(OSError): list_running_processes()
        with pytest.raises(OSError): list_running_windows()


def test_managed_runtime_machine_output_is_independent_of_child_diagnostic_encoding():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        python = root / "python.exe"
        machine = {"native": "ready"}
        completed = SimpleNamespace(
            stdout=b"\x83diagnostic\n" + json.dumps(machine).encode("utf-8") + b"\n",
            stderr=b"",
        )
        with patch.object(prepare_windows_runtime, "runtime_root", return_value=root / "runtime"), \
             patch.object(prepare_windows_runtime, "ensure_environment", return_value=python), \
             patch.object(prepare_windows_runtime.subprocess, "run", return_value=completed):
            result = prepare_windows_runtime.prepare(root, require_native=True)
        assert result["native"] == machine


def test_managed_native_alignment_asset_is_verified_before_use():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = native_runtime.managed_paths(runtime_root=root)
        model = Path(paths.alignment_model)
        model.parent.mkdir(parents=True)
        payload = b'model'
        model.write_bytes(payload)
        expected = native_runtime.hashlib.sha256(payload).hexdigest()
        with patch.object(native_runtime, 'ALIGNMENT_MODEL_SHA256', expected), \
             patch.object(native_runtime, 'ALIGNMENT_MODEL_BYTES', len(payload)):
            result = native_runtime.prepare_alignment_model(runtime_root=root, allow_download=False)
        assert result['model']['source'] == 'existing'
        assert result['license'] == 'Apache-2.0'
        assert Path(result['notice']).is_file()
        assert json.loads(Path(paths.alignment_vocab).read_text()) == native_runtime.ALIGNMENT_VOCAB

        vocal_model = Path(paths.human_vocal_model)
        vocal_model.parent.mkdir(parents=True)
        vocal_payload = b'human-vocal-model'
        vocal_model.write_bytes(vocal_payload)
        vocal_sha = native_runtime.hashlib.sha256(vocal_payload).hexdigest()
        with patch.object(native_runtime, 'HUMAN_VOCAL_MODEL_SHA256', vocal_sha), \
             patch.object(native_runtime, 'HUMAN_VOCAL_MODEL_BYTES', len(vocal_payload)):
            vocal = native_runtime.prepare_human_vocal_model(
                runtime_root=root, allow_download=False)
        assert vocal['model']['source'] == 'existing'
        assert vocal['license'] == 'Apache-2.0'
        assert Path(vocal['notice']).is_file()


@pytest.mark.skipif(importlib.util.find_spec('PySide6') is None, reason='Qt runtime required')
def test_gui_can_construct_without_game_or_external_server():
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtWidgets import QApplication
    from autoadv.gui import run_gui
    with patch('autoadv.gui.load', return_value=Catalog()), \
         patch('autoadv.application.default_g2p', return_value=TEST_G2P), \
         patch.object(QApplication, 'exec', return_value=0):
        assert run_gui() == 0


def test_luna_import_is_explicit_read_only_and_maps_only_supported_product_settings():
    payload = [
        ['uid-1'],
        {'uid-1': {
            'gamepath': 'C:/Games/Novel.exe', 'title': 'Novel',
            'hook': [[0, 0, 1, 2, 'name', 'HQ@123']],
            'needinserthookcode': ['HS@456'],
            'hooksetting_private': {
                'textthreaddelay': 850, 'codepage_value': 932,
                'maxBufferSize': 7500, 'maxHistorySize': 500000,
            },
            'tts_follow_default': False, 'tts_repair': True,
            'tts_repair_regex': [{'key': '東京', 'value': 'とうきょう'}],
        }},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'savegamedata.json'
        encoded = json.dumps(payload, ensure_ascii=False)
        path.write_text(encoded, encoding='utf-8')
        games = inspect_luna(path)
        assert path.read_text(encoding='utf-8') == encoded

    original = Catalog()
    imported = add_luna_games(original, games)
    assert original.games == {} and original.luna_imports == {}
    game = next(iter(imported.games.values()))
    p = game.profiles[game.default_profile]
    assert p.body_source == 'HQ@123|1|2' and p.inserted_hooks == ['HS@456']
    assert (p.hook_flush_ms, p.hook_codepage, p.hook_max_buffer, p.hook_max_history) == (850, 932, 7500, 500000)
    assert p.replacements[0].target == 'tts'
    with pytest.raises(ValueError, match='already imported'):
        add_luna_games(imported, games)
