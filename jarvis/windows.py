import ctypes as ct
import errno
import os
import stat
import subprocess
import threading
from ctypes import wintypes
from pathlib import Path
from time import sleep

from . import apps as app_catalog
from .audio import read_default_volume, set_default_volume
from .control import DesktopControl, default_browser_process
from .memory import MemoryStore
from .web import find_youtube_video
from .tools import (
    POWER_DELAY_SECONDS,
    FolderRedirected,
    GpuReading,
    LocalFolder,
    MemoryItem,
    ProcessInfo,
    ProgramUsage,
    Reminder,
    SystemUsage,
    VolumeStatus,
)

CREATE_NO_WINDOW = 0x08000000

TH32CS_SNAPPROCESS = 0x00000002
ERROR_NO_MORE_FILES = 18
INVALID_HANDLE_VALUE = ct.c_void_p(-1).value
PROJECT_ROOT = Path(__file__).parent.parent
PROJECT_FOLDERS = {LocalFolder.PROJECT: ".", LocalFolder.DOCUMENTATION: "docs"}


class MemoryStatus(ct.Structure):
    _fields_ = [
        ("length", wintypes.DWORD),
        ("load", wintypes.DWORD),
        ("total_physical", ct.c_ulonglong),
        ("available_physical", ct.c_ulonglong),
        ("total_page_file", ct.c_ulonglong),
        ("available_page_file", ct.c_ulonglong),
        ("total_virtual", ct.c_ulonglong),
        ("available_virtual", ct.c_ulonglong),
        ("available_extended_virtual", ct.c_ulonglong),
    ]


class ProcessEntry(ct.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("usage", wintypes.DWORD),
        ("pid", wintypes.DWORD),
        ("default_heap_id", ct.c_size_t),
        ("module_id", wintypes.DWORD),
        ("thread_count", wintypes.DWORD),
        ("parent_pid", wintypes.DWORD),
        ("base_priority", wintypes.LONG),
        ("flags", wintypes.DWORD),
        ("executable", wintypes.WCHAR * wintypes.MAX_PATH),
    ]


