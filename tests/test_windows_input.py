"""Controller's real Windows input framing; no simulated game acceptance."""
import ctypes
import unittest
from unittest.mock import patch

from autoadv.windows_input import WindowsAdvancementInput, AUTOADV_INPUT_MARK


class _Win32Function:
    def __init__(self, fn):
        self.fn = fn
    def __call__(self, *args):
        return self.fn(*args)


class _User32:
    def __init__(self, *, press_count=1, release_count=1):
        self.calls = []
        self.GetForegroundWindow = _Win32Function(lambda: 77)
        self.MapVirtualKeyW = _Win32Function(lambda key, kind: 0x1c if key == 0x0d else 0)
        def send(count, data, size):
            self.calls.append((count, data[0].ki.wVk, data[0].ki.wScan,
                               data[0].ki.dwFlags, data[0].ki.dwExtraInfo))
            return press_count if len(self.calls) == 1 else release_count
        self.SendInput = _Win32Function(send)


class WindowsInputContract(unittest.TestCase):
    def test_scancode_key_hold_has_one_press_and_one_release(self):
        fake = _User32()
        with patch('autoadv.windows_input.sys.platform', 'win32'), \
             patch('autoadv.windows_input.ctypes.windll', create=True) as dll, \
             patch('autoadv.windows_input.time.sleep') as sleep:
            dll.user32 = fake
            WindowsAdvancementInput(77).advance()
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([c[3] for c in fake.calls], [0x08, 0x0a])
        self.assertEqual([c[2] for c in fake.calls], [0x1c, 0x1c])
        self.assertEqual([c[4] for c in fake.calls], [AUTOADV_INPUT_MARK] * 2)
        sleep.assert_called_once_with(0.035)

    def test_failed_press_does_not_retry_and_does_not_release(self):
        fake = _User32(press_count=0)
        with patch('autoadv.windows_input.sys.platform', 'win32'), \
             patch('autoadv.windows_input.ctypes.windll', create=True) as dll, \
             patch('autoadv.windows_input.time.sleep') as sleep:
            dll.user32 = fake
            with self.assertRaisesRegex(RuntimeError, 'no retry'):
                WindowsAdvancementInput(77).advance()
        self.assertEqual(len(fake.calls), 1)
        sleep.assert_not_called()

    def test_release_attempted_once_after_interrupted_hold(self):
        fake = _User32()
        with patch('autoadv.windows_input.sys.platform', 'win32'), \
             patch('autoadv.windows_input.ctypes.windll', create=True) as dll, \
             patch('autoadv.windows_input.time.sleep', side_effect=RuntimeError('interrupted')):
            dll.user32 = fake
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                WindowsAdvancementInput(77).advance()
        self.assertEqual([c[3] for c in fake.calls], [0x08, 0x0a])

    def test_foreground_is_required_before_press(self):
        fake = _User32()
        fake.GetForegroundWindow = _Win32Function(lambda: 99)
        with patch('autoadv.windows_input.sys.platform', 'win32'), \
             patch('autoadv.windows_input.ctypes.windll', create=True) as dll:
            dll.user32 = fake
            with self.assertRaisesRegex(RuntimeError, 'not foreground'):
                WindowsAdvancementInput(77).advance()
        self.assertEqual(fake.calls, [])


if __name__ == '__main__':
    unittest.main()
