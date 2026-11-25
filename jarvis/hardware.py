"""GPU NVIDIA (NVML, incluida en el driver) y procesos que más consumen, sin dependencias."""

import ctypes as ct
import os
from ctypes import wintypes
from dataclasses import dataclass
from time import sleep

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class GpuStatus:
    name: str
    utilization: int
    memory_used_mib: int
    memory_total_mib: int
    temperature: int


@dataclass(frozen=True)
class ProcessUsage:
    name: str
    instances: int
    memory_mib: float
    cpu_percent: float


class _Utilization(ct.Structure):
    _fields_ = [("gpu", ct.c_uint), ("memory", ct.c_uint)]


class _Memory(ct.Structure):
    _fields_ = [("total", ct.c_ulonglong), ("free", ct.c_ulonglong), ("used", ct.c_ulonglong)]


def read_gpu() -> GpuStatus | None:
    """Estado de la primera GPU NVIDIA, o None si no hay driver NVIDIA."""
    try:
        nvml = ct.WinDLL("nvml.dll")
    except OSError:
        return None
    if nvml.nvmlInit_v2() != 0:
        return None
    try:
        handle = ct.c_void_p()
        if nvml.nvmlDeviceGetHandleByIndex_v2(0, ct.byref(handle)) != 0:
            return None
        name = ct.create_string_buffer(96)
        utilization, memory, temperature = _Utilization(), _Memory(), ct.c_uint()
        nvml.nvmlDeviceGetName(handle, name, 96)
        nvml.nvmlDeviceGetUtilizationRates(handle, ct.byref(utilization))
        nvml.nvmlDeviceGetMemoryInfo(handle, ct.byref(memory))
        nvml.nvmlDeviceGetTemperature(handle, 0, ct.byref(temperature))  # 0 = sensor de la GPU
        return GpuStatus(name.value.decode(errors="replace"), utilization.gpu, memory.used // 2**20,
                         memory.total // 2**20, temperature.value)
    finally:
        nvml.nvmlShutdown()


class _CounterItem(ct.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("status", wintypes.DWORD), ("padding", wintypes.DWORD),
                ("value", ct.c_double)]


def read_cpu_temperature() -> float | None:
    """Grados del sensor térmico del equipo (ACPI), sin permisos de administrador.

    Sigue a la CPU (68 → 99 °C con carga en el equipo del usuario) con unos 10 s de retraso.
    """
    try:
        pdh = ct.WinDLL("pdh.dll")
    except OSError:
        return None
    query, counter = ct.c_void_p(), ct.c_void_p()
    if pdh.PdhOpenQueryW(None, None, ct.byref(query)) != 0:
        return None
    try:
        path = "\\Thermal Zone Information(*)\\High Precision Temperature"
        if pdh.PdhAddEnglishCounterW(query, path, None, ct.byref(counter)) != 0 or pdh.PdhCollectQueryData(query) != 0:
            return None
        size, count = wintypes.DWORD(0), wintypes.DWORD(0)
        pdh.PdhGetFormattedCounterArrayW(counter, 0x200, ct.byref(size), ct.byref(count), None)  # 0x200 = double
        if size.value == 0:
            return None
        buffer = (ct.c_byte * size.value)()
        if pdh.PdhGetFormattedCounterArrayW(counter, 0x200, ct.byref(size), ct.byref(count), buffer) != 0:
            return None
        items = ct.cast(buffer, ct.POINTER(_CounterItem))
        values = [items[i].value / 10 - 273.15 for i in range(count.value) if items[i].status == 0]
        values = [value for value in values if 5 < value < 125]  # descarta sensores vacíos
        return max(values) if values else None
    finally:
        pdh.PdhCloseQuery(query)


class _MemoryCounters(ct.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ct.c_size_t), ("WorkingSetSize", ct.c_size_t)] + \
               [(f"field{i}", ct.c_size_t) for i in range(6)]


def _bind(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


def top_processes(sample: float = 0.5) -> list[ProcessUsage]:
    """Programas que más RAM y CPU usan, sumando todas sus instancias (p. ej. las de Opera)."""
    kernel, psapi = ct.WinDLL("kernel32.dll"), ct.WinDLL("psapi.dll")
    enum = _bind(psapi, "EnumProcesses", wintypes.BOOL, ct.POINTER(wintypes.DWORD), wintypes.DWORD,
                 ct.POINTER(wintypes.DWORD))
    open_process = _bind(kernel, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    close = _bind(kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    image = _bind(kernel, "QueryFullProcessImageNameW", wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                  wintypes.LPWSTR, ct.POINTER(wintypes.DWORD))
    memory_info = _bind(psapi, "GetProcessMemoryInfo", wintypes.BOOL, wintypes.HANDLE,
                        ct.POINTER(_MemoryCounters), wintypes.DWORD)
    times = _bind(kernel, "GetProcessTimes", wintypes.BOOL, wintypes.HANDLE, *[ct.POINTER(wintypes.FILETIME)] * 4)

    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    enum(pids, ct.sizeof(pids), ct.byref(needed))
    handles = {}
    for pid in list(pids)[: needed.value // ct.sizeof(wintypes.DWORD)]:
        if pid in (0, 4, os.getpid()):
            continue
        handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            handles[pid] = handle

    def cpu_time(handle) -> int:
        create, exit_, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not times(handle, ct.byref(create), ct.byref(exit_), ct.byref(kernel_time), ct.byref(user_time)):
            return 0
        return sum((value.dwHighDateTime << 32) | value.dwLowDateTime for value in (kernel_time, user_time))

    try:
        before = {pid: cpu_time(handle) for pid, handle in handles.items()}
        sleep(sample)
        totals: dict[str, list] = {}
        cores = os.cpu_count() or 1
        for pid, handle in handles.items():
            buffer, size = ct.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if not image(handle, 0, buffer, ct.byref(size)):
                continue
            name = os.path.splitext(os.path.basename(buffer.value))[0]
            counters = _MemoryCounters()
            counters.cb = ct.sizeof(counters)
            memory = counters.WorkingSetSize / 2**20 if memory_info(handle, ct.byref(counters), counters.cb) else 0.0
            cpu = (cpu_time(handle) - before.get(pid, 0)) / (sample * 1e7) / cores * 100
            entry = totals.setdefault(name, [0, 0.0, 0.0])
            entry[0] += 1
            entry[1] += memory
            entry[2] += max(0.0, cpu)
    finally:
        for handle in handles.values():
            close(handle)
    usage = [ProcessUsage(name, count, memory, cpu) for name, (count, memory, cpu) in totals.items()]
    return sorted(usage, key=lambda item: -item.memory_mib)