class WindowsTools:
    def __init__(self, memory: MemoryStore | None = None):
        if os.name != "nt":
            raise OSError("Se requiere Windows.")
        self._memory = memory
        self._apps = app_catalog.AppCatalog()
        self._desktop: DesktopControl | None = None
        self._dismiss_requested = threading.Event()
        self._pending_power: str | None = None  # "shutdown", "restart" o "sleep"
        self._sleep_timer: threading.Timer | None = None

        kernel = ct.WinDLL("kernel32.dll", use_last_error=True)
        self._system_directory = kernel.GetSystemDirectoryW
        self._system_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        self._system_directory.restype = wintypes.UINT

        self._system_times = kernel.GetSystemTimes
        self._system_times.argtypes = [ct.POINTER(wintypes.FILETIME)] * 3
        self._system_times.restype = wintypes.BOOL

        self._memory_status = kernel.GlobalMemoryStatusEx
        self._memory_status.argtypes = [ct.POINTER(MemoryStatus)]
        self._memory_status.restype = wintypes.BOOL

        self._process_snapshot = kernel.CreateToolhelp32Snapshot
        self._process_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self._process_snapshot.restype = wintypes.HANDLE

        self._process_first = kernel.Process32FirstW
        self._process_next = kernel.Process32NextW
        for read_entry in (self._process_first, self._process_next):
            read_entry.argtypes = [wintypes.HANDLE, ct.POINTER(ProcessEntry)]
            read_entry.restype = wintypes.BOOL

        self._close_handle = kernel.CloseHandle
        self._close_handle.argtypes = [wintypes.HANDLE]
        self._close_handle.restype = wintypes.BOOL

    def preload_apps(self) -> None:
        """Carga en segundo plano la lista de aplicaciones del menú Inicio (~1,2 s)."""
        self._apps.preload()

    def launch_app(self, name: str) -> str:
        app = self._apps.find(name)
        app_catalog.launch(app)
        return app.name

    def _control(self) -> DesktopControl:
        if self._desktop is None:
            self._desktop = DesktopControl()
        return self._desktop

    def focus_window(self, name: str) -> str:
        return self._control().focus(name).title

    def close_window(self, name: str) -> str:
        return self._control().close(name).title

    def minimize_all(self) -> None:
        self._control().minimize_all()

    def media_key(self, action: str) -> None:
        self._control().media(action)

    def open_url(self, url: str) -> None:
        if not url.startswith("https://"):
            raise PermissionError("Solo se abren direcciones https.")
        control = self._control()
        control.allow_others_to_take_focus()
        os.startfile(url)  # navegador predeterminado
        # Un navegador ya abierto no puede ponerse al frente solo: JARVIS lo trae.
        threading.Thread(
            target=self._raise_browser, args=(control,), name="jarvis-navegador", daemon=True,
        ).start()

    @staticmethod
    def _raise_browser(control: DesktopControl) -> None:
        try:
            control.bring_browser_forward(default_browser_process())
        except OSError:
            pass

    def dismiss(self) -> None:
        """Pide a la interfaz que se oculte cuando termine de hablar."""
        self._dismiss_requested.set()

    def take_dismiss(self) -> bool:
        requested = self._dismiss_requested.is_set()
        self._dismiss_requested.clear()
        return requested

    def find_video(self, query: str) -> tuple[str, str] | None:
        return find_youtube_video(query)

    def open_folder(self, folder: LocalFolder) -> None:
        target = PROJECT_ROOT.resolve(strict=True) / PROJECT_FOLDERS[folder]
        attributes = target.lstat()
        if attributes.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise FolderRedirected()
        if not stat.S_ISDIR(attributes.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "El destino autorizado debe ser una carpeta.")
        os.startfile(target, "explore")

    def read_volume(self) -> VolumeStatus:
        return read_default_volume()

    def set_volume(self, percent: int) -> VolumeStatus:
        return set_default_volume(percent)

    def list_processes(self) -> list[ProcessInfo]:
        snapshot = self._process_snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            raise ct.WinError(ct.get_last_error())

        try:
            entry = ProcessEntry()
            entry.size = ct.sizeof(entry)
            processes = []
            has_entry = self._process_first(snapshot, ct.byref(entry))
            while has_entry:
                processes.append(ProcessInfo(entry.pid, entry.executable))
                has_entry = self._process_next(snapshot, ct.byref(entry))

            error = ct.get_last_error()
            if error != ERROR_NO_MORE_FILES:
                raise ct.WinError(error)
            return processes
        finally:
            self._close_handle(snapshot)

    def _memory_store(self) -> MemoryStore:
        if self._memory is None:
            raise OSError("La memoria de JARVIS no está disponible.")
        return self._memory

    def save_memory(self, text: str) -> int:
        return self._memory_store().add_fact(text)

    def list_memories(self) -> list[MemoryItem]:
        return self._memory_store().facts()

    def forget_memory(self, memory_id: int) -> bool:
        return self._memory_store().forget_fact(memory_id)

    def clear_memories(self) -> int:
        return self._memory_store().forget_everything()

    # --- hardware ------------------------------------------------------------------

    def read_gpu(self) -> GpuReading | None:
        from .hardware import read_gpu

        gpu = read_gpu()
        if gpu is None:
            return None
        return GpuReading(gpu.name, gpu.utilization, gpu.memory_used_mib, gpu.memory_total_mib, gpu.temperature)

    def top_programs(self) -> list[ProgramUsage]:
        from .hardware import top_processes

        return [ProgramUsage(item.name, item.instances, item.memory_mib, item.cpu_percent)
                for item in top_processes()]

    # --- recordatorios ----------------------------------------------------------------

    def add_reminder(self, text: str, due, kind: str) -> int:
        return self._memory_store().add_reminder(text, due, kind)

    def list_reminders(self) -> list[Reminder]:
        return self._memory_store().pending_reminders()

    def cancel_reminder(self, reminder_id: int) -> Reminder | None:
        return self._memory_store().cancel_reminder(reminder_id)

    # --- energía ----------------------------------------------------------------------

    def _shutdown_exe(self, *arguments: str) -> None:
        exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "shutdown.exe"
        completed = subprocess.run([str(exe), *arguments], capture_output=True, creationflags=CREATE_NO_WINDOW,
                                   timeout=10)
        if completed.returncode != 0:
            raise OSError(completed.returncode, "shutdown.exe rechazó la orden")

    def power(self, action: str) -> None:
        self.cancel_power()
        delay = POWER_DELAY_SECONDS[action]
        if action == "sleep":
            timer = threading.Timer(delay, _suspend)
            timer.daemon = True
            timer.start()
            self._sleep_timer = timer
        else:
            flag = "/s" if action == "shutdown" else "/r"
            self._shutdown_exe(flag, "/t", str(delay), "/c", "JARVIS: orden del usuario.")
        self._pending_power = action

    def cancel_power(self) -> bool:
        pending, self._pending_power = self._pending_power, None
        if self._sleep_timer is not None:
            self._sleep_timer.cancel()
            self._sleep_timer = None
        if pending in ("shutdown", "restart"):
            try:
                self._shutdown_exe("/a")
            except (OSError, subprocess.SubprocessError):
                return False
        return pending is not None

    def lock(self) -> None:
        if not ct.WinDLL("user32.dll", use_last_error=True).LockWorkStation():
            raise ct.WinError(ct.get_last_error())

    def _cpu_times(self) -> tuple[int, int]:
        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not self._system_times(ct.byref(idle), ct.byref(kernel), ct.byref(user)):
            raise ct.WinError(ct.get_last_error())

        idle_ticks, kernel_ticks, user_ticks = (
            (value.dwHighDateTime << 32) | value.dwLowDateTime
            for value in (idle, kernel, user)
        )
        # Windows incluye el tiempo inactivo dentro del tiempo de núcleo.
        return idle_ticks, kernel_ticks + user_ticks

    def read_usage(self) -> SystemUsage:
        idle_before, total_before = self._cpu_times()
        sleep(0.1)
        idle_after, total_after = self._cpu_times()
        idle = idle_after - idle_before
        total = total_after - total_before
        if total <= 0 or not 0 <= idle <= total:
            raise OSError("No se pudo calcular una muestra de CPU.")

        memory = MemoryStatus()
        memory.length = ct.sizeof(memory)
        if not self._memory_status(ct.byref(memory)):
            raise ct.WinError(ct.get_last_error())

        return SystemUsage(
            cpu_percent=100 * (total - idle) / total,
            ram_total_bytes=memory.total_physical,
            ram_available_bytes=memory.available_physical,
        )


def _suspend() -> None:
    """Suspensión (no hibernación). Despierta con el botón de encendido o el teclado."""
    powrprof = ct.WinDLL("powrprof.dll")
    powrprof.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN]
    powrprof.SetSuspendState(False, False, False)
