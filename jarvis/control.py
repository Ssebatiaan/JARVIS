"""Ventanas y teclas multimedia de Windows mediante la API Win32 (sin clics por coordenadas)."""

import ctypes as ct
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from time import monotonic, sleep

from .text import normalize
from .tools import TargetNotFound

WM_CLOSE = 0x0010
SW_RESTORE = 9
GW_OWNER = 4
DWMWA_CLOAKED = 14
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_D = 0x44
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ASFW_ANY = 0xFFFFFFFF
KNOWN_BROWSERS = ("opera", "chrome", "msedge", "firefox", "brave", "vivaldi")
DEFAULT_BROWSER_KEY = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
MEDIA_KEYS = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "mute": 0xAD}
# Procesos del sistema cuyas ventanas no se ofrecen para cerrar o enfocar.
SYSTEM_PROCESSES = {"explorer", "searchhost", "shellexperiencehost", "startmenuexperiencehost",
                    "textinputhost", "applicationframehost", "lockapp", "systemsettings", "nvidia overlay"}


def default_browser_process() -> str | None:
    """Nombre del ejecutable del navegador predeterminado para https (por ejemplo «opera»)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DEFAULT_BROWSER_KEY) as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r"\shell\open\command") as key:
            command = winreg.QueryValueEx(key, "")[0]
    except OSError:
        return None
    match = re.match(r'\s*"([^"]+)"|\s*(\S+)', command)
    if not match:
        return None
    return os.path.splitext(os.path.basename(match[1] or match[2]))[0].lower()


@dataclass(frozen=True)
class WindowInfo:
    handle: int
    title: str
    process: str


def _bind(library, name, result_type, *argument_types):
    function = getattr(library, name)
    function.restype = result_type
    function.argtypes = argument_types
    return function


class _Win32:
    def __init__(self):
        user = ct.WinDLL("user32.dll", use_last_error=True)
        kernel = ct.WinDLL("kernel32.dll", use_last_error=True)
        dwm = ct.WinDLL("dwmapi.dll")
        hwnd = wintypes.HWND
        self.enum_proc = ct.WINFUNCTYPE(wintypes.BOOL, hwnd, wintypes.LPARAM)
        self.enum_windows = _bind(user, "EnumWindows", wintypes.BOOL, self.enum_proc, wintypes.LPARAM)
        self.visible = _bind(user, "IsWindowVisible", wintypes.BOOL, hwnd)
        self.iconic = _bind(user, "IsIconic", wintypes.BOOL, hwnd)
        self.is_window = _bind(user, "IsWindow", wintypes.BOOL, hwnd)
        self.owner = _bind(user, "GetWindow", hwnd, hwnd, wintypes.UINT)
        self.title_length = _bind(user, "GetWindowTextLengthW", ct.c_int, hwnd)
        self.title = _bind(user, "GetWindowTextW", ct.c_int, hwnd, wintypes.LPWSTR, ct.c_int)
        self.thread_process = _bind(user, "GetWindowThreadProcessId", wintypes.DWORD, hwnd,
                                    ct.POINTER(wintypes.DWORD))
        self.show = _bind(user, "ShowWindow", wintypes.BOOL, hwnd, ct.c_int)
        self.foreground = _bind(user, "SetForegroundWindow", wintypes.BOOL, hwnd)
        self.current_foreground = _bind(user, "GetForegroundWindow", hwnd)
        self.allow_foreground = _bind(user, "AllowSetForegroundWindow", wintypes.BOOL, wintypes.DWORD)
        self.post = _bind(user, "PostMessageW", wintypes.BOOL, hwnd, wintypes.UINT,
                          wintypes.WPARAM, wintypes.LPARAM)
        self.key = _bind(user, "keybd_event", None, wintypes.BYTE, wintypes.BYTE, wintypes.DWORD,
                         ct.c_size_t)
        self.open_process = _bind(kernel, "OpenProcess", wintypes.HANDLE, wintypes.DWORD,
                                  wintypes.BOOL, wintypes.DWORD)
        self.image_name = _bind(kernel, "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE,
                                wintypes.DWORD, wintypes.LPWSTR, ct.POINTER(wintypes.DWORD))
        self.close_handle = _bind(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
        self.cloak_attribute = _bind(dwm, "DwmGetWindowAttribute", ct.c_long, hwnd, wintypes.DWORD,
                                     ct.c_void_p, wintypes.DWORD)

    def process_name(self, handle) -> str:
        pid = wintypes.DWORD()
        self.thread_process(handle, ct.byref(pid))
        process = self.open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not process:
            return ""
        try:
            buffer = ct.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if not self.image_name(process, 0, buffer, ct.byref(size)):
                return ""
            return os.path.splitext(os.path.basename(buffer.value))[0].lower()
        finally:
            self.close_handle(process)

    def cloaked(self, handle) -> bool:
        value = wintypes.DWORD()
        return self.cloak_attribute(handle, DWMWA_CLOAKED, ct.byref(value), ct.sizeof(value)) == 0 \
            and value.value != 0

    def tap(self, key: int, extended: bool = True) -> None:
        flags = KEYEVENTF_EXTENDEDKEY if extended else 0
        self.key(key, 0, flags, 0)
        self.key(key, 0, flags | KEYEVENTF_KEYUP, 0)


class DesktopControl:
    def __init__(self):
        self._api = _Win32()
        self._own_pid = os.getpid()

    def windows(self) -> list[WindowInfo]:
        api, found = self._api, []

        def collect(handle, _):
            if not api.visible(handle) or api.owner(handle, GW_OWNER) or api.cloaked(handle):
                return True
            length = api.title_length(handle)
            if length <= 0:
                return True
            buffer = ct.create_unicode_buffer(length + 1)
            api.title(handle, buffer, length + 1)
            pid = wintypes.DWORD()
            api.thread_process(handle, ct.byref(pid))
            process = api.process_name(handle)
            if pid.value != self._own_pid and process not in SYSTEM_PROCESSES and buffer.value.strip():
                found.append(WindowInfo(int(handle), buffer.value, process))
            return True

        callback = api.enum_proc(collect)
        api.enum_windows(callback, 0)
        return found

    @staticmethod
    def _score(query: str, window: WindowInfo) -> float:
        process = normalize(window.process)
        title = normalize(window.title)
        if query == process or process.startswith(query):
            return 1.0
        words = title.replace("-", " ").replace("|", " ").split()
        if query in words or title.endswith(query):
            return 0.9
        if query in title:
            return 0.8
        if query.replace(" ", "") in process:
            return 0.85
        return 0.0

    def find(self, query: str) -> WindowInfo:
        wanted = normalize(query).strip(" .¿?¡!")
        for prefix in ("la ventana de ", "la ventana del ", "el ", "la ", "mi "):
            wanted = wanted.removeprefix(prefix)
        aliases = {"vs code": "code", "visual studio code": "code", "opera": "opera", "opera gx": "opera",
                   "navegador": "opera", "explorador": "explorer"}
        wanted = aliases.get(wanted, wanted)
        scored = sorted(((self._score(wanted, window), window) for window in self.windows()),
                        key=lambda item: -item[0])
        if not scored or scored[0][0] < 0.75:
            raise TargetNotFound(query, [])
        return scored[0][1]

    def _raise(self, window: WindowInfo) -> None:
        api = self._api
        if api.iconic(window.handle):
            api.show(window.handle, SW_RESTORE)
        if not api.foreground(window.handle):
            # Windows solo cede el foco a quien recibió la última entrada; un toque de Alt lo permite.
            api.tap(VK_MENU, extended=False)
            api.foreground(window.handle)

    def focus(self, query: str) -> WindowInfo:
        window = self.find(query)
        self._raise(window)
        return window

    def allow_others_to_take_focus(self) -> None:
        """Permite que el programa al que se pasa un enlace se ponga al frente por sí mismo."""
        self._api.allow_foreground(ASFW_ANY)

    def bring_browser_forward(self, process: str | None, timeout: float = 3.0) -> WindowInfo | None:
        """Espera la ventana del navegador y la trae al frente si quedó detrás."""
        names = (process,) if process else KNOWN_BROWSERS
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            sleep(0.25)
            windows = [window for window in self.windows() if window.process in names]
            if windows:
                window = windows[0]  # EnumWindows va de arriba abajo: la más reciente primero
                if self._api.current_foreground() != window.handle:
                    self._raise(window)
                return window
        return None

    def close(self, query: str) -> WindowInfo:
        window = self.find(query)
        self._api.post(window.handle, WM_CLOSE, 0, 0)
        return window

    def minimize_all(self) -> None:
        api = self._api
        api.key(VK_LWIN, 0, KEYEVENTF_EXTENDEDKEY, 0)
        api.key(VK_D, 0, 0, 0)
        api.key(VK_D, 0, KEYEVENTF_KEYUP, 0)
        api.key(VK_LWIN, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
        sleep(0.05)

    def media(self, action: str) -> None:
        self._api.tap(MEDIA_KEYS[action])
