"""User-driven source discovery, independent of existing profile data."""
import os
import unittest
from unittest.mock import patch, Mock
from autoadv.game_windows import ProcessCandidate, WindowCandidate, find_game_windows, find_game_processes
from autoadv.running_target_dialog import available_targets


class RunningTargetDiscoveryTests(unittest.TestCase):
    def test_unregistered_visible_windows_are_available(self):
        w = WindowCandidate(24, 1234, '新しいゲーム', r'C:\Games\Novel.exe')
        with patch('autoadv.running_target_dialog.list_running_windows', return_value=(w,)):
            self.assertEqual(available_targets(), (w,))

    def test_background_processes_are_opt_in_and_have_no_input_window(self):
        w = WindowCandidate(24, 1234, 'ゲーム', r'C:\Games\Novel.exe')
        processes = (ProcessCandidate(24, w.executable), ProcessCandidate(88, r'C:\Games\Renderer.exe'))
        with patch('autoadv.running_target_dialog.list_running_windows', return_value=(w,)), \
             patch('autoadv.running_target_dialog.list_running_processes', return_value=processes):
            self.assertEqual(available_targets(False), (w,))
            self.assertEqual(available_targets(True), (w, WindowCandidate(88, 0, '', processes[1].executable)))

    def test_existing_executable_lookup_still_requires_exact_path(self):
        w = WindowCandidate(24, 1234, 'ゲーム', r'C:\Games\Novel.exe')
        with patch('autoadv.game_windows.list_running_windows', return_value=(w,)):
            self.assertEqual(find_game_windows(r'c:\games\NOVEL.exe'), (w,))
            self.assertEqual(find_game_windows(r'C:\Other\Novel.exe'), ())
        with patch('autoadv.game_windows.list_running_processes', return_value=(ProcessCandidate(24, w.executable),)):
            self.assertEqual(find_game_processes(r'c:\games\novel.exe'), (24,))

    @unittest.skipUnless(os.name == 'nt', 'real Windows process enumeration')
    def test_windows_lists_current_process_without_registered_executable(self):
        from autoadv.game_windows import list_running_processes
        matches = [process for process in list_running_processes() if process.pid == os.getpid()]
        self.assertEqual(len(matches), 1)
        self.assertTrue(matches[0].executable.lower().endswith(('.exe',)))

    def test_linux_has_no_fabricated_windows_processes(self):
        with patch('autoadv.game_windows.sys.platform', 'linux'):
            from autoadv.game_windows import list_running_processes, list_running_windows
            with self.assertRaises(OSError):
                list_running_processes()
            with self.assertRaises(OSError):
                list_running_windows()


@unittest.skipUnless(__import__('importlib').util.find_spec('PySide6'), 'Qt runtime required')
class RunningTargetGuiTests(unittest.TestCase):
    def test_picker_registers_unregistered_executable_and_recovers_it(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from types import SimpleNamespace
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        candidate = WindowCandidate(135, 9876, '作品タイトル', r'C:\Games\Novel.exe')

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            try:
                self.assertEqual(window.tabs.currentIndex(), 0)
                commands = Mock()
                starts = []
                def fake_host_start():
                    starts.append('host-start')
                    window._connecting_pid = 0
                    window.attached_pid = 0
                    window.hook_host = SimpleNamespace(
                        command=commands, child=SimpleNamespace(poll=lambda: None))
                window.start_host = fake_host_start
                window.use_running_target(candidate)
                self.assertEqual(window.tabs.currentIndex(), 1)
                commands.assert_any_call('connect', pid=candidate.pid)
                self.assertEqual(len(starts), 1)
                self.assertEqual(window.pid.value(), candidate.pid)
                self.assertEqual(window.hwnd.text(), hex(candidate.hwnd))
                self.assertEqual(window.catalog.games[window.game_id].executable, candidate.executable)
                gid = window.game_id
                commands.reset_mock()
                window.use_running_target(candidate)
                self.assertEqual(window.game_id, gid)
                self.assertEqual(len(window.catalog.games), 1)
                self.assertEqual(len(starts), 1)
                commands.assert_not_called()  # already connecting to this PID
                next_target = WindowCandidate(136, 9888, '作品タイトル', candidate.executable)
                window.use_running_target(next_target)
                self.assertEqual(len(starts), 2)
                commands.assert_any_call('connect', pid=next_target.pid)
                self.assertEqual(window._connecting_pid, next_target.pid)
                self.assertFalse(window.controller.authorized)
                # Delayed signals from an old worker must not change the new game.
                window._host_epoch = 4
                window.on_host_closed(3, 'old native Host exit')
                self.assertEqual(window._connecting_pid, next_target.pid)
                self.assertEqual(window.hook_host.child.poll(), None)
            finally:
                window.hook_host = None  # fake Host has no subprocess to close
                window.close()
            return 0

        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)
    def test_saved_window_selection_attaches_without_extra_clicks(self):
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        from PySide6.QtWidgets import QApplication
        from types import SimpleNamespace
        from autoadv.gui import run_gui
        from autoadv.storage import Catalog
        catalog = Catalog()
        catalog.create_game('Saved novel', r'C:\Games\Novel.exe')
        target = WindowCandidate(137, 9988, 'Saved novel', r'C:\Games\Novel.exe')

        def inspect(*_):
            window = next(w for w in QApplication.topLevelWidgets()
                          if getattr(w, 'catalog', None) is catalog)
            commands = Mock()
            starts = []
            try:
                def fake_host_start():
                    starts.append('start')
                    window.hook_host = SimpleNamespace(command=commands,
                        child=SimpleNamespace(poll=lambda: None))
                window.start_host = fake_host_start
                with patch('autoadv.gui.find_game_windows', return_value=(target,)):
                    window.detect_game()
                    commands.assert_any_call('connect', pid=137)
                    self.assertEqual(len(starts), 1)
                    commands.reset_mock()
                    window.detect_registered_game()
                    commands.assert_not_called()
                    self.assertEqual(len(starts), 1)
                    self.assertEqual(window.pid.value(), 137)
            finally:
                window.hook_host = None
                window.close()
            return 0
        with patch('autoadv.gui.load', return_value=catalog), patch('autoadv.gui.save'):
            with patch.object(QApplication, 'exec', inspect):
                self.assertEqual(run_gui(), 0)


if __name__ == '__main__':
    unittest.main()
