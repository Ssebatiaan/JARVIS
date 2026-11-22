"""Atajo de teclado global de Windows para mostrar JARVIS desde cualquier programa.

El atajo se registra en un hilo propio con su bucle de mensajes de Windows. Ese hilo nunca
llama a Tkinter: solo marca un evento que la ventana consulta cada 50 ms. Así se evita que un
callback nativo entre en Tkinter mientras Tcl tiene liberado el GIL, lo que cierra Python.
"""

import ctypes as ct
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tkinter import Tk

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
PM_NOREMOVE = 0x0000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_J = 0x4A
HOTKEY_ID = 0x4A41
POLL_MS = 50

# En Windows, AltGr equivale a Ctrl+Alt: registrar Ctrl+Alt+J también responde a AltGr+J.
DEFAULT_MODIFIERS = MOD_CONTROL | MOD_ALT
DEFAULT_KEY = VK_J
HOTKEY_LABEL = "AltGr+J"


class HotkeyUnavailable(OSError):
    """Windows no permitió registrar el atajo (por ejemplo, otro programa ya lo usa)."""


def _bind(library, name, result_type, *argument_types):
    function = getattr(library, name)
    function.restype = result_type
    function.argtypes = argument_types
    return function


class GlobalHotkey:
    def __init__(
        self,
        root: "Tk",
        callback: Callable[[], None],
        *,
        modifiers: int = DEFAULT_MODIFIERS,
        key: int = DEFAULT_KEY,
        hotkey_id: int = HOTKEY_ID,
    ):
        self.root = root
        self._callback = callback
        self._id = hotkey_id
        self._pressed = threading.Event()
        self._ready = threading.Event()
        self._registered = False
        self._error = 0
        self._thread_id = 0
        self._after: str | None = None

        user = ct.WinDLL("user32.dll", use_last_error=True)
        self._post_thread_message = _bind(
            user, "PostThreadMessageW", wintypes.BOOL, wintypes.DWORD, wintypes.UINT,
            ct.c_size_t, ct.c_ssize_t,
        )
        self._thread = threading.Thread(
            target=self._message_loop, args=(modifiers, key), name="jarvis-hotkey", daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(3) or not self._registered:
            self._thread.join(1)
            raise HotkeyUnavailable(self._error, "Otro programa ya usa este atajo.")
        self._poll()

    @property
    def available(self) -> bool:
        return self._registered

    @property
    def thread_id(self) -> int:
        return self._thread_id

    def _message_loop(self, modifiers: int, key: int) -> None:
        user = ct.WinDLL("user32.dll", use_last_error=True)
        kernel = ct.WinDLL("kernel32.dll")
        register = _bind(user, "RegisterHotKey", wintypes.BOOL, wintypes.HWND, ct.c_int,
                         wintypes.UINT, wintypes.UINT)
        unregister = _bind(user, "UnregisterHotKey", wintypes.BOOL, wintypes.HWND, ct.c_int)
        get_message = _bind(user, "GetMessageW", wintypes.BOOL, ct.POINTER(wintypes.MSG),
                            wintypes.HWND, wintypes.UINT, wintypes.UINT)
        peek_message = _bind(user, "PeekMessageW", wintypes.BOOL, ct.POINTER(wintypes.MSG),
                             wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT)
        message = wintypes.MSG()
        self._thread_id = kernel.GetCurrentThreadId()
        peek_message(ct.byref(message), None, 0, 0, PM_NOREMOVE)  # crea la cola de mensajes
        if not register(None, self._id, modifiers | MOD_NOREPEAT, key):
            self._error = ct.get_last_error()
            self._ready.set()
            return
        self._registered = True
        self._ready.set()
        try:
            while get_message(ct.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY and message.wParam == self._id:
                    self._pressed.set()
        finally:
            unregister(None, self._id)

    def _poll(self) -> None:
        if not self._registered:
            return
        if self._pressed.is_set():
            self._pressed.clear()
            self._callback()
        try:
            self._after = self.root.after(POLL_MS, self._poll)
        except Exception:
            self._after = None

    def close(self) -> None:
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        if self._registered:
            self._registered = False
            self._post_thread_message(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(2)
