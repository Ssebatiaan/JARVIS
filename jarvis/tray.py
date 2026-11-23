import ctypes as ct
from collections.abc import Callable
from ctypes import wintypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tkinter import Tk

WM_NULL = 0x0000
WM_CONTEXTMENU = 0x007B
WM_NCDESTROY = 0x0082
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
TRAY_MESSAGE = 0x8001
TRAY_ICON_ID = 1
SUBCLASS_ID = 1
GA_ROOT = 2
IDI_APPLICATION = 32512

NIM_ADD = 0
NIM_DELETE = 2
NIM_SETFOCUS = 3
NIM_SETVERSION = 4
NIF_MESSAGE = 0x0001
NIF_ICON = 0x0002
NIF_TIP = 0x0004
NIF_SHOWTIP = 0x0080
NOTIFYICON_VERSION_4 = 4

MENU_OPEN = 1
MENU_EXIT = 2
MF_STRING = 0
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100
# Los mensajes nativos se anotan y la ventana los atiende en su propio ciclo. Llamar a
# Tkinter dentro del callback de Windows, mientras Tcl tiene el GIL liberado, cierra Python.
POLL_MS = 50


class NotifyIconData(ct.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("window", wintypes.HWND),
        ("icon_id", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("callback", wintypes.UINT),
        ("icon", wintypes.HICON),
        ("tip", wintypes.WCHAR * 128),
        ("state", wintypes.DWORD),
        ("state_mask", wintypes.DWORD),
        ("info", wintypes.WCHAR * 256),
        ("version", wintypes.UINT),
        ("info_title", wintypes.WCHAR * 64),
        ("info_flags", wintypes.DWORD),
        ("guid", ct.c_ubyte * 16),
        ("balloon_icon", wintypes.HICON),
    ]


def _bind(library, name, result_type, *argument_types):
    function = getattr(library, name)
    function.restype = result_type
    function.argtypes = argument_types
    return function


class SystemTray:
    def __init__(
        self,
        root: "Tk",
        show_window: Callable[[], None],
        exit_window: Callable[[], None],
        tray_changed: Callable[[bool], None],
        icon_path: str | None = None,
    ):
        self.root = root
        self._show_window = show_window
        self._exit_window = exit_window
        self._tray_changed = tray_changed
        self.available = False
        self._attached = False
        self._version4 = False
        self._menu_open = False
        self._pending: list = []
        self._poll_after = None

        user = ct.WinDLL("user32.dll")
        shell = ct.WinDLL("shell32.dll")
        controls = ct.WinDLL("comctl32.dll")
        hwnd, uint, boolean = wintypes.HWND, wintypes.UINT, wintypes.BOOL
        wparam, lparam = ct.c_size_t, ct.c_ssize_t
        subclass_type = ct.WINFUNCTYPE(lparam, hwnd, uint, wparam, lparam, wparam, wparam)

        self._notify = _bind(shell, "Shell_NotifyIconW", boolean, wintypes.DWORD, ct.POINTER(NotifyIconData))
        set_subclass = _bind(controls, "SetWindowSubclass", boolean, hwnd, subclass_type, wparam, wparam)
        self._remove_subclass = _bind(controls, "RemoveWindowSubclass", boolean, hwnd, subclass_type, wparam)
        self._default_proc = _bind(controls, "DefSubclassProc", lparam, hwnd, uint, wparam, lparam)
        self._create_menu = _bind(user, "CreatePopupMenu", wintypes.HMENU)
        self._append_menu = _bind(user, "AppendMenuW", boolean, wintypes.HMENU, uint, wparam, wintypes.LPCWSTR)
        self._destroy_menu = _bind(user, "DestroyMenu", boolean, wintypes.HMENU)
        self._foreground = _bind(user, "SetForegroundWindow", boolean, hwnd)
        self._cursor = _bind(user, "GetCursorPos", boolean, ct.POINTER(wintypes.POINT))
        self._track_menu = _bind(
            user, "TrackPopupMenu", boolean, wintypes.HMENU, uint, ct.c_int, ct.c_int,
            ct.c_int, hwnd, ct.c_void_p,
        )
        self._post = _bind(user, "PostMessageW", boolean, hwnd, uint, wparam, lparam)

        root.update_idletasks()
        get_ancestor = _bind(user, "GetAncestor", hwnd, hwnd, uint)
        load_icon = _bind(user, "LoadIconW", wintypes.HICON, wintypes.HINSTANCE, ct.c_void_p)
        register_message = _bind(user, "RegisterWindowMessageW", uint, wintypes.LPCWSTR)
        self._window = get_ancestor(root.winfo_id(), GA_ROOT)
        self._taskbar_created = register_message("TaskbarCreated")
        icon = None
        if icon_path:
            load_image = _bind(user, "LoadImageW", wintypes.HANDLE, wintypes.HINSTANCE, wintypes.LPCWSTR,
                               uint, ct.c_int, ct.c_int, uint)
            icon = load_image(None, icon_path, 1, 0, 0, 0x10 | 0x40)  # IMAGE_ICON, LR_LOADFROMFILE|LR_DEFAULTSIZE
        if not icon:
            icon = load_icon(None, ct.c_void_p(IDI_APPLICATION))
        if not self._window or not self._taskbar_created or not icon:
            raise OSError("No se pudo inicializar la bandeja de JARVIS.")

        self._data = NotifyIconData(
            size=ct.sizeof(NotifyIconData),
            window=self._window,
            icon_id=TRAY_ICON_ID,
            flags=NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP,
            callback=TRAY_MESSAGE,
            icon=icon,
            tip="JARVIS",
        )
        self._callback = subclass_type(self._window_proc)
        if not set_subclass(self._window, self._callback, SUBCLASS_ID, 0):
            raise OSError("No se pudieron vincular los eventos de la bandeja.")
        self._attached = True
        if not self._register():
            self.close()
            raise OSError("Windows no pudo mostrar el icono de JARVIS.")
        self._poll()

    def _register(self) -> bool:
        self.available = bool(self._notify(NIM_ADD, ct.byref(self._data)))
        if self.available:
            self._data.version = NOTIFYICON_VERSION_4
            self._version4 = bool(self._notify(NIM_SETVERSION, ct.byref(self._data)))
        return self.available

    def _restore_icon(self) -> None:
        if not self._attached:
            return
        if self.available:
            self._notify(NIM_DELETE, ct.byref(self._data))
        self._tray_changed(self._register())

    def _window_proc(self, window, message, wparam, lparam, subclass_id, reference):
        if message == WM_NCDESTROY:
            self.close()
        elif message == self._taskbar_created:
            self._queue(self._restore_icon)
        elif message == TRAY_MESSAGE:
            code = lparam & 0xFFFF
            icon_id = (lparam >> 16) & 0xFFFF if self._version4 else wparam
            if icon_id == TRAY_ICON_ID and self.available:
                if code in (WM_LBUTTONUP, NIN_SELECT, NIN_KEYSELECT):
                    self._queue(self._show)
                elif code == (WM_CONTEXTMENU if self._version4 else WM_RBUTTONUP):
                    self._queue(self._context_menu)
            return 0
        return self._default_proc(window, message, wparam, lparam)

    def _show(self) -> None:
        if self._attached:
            self._show_window()

    def _context_menu(self) -> None:
        if not self.available or self._menu_open:
            return
        cursor = wintypes.POINT()
        if not self._cursor(ct.byref(cursor)):
            return
        menu = self._create_menu()
        if not menu:
            return
        self._menu_open = True
        try:
            if not (
                self._append_menu(menu, MF_STRING, MENU_OPEN, "Abrir JARVIS")
                and self._append_menu(menu, MF_STRING, MENU_EXIT, "Salir")
            ):
                return
            self._foreground(self._window)
            command = self._track_menu(
                menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                cursor.x, cursor.y, 0, self._window, None,
            )
            self._post(self._window, WM_NULL, 0, 0)
        finally:
            self._destroy_menu(menu)
            self._menu_open = False

        if command == MENU_OPEN:
            self._show()
        elif command == MENU_EXIT and self._attached:
            self._exit_window()
        elif self.available:
            self._notify(NIM_SETFOCUS, ct.byref(self._data))

    def _queue(self, action) -> None:
        if action not in self._pending:
            self._pending.append(action)

    def _poll(self) -> None:
        if not self._attached:
            return
        actions, self._pending = self._pending, []
        for action in actions:
            action()
        if self._attached:
            try:
                self._poll_after = self.root.after(POLL_MS, self._poll)
            except Exception:
                self._poll_after = None

    def close(self) -> None:
        if self._poll_after is not None:
            try:
                self.root.after_cancel(self._poll_after)
            except Exception:
                pass
            self._poll_after = None
        if self.available:
            self.available = False
            self._notify(NIM_DELETE, ct.byref(self._data))
        if self._attached:
            self._attached = False
            self._remove_subclass(self._window, self._callback, SUBCLASS_ID)
