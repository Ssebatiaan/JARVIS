import unittest
from unittest.mock import Mock, call

from jarvis.tray import (
    MENU_EXIT,
    MENU_OPEN,
    NIM_SETFOCUS,
    TRAY_ICON_ID,
    TRAY_MESSAGE,
    WM_CONTEXTMENU,
    WM_LBUTTONUP,
    WM_RBUTTONUP,
    NotifyIconData,
    SystemTray,
)


class TrayTests(unittest.TestCase):
    def setUp(self):
        self.tray = SystemTray.__new__(SystemTray)
        self.tray.root = Mock()
        self.tray.available = True
        self.tray._attached = True
        self.tray._version4 = True
        self.tray._menu_open = False
        self.tray._taskbar_created = 40000
        self.tray._window = 11
        self.tray._data = NotifyIconData()
        self.tray._pending = []
        self.tray._poll_after = None

    def test_native_message_routes_only_events_for_jarvis_icon(self):
        tray = self.tray
        for icon, code in ((TRAY_ICON_ID, WM_LBUTTONUP), (42, WM_CONTEXTMENU),
                           (TRAY_ICON_ID, WM_CONTEXTMENU)):
            tray._window_proc(tray._window, TRAY_MESSAGE, 0, (icon << 16) | code, 0, 0)
        self.assertEqual(tray._pending, [tray._show, tray._context_menu])
        tray.root.after_idle.assert_not_called()
        tray._version4 = False
        tray._pending.clear()
        tray._window_proc(tray._window, TRAY_MESSAGE, TRAY_ICON_ID, WM_RBUTTONUP, 0, 0)
        tray._window_proc(tray._window, TRAY_MESSAGE, TRAY_ICON_ID, WM_RBUTTONUP, 0, 0)
        self.assertEqual(tray._pending, [tray._context_menu])

    def test_queued_actions_run_on_the_window_loop(self):
        tray = self.tray
        action = Mock()
        tray._queue(action)
        tray._poll()
        action.assert_called_once_with()
        self.assertEqual(tray._pending, [])
        tray.root.after.assert_called_once()

    def test_native_menu_offers_open_and_exit_and_returns_focus_on_dismissal(self):
        tray = self.tray
        tray._cursor = Mock(return_value=True)
        tray._create_menu = Mock(return_value=21)
        tray._append_menu = Mock(return_value=True)
        tray._foreground = Mock()
        tray._track_menu = Mock(side_effect=[MENU_OPEN, MENU_EXIT, 0])
        tray._post = Mock()
        tray._destroy_menu = Mock()
        tray._show_window = Mock()
        tray._exit_window = Mock()
        tray._notify = Mock()

        tray._context_menu()
        tray._context_menu()
        tray._context_menu()
        self.assertEqual(tray._append_menu.call_args_list[:2], [
            call(21, 0, MENU_OPEN, "Abrir JARVIS"),
            call(21, 0, MENU_EXIT, "Salir"),
        ])
        tray._show_window.assert_called_once_with()
        tray._exit_window.assert_called_once_with()
        self.assertEqual(tray._destroy_menu.call_args_list, [call(21)] * 3)
        self.assertEqual(tray._notify.call_args.args[0], NIM_SETFOCUS)
        self.assertFalse(tray._menu_open)


class TrayWindowsTests(unittest.TestCase):
    """Envía un mensaje real de Windows al icono: antes, atenderlo cerraba Python."""

    def test_a_real_click_message_reaches_the_window_without_crashing(self):
        import ctypes as ct
        import os
        from ctypes import wintypes
        from time import monotonic, sleep

        if os.name != "nt":
            self.skipTest("Requiere Windows")
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        root.withdraw()
        self.addCleanup(__import__("gc").collect)
        self.addCleanup(root.destroy)
        shown = Mock()
        try:
            tray = SystemTray(root, shown, Mock(), Mock())
        except OSError as error:
            self.skipTest(f"Bandeja no disponible: {error}")
        self.addCleanup(tray.close)
        post = ct.WinDLL("user32.dll").PostMessageW
        post.argtypes = [wintypes.HWND, wintypes.UINT, ct.c_size_t, ct.c_ssize_t]
        post(tray._window, TRAY_MESSAGE, 0, (TRAY_ICON_ID << 16) | WM_LBUTTONUP)
        deadline = monotonic() + 3
        while not shown.called and monotonic() < deadline:
            root.update()
            sleep(0.01)
        shown.assert_called_once_with()
