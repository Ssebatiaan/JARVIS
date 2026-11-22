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

    def test_native_message_routes_only_events_for_jarvis_icon(self):
        tray = self.tray
        for icon, code in ((TRAY_ICON_ID, WM_LBUTTONUP), (42, WM_CONTEXTMENU),
                           (TRAY_ICON_ID, WM_CONTEXTMENU)):
            tray._window_proc(tray._window, TRAY_MESSAGE, 0, (icon << 16) | code, 0, 0)
        self.assertEqual(tray.root.after_idle.call_args_list, [
            call(tray._show), call(tray._context_menu),
        ])
        tray._version4 = False
        tray.root.after_idle.reset_mock()
        tray._window_proc(tray._window, TRAY_MESSAGE, TRAY_ICON_ID, WM_RBUTTONUP, 0, 0)
        tray.root.after_idle.assert_called_once_with(tray._context_menu)

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
