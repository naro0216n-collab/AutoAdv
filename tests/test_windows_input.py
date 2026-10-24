"""Targeted Windows input framing; no simulated game acceptance."""
import unittest
from unittest.mock import patch

from autoadv.application import AutoAdvApplication
from autoadv.conversation import Target
from autoadv.windows_input import (
    WindowsAdvancementInput, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP,
    key_message_parameters,
)


class _Win32Function:
    def __init__(self, fn):
        self.fn = fn
    def __call__(self, *args):
        return self.fn(*args)


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


class WindowsInputContract(unittest.TestCase):
    def test_key_messages_encode_scan_state_without_foreground_dependency(self):
        down, down_lparam, up, up_lparam = key_message_parameters(0x0D, 0x1C)
        self.assertEqual((down, up), (WM_KEYDOWN, WM_KEYUP))
        self.assertEqual(down_lparam & 0xFFFF, 1)
        self.assertEqual((down_lparam >> 16) & 0xFF, 0x1C)
        self.assertEqual((down_lparam >> 30) & 0x3, 0)
        self.assertEqual((up_lparam >> 30) & 0x3, 0x3)


    def test_target_readiness_checks_window_ownership_not_foreground(self):
        fake = _TargetUser32(pid=101)
        with patch('autoadv.application.sys.platform', 'win32'), \
             patch('autoadv.application.ctypes.windll', create=True) as dll:
            dll.user32 = fake
            self.assertTrue(AutoAdvApplication._windows_target_ready(Target(101, 77)))
            self.assertFalse(AutoAdvApplication._windows_target_ready(Target(202, 77)))

    def test_system_key_uses_system_message_pair(self):
        down, _, up, _ = key_message_parameters(0x12, 0x38)
        self.assertEqual((down, up), (WM_SYSKEYDOWN, WM_SYSKEYUP))

    def test_posts_one_down_and_one_up_to_selected_hwnd(self):
        fake = _User32()
        with patch('autoadv.windows_input.sys.platform', 'win32'), \
             patch('autoadv.windows_input.ctypes.windll', create=True) as dll:
            dll.user32 = fake
            WindowsAdvancementInput(77).advance()
        self.assertEqual([(c[0], c[1], c[2]) for c in fake.calls], [
            (77, WM_KEYDOWN, 0x0D), (77, WM_KEYUP, 0x0D)])

    def test_failed_down_is_not_retried_and_does_not_post_up(self):
        fake = _User32(results=(0,))
        with patch('autoadv.windows_input.sys.platform', 'win32'), \
             patch('autoadv.windows_input.ctypes.windll', create=True) as dll:
            dll.user32 = fake
            with self.assertRaisesRegex(RuntimeError, 'no retry'):
                WindowsAdvancementInput(77).advance()
        self.assertEqual(len(fake.calls), 1)

    def test_failed_up_is_not_retried(self):
        fake = _User32(results=(1, 0))
        with patch('autoadv.windows_input.sys.platform', 'win32'), \
             patch('autoadv.windows_input.ctypes.windll', create=True) as dll:
            dll.user32 = fake
            with self.assertRaisesRegex(RuntimeError, 'no retry'):
                WindowsAdvancementInput(77).advance()
        self.assertEqual([c[1] for c in fake.calls], [WM_KEYDOWN, WM_KEYUP])


if __name__ == '__main__':
    unittest.main()
