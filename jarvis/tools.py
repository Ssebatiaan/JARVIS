import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Protocol, cast

NOTEPAD = "notepad"


class LocalFolder(Enum):
    PROJECT = "del proyecto"
    DOCUMENTATION = "de documentación"


@dataclass(frozen=True)
class SystemUsage:
    cpu_percent: float
    ram_total_bytes: int
    ram_available_bytes: int


@dataclass(frozen=True)
class VolumeStatus:
    percent: int
    muted: bool


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    name: str


@dataclass(frozen=True)
class MemoryItem:
    id: int
    text: str


class AudioOutputUnavailable(OSError):
    """Windows carece de una salida de audio predeterminada."""


class MemoryFull(Exception):
    """La memoria alcanzó el máximo de recuerdos permitidos."""


class FolderRedirected(PermissionError):
    """La entrada autorizada representa un enlace o un punto de redirección."""


class LocalTools(Protocol):
    def open_notepad(self) -> int: ...
    def open_folder(self, folder: LocalFolder) -> None: ...
    def read_usage(self) -> SystemUsage: ...
    def read_volume(self) -> VolumeStatus: ...
    def set_volume(self, percent: int) -> VolumeStatus: ...
    def list_processes(self) -> list[ProcessInfo]: ...
    def save_memory(self, text: str) -> int: ...
    def list_memories(self) -> list[MemoryItem]: ...
    def forget_memory(self, memory_id: int) -> bool: ...
    def clear_memories(self) -> int: ...


class ToolId(StrEnum):
    OPEN_APP = "apps.open"
    OPEN_FOLDER = "folders.open"
    SYSTEM_METRICS = "system.metrics"
    READ_VOLUME = "audio.volume"
    SET_VOLUME = "audio.set_volume"
    LIST_PROCESSES = "system.processes"
    SAVE_MEMORY = "memory.save"
    LIST_MEMORIES = "memory.list"
    FORGET_MEMORY = "memory.forget"
    CLEAR_MEMORIES = "memory.clear"


class Permission(Enum):
    OPEN_APP = "apps.launch"
    OPEN_FOLDER = "folders.open"
    READ_METRICS = "system.metrics.read"
    READ_VOLUME = "audio.volume.read"
    SET_VOLUME = "audio.volume.write"
    READ_PROCESSES = "system.processes.read"
    SAVE_MEMORY = "memory.write"
    READ_MEMORY = "memory.read"
    FORGET_MEMORY = "memory.forget"
    CLEAR_MEMORY = "memory.clear"


DEFAULT_PERMISSIONS = frozenset({
    Permission.OPEN_APP,
    Permission.OPEN_FOLDER,
    Permission.READ_METRICS,
    Permission.READ_VOLUME,
    Permission.SET_VOLUME,
    Permission.READ_PROCESSES,
    Permission.SAVE_MEMORY,
    Permission.READ_MEMORY,
    Permission.FORGET_MEMORY,
    Permission.CLEAR_MEMORY,
})

MAX_MEMORY_TEXT = 300


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    value_type: type
    allowed_values: tuple[object, ...] = ()
    bounds: tuple[int, int] | None = None
    max_length: int | None = None

    def accepts(self, value: object) -> bool:
        if type(value) is not self.value_type:
            return False
        if self.allowed_values and value not in self.allowed_values:
            return False
        if self.max_length is not None:
            return 0 < len(cast(str, value).strip()) <= self.max_length
        if self.bounds is not None:
            minimum, maximum = self.bounds
            return minimum <= cast(int, value) <= maximum
        return True


@dataclass(frozen=True)
class ToolOutcome:
    code: int
    status: str
    message: str


ToolArguments = Mapping[str, object]


@dataclass(frozen=True)
class ToolSpec:
    id: ToolId
    permission: Permission
    error_target: str
    handler: Callable[[LocalTools, ToolArguments], ToolOutcome]
    parameters: tuple[ArgumentSpec, ...] = ()
    invalid_arguments: str = "Esta herramienta no recibe argumentos."
    approval_preview: Callable[[ToolArguments], str] | None = None

    def accepts_arguments(self, arguments: ToolArguments) -> bool:
        if arguments.keys() != {parameter.name for parameter in self.parameters}:
            return False
        return all(parameter.accepts(arguments[parameter.name]) for parameter in self.parameters)


def normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(plain.split())


