import unittest
from autoadv.live_guard import is_manual_hook_event
from autoadv.windows_input import AUTOADV_INPUT_MARK, input_types

class InputOwnership(unittest.TestCase):
    def test_automatic_injection_must_be_tagged(self):
        for keyboard, injected in ((True,0x10),(False,0x01)):
            self.assertFalse(is_manual_hook_event(keyboard=keyboard,flags=injected,extra_info=AUTOADV_INPUT_MARK))
            self.assertTrue(is_manual_hook_event(keyboard=keyboard,flags=injected,extra_info=11))
            self.assertTrue(is_manual_hook_event(keyboard=keyboard,flags=0,extra_info=AUTOADV_INPUT_MARK))

    def test_full_win32_input_layout_keeps_marker(self):
        INPUT, U, KI = input_types()
        a=INPUT(1,U(ki=KI(13,0,0,0,AUTOADV_INPUT_MARK)))
        self.assertEqual(a.ki.dwExtraInfo,AUTOADV_INPUT_MARK)

if __name__=='__main__':unittest.main()
