import ctypes
import unittest
from autoadv.windows_input import input_types


class WindowsABI(unittest.TestCase):
    def test_input_union_contains_full_mouse_layout(self):
        INPUT,UNION,KEYBOARD=input_types()
        if ctypes.sizeof(ctypes.c_void_p)==8:
            self.assertEqual(ctypes.sizeof(INPUT),40)
        else:
            self.assertEqual(ctypes.sizeof(INPUT),28)
        self.assertGreaterEqual(ctypes.sizeof(UNION),ctypes.sizeof(KEYBOARD))

if __name__=='__main__':unittest.main()