def _open_app(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    pid = tools.open_notepad()
    return ToolOutcome(0, "completada", f"Windows inició el proceso del Bloc de notas (PID {pid}).")


def _open_folder(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    folder = cast(LocalFolder, arguments["folder"])
    tools.open_folder(folder)
    return ToolOutcome(0, "solicitada", f"Windows recibió la solicitud para abrir la carpeta {folder.value}.")


def _read_metrics(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    usage = tools.read_usage()
    gib = 1024**3
    used = (usage.ram_total_bytes - usage.ram_available_bytes) / gib
    total = usage.ram_total_bytes / gib
    return ToolOutcome(0, "completada", f"CPU {usage.cpu_percent:.1f} % · RAM {used:.2f} de {total:.2f} GiB.")


def _format_volume(volume: VolumeStatus) -> str:
    mute_status = " (silenciado)" if volume.muted else ""
    return f"{volume.percent} %{mute_status}"


def _read_volume(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    volume = tools.read_volume()
    return ToolOutcome(0, "completada", f"Volumen de la salida predeterminada: {_format_volume(volume)}.")


def _set_volume(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    percent = cast(int, arguments["percent"])
    volume = tools.set_volume(percent)
    if volume.percent == percent:
        return ToolOutcome(0, "completada", f"Volumen ajustado a {_format_volume(volume)}.")
    return ToolOutcome(1, "nivel_distinto", f"Solicité {percent} %. Windows reportó {_format_volume(volume)}.")


def _list_processes(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    processes = sorted(tools.list_processes(), key=lambda item: (normalize(item.name), item.pid))
    rows = ["Procesos en la captura de Windows", f"{'PID':>10}  Ejecutable"]
    rows.extend(f"{process.pid:>10}  {process.name}" for process in processes)
    rows.append(f"Total de procesos: {len(processes)}.")
    return ToolOutcome(0, "completada", "\n".join(rows))


def _save_memory(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    text = " ".join(cast(str, arguments["text"]).split())
    memory_id = tools.save_memory(text)
    return ToolOutcome(0, "completada", f"Lo recordaré (recuerdo {memory_id}): {text}")


def _list_memories(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    memories = tools.list_memories()
    if not memories:
        return ToolOutcome(0, "completada", "Todavía no recuerdo nada sobre ti.")
    rows = ["Esto es lo que recuerdo:"]
    rows.extend(f"{memory.id}. {memory.text}" for memory in memories)
    rows.append("Di «olvida el recuerdo N» para borrar uno.")
    return ToolOutcome(0, "completada", "\n".join(rows))


def _forget_memory(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    memory_id = cast(int, arguments["memory_id"])
    if tools.forget_memory(memory_id):
        return ToolOutcome(0, "completada", f"Olvidé el recuerdo {memory_id}.")
    return ToolOutcome(2, "no_encontrada", f"No tengo un recuerdo con el número {memory_id}.")


def _clear_memories(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    removed = tools.clear_memories()
    return ToolOutcome(0, "completada", f"Olvidé {removed} recuerdos y el historial de conversación.")


TOOLS: Mapping[ToolId, ToolSpec] = MappingProxyType({
    tool.id: tool for tool in (
        ToolSpec(
            id=ToolId.OPEN_APP,
            permission=Permission.OPEN_APP,
            error_target="abrir el Bloc de notas",
            handler=_open_app,
            parameters=(ArgumentSpec("app_id", str, allowed_values=(NOTEPAD,)),),
            invalid_arguments="Aplicación no permitida. Puedes abrir el Bloc de notas.",
        ),
        ToolSpec(
            id=ToolId.OPEN_FOLDER,
            permission=Permission.OPEN_FOLDER,
            error_target="solicitar la apertura de la carpeta",
            handler=_open_folder,
            parameters=(ArgumentSpec("folder", LocalFolder, allowed_values=tuple(LocalFolder)),),
            invalid_arguments="Carpeta no autorizada. Elige la carpeta del proyecto o la de documentación.",
        ),
        ToolSpec(
            id=ToolId.SYSTEM_METRICS,
            permission=Permission.READ_METRICS,
            error_target="consultar CPU y RAM",
            handler=_read_metrics,
        ),
        ToolSpec(
            id=ToolId.READ_VOLUME,
            permission=Permission.READ_VOLUME,
            error_target="consultar el volumen",
            handler=_read_volume,
        ),
        ToolSpec(
            id=ToolId.SET_VOLUME,
            permission=Permission.SET_VOLUME,
            error_target="confirmar el ajuste de volumen",
            handler=_set_volume,
            parameters=(ArgumentSpec("percent", int, bounds=(0, 100)),),
            invalid_arguments="Indica un porcentaje entero de 0 a 100. Ejemplo «pon el volumen al 35 %».",
            approval_preview=lambda args: (
                f"Ajustar el volumen de la salida predeterminada al {args['percent']} %."
            ),
        ),
        ToolSpec(
            id=ToolId.LIST_PROCESSES,
            permission=Permission.READ_PROCESSES,
            error_target="listar los procesos",
            handler=_list_processes,
        ),
        ToolSpec(
            id=ToolId.SAVE_MEMORY,
            permission=Permission.SAVE_MEMORY,
            error_target="guardar el recuerdo",
            handler=_save_memory,
            parameters=(ArgumentSpec("text", str, max_length=MAX_MEMORY_TEXT),),
            invalid_arguments="Indica qué debo recordar, en menos de 300 caracteres.",
        ),
        ToolSpec(
            id=ToolId.LIST_MEMORIES,
            permission=Permission.READ_MEMORY,
            error_target="leer la memoria",
            handler=_list_memories,
        ),
        ToolSpec(
            id=ToolId.FORGET_MEMORY,
            permission=Permission.FORGET_MEMORY,
            error_target="borrar el recuerdo",
            handler=_forget_memory,
            parameters=(ArgumentSpec("memory_id", int, bounds=(1, 2**31 - 1)),),
            invalid_arguments="Indica el número del recuerdo. Ejemplo «olvida el recuerdo 3».",
        ),
        ToolSpec(
            id=ToolId.CLEAR_MEMORIES,
            permission=Permission.CLEAR_MEMORY,
            error_target="borrar la memoria",
            handler=_clear_memories,
            approval_preview=lambda args: (
                "Borrar todos los recuerdos y el historial de conversación de JARVIS."
            ),
        ),
    )
})
