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

class FailClosedHookCrossCheck(unittest.TestCase):
    def test_missed_hook_is_detected_from_os_input_time(self):
        from autoadv.live_guard import InputOriginTracker
        tracker=InputOriginTracker(100)
        self.assertFalse(tracker.os_input_tick(100))
        self.assertTrue(tracker.os_input_tick(105))

    def test_tagged_automatic_event_can_update_last_input_time(self):
        from autoadv.live_guard import InputOriginTracker
        t=InputOriginTracker(100)
        t.hook_event(keyboard=True,flags=0x10,extra_info=AUTOADV_INPUT_MARK,tick=105)
        self.assertFalse(t.os_input_tick(105))
        self.assertFalse(t.os_input_tick(105))
        self.assertTrue(t.os_input_tick(109))

    def test_mouse_motion_and_other_window_input_do_not_stop_game(self):
        from autoadv.live_guard import InputOriginTracker
        t=InputOriginTracker(100)
        # Passive hook callbacks explicitly identify non-dialogue interaction.
        t.hook_event(keyboard=False,flags=0,extra_info=0,tick=101,
                     passive_mouse_motion=True)
        self.assertFalse(t.os_input_tick(101))
        t.hook_event(keyboard=True,flags=0,extra_info=0,tick=102,
                     passive_mouse_motion=True)
        self.assertFalse(t.os_input_tick(102))
        t.hook_event(keyboard=True,flags=0,extra_info=0,tick=103)
        self.assertTrue(t.os_input_tick(103))

    def test_manual_input_is_latched_even_if_automatic_follows(self):
        from autoadv.live_guard import InputOriginTracker
        t=InputOriginTracker(100)
        t.hook_event(keyboard=True,flags=0,extra_info=0,tick=101)
        t.hook_event(keyboard=True,flags=0x10,extra_info=AUTOADV_INPUT_MARK,tick=102)
        self.assertTrue(t.os_input_tick(102))
