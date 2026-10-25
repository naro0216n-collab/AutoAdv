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
from autoadv.application import AutoAdvApplication
from autoadv.conversation import Target
from autoadv.game_windows import ProcessCandidate, WindowCandidate, find_game_processes, find_game_windows
from autoadv.hook_host import HookHost, parse_host_event
from autoadv.hook_host_worker import NativeHost, _full_string_hook_code, _luna_output_content, ipc_json
from autoadv.import_luna import inspect_luna, add_luna_games
from autoadv.luna_abi import load_exports, validate_abi
from autoadv.model import HookSelection, ReplacementRule, SpeakerStyle
from autoadv.running_target_dialog import available_targets
from autoadv.storage import Catalog, CommonSettings, load, save
from autoadv.voice import synthesize
from autoadv.windows_input import WindowsAdvancementInput, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP, key_message_parameters
from scripts import adopt_luna_release
from tests.g2p_support import TEST_G2P

ROOT = Path(__file__).resolve().parents[1]


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
    def __init__(self, *, results=(1, 1)):
        self.calls = []
        self.results = iter(results)
        self.MapVirtualKeyW = _Win32Function(lambda key, kind: 0x1c if key == 0x0d else 0x38)
        def post(hwnd, message, wparam, lparam):
            self.calls.append((hwnd.value, message, wparam, lparam))
            return next(self.results)
        self.PostMessageW = _Win32Function(post)


class _TargetUser32:
    def __init__(self, *, hwnd_valid=True, pid=101):
        self.IsWindow = _Win32Function(lambda hwnd: int(hwnd_valid))
        def get_pid(hwnd, out):
            out._obj.value = pid
            return 1 if hwnd_valid else 0
        self.GetWindowThreadProcessId = _Win32Function(get_pid)


def test_luna_boundary_preserves_provenance_content_and_upstream_abi():
    packet = dict(kind='output', host_generation='h', connection_generation=1,
                  sequence=2, received_ns=5, process_id=31,
                  source=dict(process_id=31, address=5, context=3, context2=4,
                              hook_code='AA', hook_name='text'), text='Hello', detail='')
    assert parse_host_event(packet).source.identity == 'AA|3|4'
    with pytest.raises(ValueError):
        parse_host_event({**packet, 'process_id': 32})

    for code in ('HQF0@1', 'EXHQXFN0@1', 'ENHQXFN0@1'):
        assert _full_string_hook_code(code)
        assert _luna_output_content(code, '話者\n本文\n') == '話者\n本文'
        assert _luna_output_content(code, '本文\n\n') == '本文\n'
        assert _luna_output_content(code, '  本文  \n') == '  本文  '
    assert _luna_output_content('HQF0@1', '\n') == '\n'
    assert _luna_output_content('HQ0@1', '本文\n') == '本文\n'

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

    # NativeHost delegates discovery/insertion to Luna while matching the
    # actual target process architecture and never taking over an existing
    # upstream injection.
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


def test_windows_input_targets_selected_hwnd_once_without_foreground_dependency():
    down, down_lparam, up, up_lparam = key_message_parameters(0x0D, 0x1C)
    assert (down, up) == (WM_KEYDOWN, WM_KEYUP)
    assert ((down_lparam >> 16) & 0xFF, (up_lparam >> 30) & 0x3) == (0x1C, 0x3)
    assert key_message_parameters(0x12, 0x38)[::2] == (WM_SYSKEYDOWN, WM_SYSKEYUP)

    fake = _TargetUser32(pid=101)
    with patch('autoadv.application.sys.platform', 'win32'), patch('autoadv.application.ctypes.windll', create=True) as dll:
        dll.user32 = fake
        assert AutoAdvApplication._windows_target_ready(Target(101, 77))
        assert not AutoAdvApplication._windows_target_ready(Target(202, 77))

    for results, expected_calls, raises in (((1, 1), 2, False), ((0,), 1, True), ((1, 0), 2, True)):
        fake = _User32(results=results)
        with patch('autoadv.windows_input.sys.platform', 'win32'), patch('autoadv.windows_input.ctypes.windll', create=True) as dll:
            dll.user32 = fake
            if raises:
                with pytest.raises(RuntimeError, match='no retry'):
                    WindowsAdvancementInput(77).advance()
            else:
                WindowsAdvancementInput(77).advance()
        assert len(fake.calls) == expected_calls


def test_settings_roundtrip_preserves_product_configuration_but_not_runtime_state():
    assert CommonSettings().uncertain_text_fallback
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'settings.json'
        c = Catalog(irodori_root='C:/tts', host_dll='C:/luna/LunaHost.dll')
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
        game.default_profile = p.id
        save(c, path)
        q = load(path).games[game.id].profiles[p.id]
        assert (q.hooks, q.body_source, q.speaker_source) == (p.hooks, p.body_source, p.speaker_source)
        assert (q.speaker_tts, q.speaker_voices, q.speaker_styles) == (
            p.speaker_tts, p.speaker_voices, p.speaker_styles)
        assert q.replacements == p.replacements and q.unwrap_speaker_enclosed_body
        assert (q.hook_flush_ms, q.hook_codepage, q.hook_max_buffer,
                q.hook_max_history, q.insert_pc_hooks) == (850, 932, 7500, 500000, True)
        assert 'pid' not in path.read_text(encoding='utf-8')
        raw = json.loads(path.read_text(encoding='utf-8'))
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
