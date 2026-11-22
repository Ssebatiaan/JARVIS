import ctypes as ct
import os
import unittest
from ctypes import wintypes
from time import monotonic, sleep
from unittest.mock import Mock

from jarvis.hotkey import (
    HOTKEY_ID,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    WM_HOTKEY,
    GlobalHotkey,
    HotkeyUnavailable,
)

try:
    import tkinter as tk
except ImportError:
    tk = None

VK_F24 = 0x87
TEST_MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_SHIFT


class HotkeyPollingTests(unittest.TestCase):
    def setUp(self):
        self.hotkey = GlobalHotkey.__new__(GlobalHotkey)
        self.hotkey.root = Mock()
        self.hotkey._registered = True
        self.hotkey._callback = Mock()
        self.hotkey._pressed = __import__("threading").Event()
        self.hotkey._after = None

    def test_each_press_runs_the_callback_once_on_the_window_thread(self):
        self.hotkey._poll()
        self.hotkey._callback.assert_not_called()
        self.hotkey._pressed.set()
        self.hotkey._poll()
        self.hotkey._poll()
        self.hotkey._callback.assert_called_once_with()
        self.assertEqual(self.hotkey.root.after.call_count, 3)

    def test_polling_stops_after_release(self):
        self.hotkey._registered = False
        self.hotkey._pressed.set()
        self.hotkey._poll()
        self.hotkey._callback.assert_not_called()
        self.hotkey.root.after.assert_not_called()


@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class HotkeyWindowsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def register(self, callback, hotkey_id=HOTKEY_ID + 1):
        hotkey = GlobalHotkey(self.root, callback, modifiers=TEST_MODIFIERS, key=VK_F24,
                              hotkey_id=hotkey_id)
        self.addCleanup(hotkey.close)
        return hotkey

    def test_windows_delivers_the_hotkey_message_to_the_callback(self):
        pressed = Mock()
        hotkey = self.register(pressed)
        self.assertTrue(hotkey.available)
        post = ct.WinDLL("user32.dll").PostThreadMessageW
        post.argtypes = [wintypes.DWORD, wintypes.UINT, ct.c_size_t, ct.c_ssize_t]
        self.assertTrue(post(hotkey.thread_id, WM_HOTKEY, hotkey._id, 0))
        deadline = monotonic() + 3
        while not pressed.called and monotonic() < deadline:
            self.root.update()
            sleep(0.01)
        pressed.assert_called_once_with()

    def test_a_taken_combination_is_reported_and_released_on_close(self):
        first = self.register(Mock())
        with self.assertRaises(HotkeyUnavailable):
            GlobalHotkey(self.root, Mock(), modifiers=TEST_MODIFIERS, key=VK_F24, hotkey_id=HOTKEY_ID + 2)
        first.close()
        self.assertFalse(first.available)
        second = self.register(Mock(), hotkey_id=HOTKEY_ID + 3)
        self.assertTrue(second.available)


if __name__ == "__main__":
    unittest.main()
