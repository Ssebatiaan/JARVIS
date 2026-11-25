import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from types import MappingProxyType
from typing import Protocol, cast

from .text import normalize as normalize

# Sitios y buscadores permitidos: las claves válidas para web.open y web.search.
SITE_KEYS = (
    "youtube", "youtube music", "google", "gmail", "github", "whatsapp web", "netflix", "twitch",
    "chatgpt", "claude", "drive", "google drive", "maps", "google maps", "wikipedia", "instagram",
    "x", "twitter", "reddit", "savio", "outlook",
)
SEARCH_ENGINE_KEYS = ("google", "youtube", "wikipedia")
MEDIA_ACTIONS = ("play_pause", "next", "previous", "stop", "mute")
REMINDER_KINDS = ("recordatorio", "alarma", "temporizador")
POWER_ACTIONS = ("shutdown", "restart", "sleep")
POWER_DELAY_SECONDS = {"shutdown": 60, "restart": 60, "sleep": 15}
DUE_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?"
MAX_REMINDER_TEXT = 200
TASK_DUE_PATTERN = r"(?:\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?)?"
TASK_KINDS = ("tarea", "evento")
AGENDA_PERIODS = ("hoy", "manana", "semana", "proxima_semana", "mes", "lunes", "martes", "miercoles", "jueves",
                  "viernes", "sabado", "domingo")
AGENDA_KINDS = ("todo", "tareas", "eventos")
CITY_PATTERN = r"[^\W\d_][\w .,'-]{0,59}"
# Un nombre de aplicación o ventana, nunca una ruta ni una orden.
NAME_PATTERN = r"[^\\/:*?\"<>|;&]{1,80}"


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


@dataclass(frozen=True)
class Reminder:
    id: int
    text: str
    due: datetime  # hora local
    kind: str
    created: datetime | None = None


@dataclass(frozen=True)
class AgendaItem:
    when: datetime | None  # hora local; None si no tiene fecha
    all_day: bool
    title: str
    kind: str  # "tarea", "evento" o "aviso"
    source: str = ""  # nombre del calendario; vacío si se anotó en JARVIS


@dataclass(frozen=True)
class Task:
    id: int
    text: str
    due: datetime | None
    has_time: bool
    kind: str


@dataclass(frozen=True)
class AgendaData:
    items: list[AgendaItem]
    warnings: list[str]
    calendars: int


class CalendarError(Exception):
    """No se pudo conectar o leer un calendario. El mensaje nunca incluye el enlace."""


class TasksFull(Exception):
    """Demasiadas tareas abiertas."""


@dataclass(frozen=True)
class GpuReading:
    name: str
    utilization: int
    memory_used_mib: int
    memory_total_mib: int
    temperature: int


@dataclass(frozen=True)
class ProgramUsage:
    name: str
    instances: int
    memory_mib: float
    cpu_percent: float


class RemindersFull(Exception):
    """Demasiados recordatorios pendientes."""


class AudioOutputUnavailable(OSError):
    """Windows carece de una salida de audio predeterminada."""


class MemoryFull(Exception):
    """La memoria alcanzó el máximo de recuerdos permitidos."""


class FolderRedirected(PermissionError):
    """La entrada autorizada representa un enlace o un punto de redirección."""


class TargetNotFound(LookupError):
    """No hay una aplicación instalada o ventana abierta con ese nombre."""

    def __init__(self, query: str, suggestions: list[str]):
        super().__init__(query)
        self.query = query
        self.suggestions = suggestions


class TargetAmbiguous(LookupError):
    """Varias aplicaciones coinciden por igual con el nombre pedido."""

    def __init__(self, query: str, options: list[str]):
        super().__init__(query)
        self.query = query
        self.options = options


class LocalTools(Protocol):
    def launch_app(self, name: str) -> str: ...
    def focus_window(self, name: str) -> str: ...
    def close_window(self, name: str) -> str: ...
    def minimize_all(self) -> None: ...
    def media_key(self, action: str) -> None: ...
    def open_url(self, url: str) -> None: ...
    def find_video(self, query: str) -> tuple[str, str] | None: ...
    def dismiss(self) -> None: ...
    def open_folder(self, folder: LocalFolder) -> None: ...
    def read_usage(self) -> SystemUsage: ...
    def read_volume(self) -> VolumeStatus: ...
    def set_volume(self, percent: int) -> VolumeStatus: ...
    def list_processes(self) -> list[ProcessInfo]: ...
    def save_memory(self, text: str) -> int: ...
    def list_memories(self) -> list[MemoryItem]: ...
    def forget_memory(self, memory_id: int) -> bool: ...
    def clear_memories(self) -> int: ...
    def read_gpu(self) -> GpuReading | None: ...
    def read_cpu_temperature(self) -> float | None: ...
    def top_programs(self) -> list[ProgramUsage]: ...
    def add_reminder(self, text: str, due: datetime, kind: str) -> int: ...
    def list_reminders(self) -> list[Reminder]: ...
    def cancel_reminder(self, reminder_id: int) -> Reminder | None: ...
    def power(self, action: str) -> None: ...
    def lock(self) -> None: ...
    def cancel_power(self) -> bool: ...
    def agenda(self, start: datetime, end: datetime, now: datetime) -> AgendaData: ...
    def add_task(self, text: str, due: datetime | None, has_time: bool, kind: str) -> int: ...
    def open_tasks(self) -> list[Task]: ...
    def finish_task(self, task_id: int) -> bool: ...
    def remove_task(self, task_id: int) -> bool: ...
    def calendar_names(self) -> list[str]: ...
    def remove_calendar(self, name: str) -> bool: ...
    def request_calendar(self) -> bool: ...
    def weather(self, city: str, day: str) -> str: ...
    def get_city(self) -> str | None: ...
    def set_city(self, city: str) -> None: ...
    def add_wish(self, text: str) -> None: ...
    def list_wishes(self) -> list[str]: ...


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
    FOCUS_WINDOW = "windows.focus"
    CLOSE_WINDOW = "windows.close"
    MINIMIZE_ALL = "windows.minimize_all"
    MEDIA = "media.control"
    OPEN_SITE = "web.open"
    WEB_SEARCH = "web.search"
    PLAY_YOUTUBE = "youtube.play"
    TIME = "system.time"
    DISMISS = "assistant.dismiss"
    GPU = "system.gpu"
    TOP_PROGRAMS = "system.top"
    ADD_REMINDER = "reminders.add"
    LIST_REMINDERS = "reminders.list"
    CANCEL_REMINDER = "reminders.cancel"
    POWER = "system.power"
    LOCK = "system.lock"
    CANCEL_POWER = "system.power_cancel"
    AGENDA = "agenda.list"
    ADD_TASK = "tasks.add"
    FINISH_TASK = "tasks.done"
    REMOVE_TASK = "tasks.remove"
    DAY_SUMMARY = "summary.day"
    CONNECT_CALENDAR = "calendars.connect"
    LIST_CALENDARS = "calendars.list"
    REMOVE_CALENDAR = "calendars.remove"
    WEATHER = "weather.read"
    SET_CITY = "weather.city"
    CALCULATE = "math.calc"
    CPU_TEMPERATURE = "system.cpu_temperature"
    ADD_WISH = "wishes.add"
    LIST_WISHES = "wishes.list"


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
    MANAGE_WINDOWS = "windows.manage"
    CLOSE_WINDOWS = "windows.close"
    MINIMIZE_WINDOWS = "windows.minimize"
    MEDIA = "media.control"
    OPEN_SITE = "web.open"
    WEB_SEARCH = "web.search"
    PLAY_YOUTUBE = "youtube.play"
    READ_TIME = "system.time.read"
    DISMISS = "assistant.dismiss"
    READ_GPU = "system.gpu.read"
    READ_TOP_PROGRAMS = "system.top.read"
    ADD_REMINDER = "reminders.write"
    READ_REMINDERS = "reminders.read"
    CANCEL_REMINDER = "reminders.cancel"
    POWER = "system.power"
    LOCK = "system.lock"
    CANCEL_POWER = "system.power.cancel"
    READ_AGENDA = "agenda.read"
    ADD_TASK = "tasks.write"
    FINISH_TASK = "tasks.done"
    REMOVE_TASK = "tasks.remove"
    DAY_SUMMARY = "summary.read"
    CONNECT_CALENDAR = "calendars.connect"
    READ_CALENDARS = "calendars.read"
    REMOVE_CALENDAR = "calendars.remove"
    READ_WEATHER = "weather.read"
    SET_CITY = "weather.city"
    CALCULATE = "math.calc"
    READ_CPU_TEMPERATURE = "system.cpu_temperature.read"
    ADD_WISH = "wishes.write"
    READ_WISHES = "wishes.read"


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
    Permission.MANAGE_WINDOWS,
    Permission.CLOSE_WINDOWS,
    Permission.MINIMIZE_WINDOWS,
    Permission.MEDIA,
    Permission.OPEN_SITE,
    Permission.WEB_SEARCH,
    Permission.PLAY_YOUTUBE,
    Permission.READ_TIME,
    Permission.DISMISS,
    Permission.READ_GPU,
    Permission.READ_TOP_PROGRAMS,
    Permission.ADD_REMINDER,
    Permission.READ_REMINDERS,
    Permission.CANCEL_REMINDER,
    Permission.POWER,
    Permission.LOCK,
    Permission.CANCEL_POWER,
    Permission.READ_AGENDA,
    Permission.ADD_TASK,
    Permission.FINISH_TASK,
    Permission.REMOVE_TASK,
    Permission.DAY_SUMMARY,
    Permission.CONNECT_CALENDAR,
    Permission.READ_CALENDARS,
    Permission.REMOVE_CALENDAR,
    Permission.READ_WEATHER,
    Permission.SET_CITY,
    Permission.CALCULATE,
    Permission.READ_CPU_TEMPERATURE,
    Permission.ADD_WISH,
    Permission.READ_WISHES,
})

MAX_MEMORY_TEXT = 300


@dataclass(frozen=True)
class ArgumentSpec:
    name: str
    value_type: type
    allowed_values: tuple[object, ...] = ()
    bounds: tuple[int, int] | None = None
    max_length: int | None = None
    pattern: str | None = None

    def accepts(self, value: object) -> bool:
        if type(value) is not self.value_type:
            return False
        if self.allowed_values and value not in self.allowed_values:
            return False
        if self.pattern is not None and not re.fullmatch(self.pattern, cast(str, value).strip()):
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


def _not_found(error: LookupError, what: str) -> ToolOutcome:
    if isinstance(error, TargetAmbiguous):
        options = ", ".join(f"«{option}»" for option in error.options)
        return ToolOutcome(2, "ambigua", f"Hay varias coincidencias para «{error.query}»: {options}. ¿Cuál?")
    query = getattr(error, "query", "")
    suggestions = getattr(error, "suggestions", [])
    hint = f" ¿Quizás {', '.join(f'«{item}»' for item in suggestions)}?" if suggestions else ""
    return ToolOutcome(2, "no_encontrada", f"No encontré «{query}» {what}.{hint}")


def _open_app(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    try:
        name = tools.launch_app(cast(str, arguments["app"]).strip())
    except LookupError as error:
        outcome = _not_found(error, "entre sus aplicaciones instaladas")
        if not isinstance(error, TargetAmbiguous) and not getattr(error, "suggestions", []):
            return ToolOutcome(outcome.code, outcome.status, f"{outcome.message} ¿Lo busco en internet?")
        return outcome
    return ToolOutcome(0, "solicitada", f"Abriendo {name}.")


def _focus_window(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    try:
        title = tools.focus_window(cast(str, arguments["app"]).strip())
    except LookupError as error:
        return _not_found(error, "entre las ventanas abiertas")
    return ToolOutcome(0, "completada", f"Traje al frente «{title}».")


def _close_window(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    try:
        title = tools.close_window(cast(str, arguments["app"]).strip())
    except LookupError as error:
        return _not_found(error, "entre las ventanas abiertas")
    return ToolOutcome(0, "solicitada", f"Pedí cerrar «{title}».")


def _minimize_all(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    tools.minimize_all()
    return ToolOutcome(0, "solicitada", "Minimicé las ventanas.")


MEDIA_LABELS = {
    "play_pause": "Pausé o reanudé la reproducción.",
    "next": "Siguiente pista.",
    "previous": "Pista anterior.",
    "stop": "Detuve la reproducción.",
    "mute": "Activé o desactivé el silencio.",
}


def _media(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    action = cast(str, arguments["action"])
    tools.media_key(action)
    return ToolOutcome(0, "solicitada", MEDIA_LABELS[action])


def _open_site(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    from .web import SITES

    label, url = SITES[cast(str, arguments["site"])]
    tools.open_url(url)
    return ToolOutcome(0, "solicitada", f"Abriendo {label}.")


def _web_search(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    from .web import SEARCH_ENGINES, search_url

    engine, query = cast(str, arguments["engine"]), cast(str, arguments["query"]).strip()
    tools.open_url(search_url(engine, query))
    return ToolOutcome(0, "solicitada", f"Buscando «{query}» en {SEARCH_ENGINES[engine][0]}.")


WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre")


def describe_time(now) -> str:
    article = "Es la" if now.hour % 12 == 1 else "Son las"
    return (f"{article} {now:%H:%M} del {WEEKDAYS[now.weekday()]} {now.day} de "
            f"{MONTHS[now.month - 1]} de {now.year}.")


FAREWELL = "Como desee, señor. Estaré aquí si me necesita; basta con decir «Hey Jarvis»."


def _dismiss(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    tools.dismiss()
    return ToolOutcome(0, "completada", FAREWELL)


def _read_time(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    from datetime import datetime

    return ToolOutcome(0, "completada", describe_time(datetime.now()))


def _play_youtube(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    from .web import search_url

    query = cast(str, arguments["query"]).strip()
    found = tools.find_video(query)
    if found is None:
        tools.open_url(search_url("youtube", query))
        return ToolOutcome(0, "solicitada", f"No pude elegir un video; abrí los resultados de «{query}» en YouTube.")
    url, title = found
    tools.open_url(url)
    return ToolOutcome(0, "solicitada", f"Reproduciendo «{title}» en YouTube.")


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
        return ToolOutcome(0, "completada", "Todavía no recuerdo nada sobre usted.")
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


def _read_gpu(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    gpu = tools.read_gpu()
    if gpu is None:
        return ToolOutcome(2, "no_disponible", "No encontré una GPU NVIDIA con su driver instalado.")
    return ToolOutcome(0, "completada", (
        f"{gpu.name}: {gpu.utilization} % de uso, {gpu.memory_used_mib / 1024:.1f} de "
        f"{gpu.memory_total_mib / 1024:.0f} GiB de VRAM y {gpu.temperature} °C."
    ))


def _read_cpu_temperature(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    value = tools.read_cpu_temperature()
    if value is None:
        return ToolOutcome(2, "no_disponible", "Este equipo no publica su sensor térmico en Windows.")
    note = " Está bastante caliente." if value >= 90 else ""
    return ToolOutcome(0, "completada", f"El procesador ronda los {value:.0f} °C, según el sensor térmico del equipo.{note}")


def _add_wish(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    tools.add_wish(" ".join(cast(str, arguments["text"]).split()))
    return ToolOutcome(0, "completada", "")


def _list_wishes(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    wishes = tools.list_wishes()
    if not wishes:
        return ToolOutcome(0, "completada", "No tengo nada pendiente por aprender.")
    first = "; ".join(wishes[:4])
    rest = "\nDetalle:\n" + "\n".join(f"- {wish}" for wish in wishes) if len(wishes) > 4 else ""
    return ToolOutcome(0, "completada", f"Me gustaría aprender {len(wishes)} cosas: {first}.{rest}"
                       if len(wishes) > 1 else f"Me gustaría aprender esto: {first}.")


def _size(mib: float) -> str:
    return f"{mib / 1024:.1f} GiB" if mib >= 1024 else f"{mib:.0f} MiB"


def _top_programs(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    by_cpu = arguments["by"] == "cpu"
    programs = tools.top_programs()
    key = (lambda item: -item.cpu_percent) if by_cpu else (lambda item: -item.memory_mib)
    if not programs:
        return ToolOutcome(1, "fallida", "No pude leer los programas en ejecución.")
    top = sorted(programs, key=key)[:5]
    if by_cpu:
        top = [item for item in top if item.cpu_percent >= 0.5]
        if not top:
            return ToolOutcome(0, "completada", "El procesador está tranquilo: ningún programa usa más del 0,5 %.")

    def label(item: ProgramUsage) -> str:
        copies = f" ({item.instances} procesos)" if item.instances > 1 else ""
        value = f"{item.cpu_percent:.0f} % de CPU" if by_cpu else _size(item.memory_mib)
        return f"{item.name}{copies}: {value}"

    title = "Lo que más CPU usa ahora" if by_cpu else "Lo que más memoria usa ahora"
    return ToolOutcome(0, "completada", f"{title}: " + "; ".join(label(item) for item in top) + ".")


def _describe_due(moment: datetime) -> str:
    from .when import describe_due

    return describe_due(moment)


def _add_reminder(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    try:
        due = datetime.fromisoformat(cast(str, arguments["due"]).strip())
    except ValueError:
        return ToolOutcome(2, "denegada", "No entendí la fecha u hora del aviso.")
    now = datetime.now()
    if due <= now:
        return ToolOutcome(2, "denegada", "Esa hora ya pasó. Indíqueme una hora futura.")
    if due - now > timedelta(days=366):
        return ToolOutcome(2, "denegada", "Solo programo avisos dentro del próximo año.")
    kind = cast(str, arguments["kind"])
    text = " ".join(cast(str, arguments["text"]).split())
    try:
        tools.add_reminder(text, due, kind)
    except RemindersFull:
        return ToolOutcome(2, "denegada", "Tengo demasiados avisos pendientes. Cancele alguno primero.")
    when = _describe_due(due)
    if kind == "temporizador":
        return ToolOutcome(0, "completada", f"Temporizador en marcha: le aviso {when}.")
    if kind == "alarma":
        return ToolOutcome(0, "completada", f"Alarma programada {when}.")
    what = f"«{text}» " if text else ""
    return ToolOutcome(0, "completada", f"Le recordaré {what}{when}.")


def describe_reminder(reminder: Reminder) -> str:
    what = f" «{reminder.text}»" if reminder.text else ""
    return f"{reminder.kind.capitalize()}{what}, {_describe_due(reminder.due)}"


def _list_reminders(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    reminders = tools.list_reminders()
    if not reminders:
        return ToolOutcome(0, "completada", "No hay recordatorios, alarmas ni temporizadores pendientes.")
    rows = ["Pendientes:"]
    rows.extend(f"{item.id}. {describe_reminder(item)}." for item in reminders)
    return ToolOutcome(0, "completada", "\n".join(rows))


def _cancel_reminder(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    reminder_id, kind = cast(int, arguments["reminder_id"]), cast(str, arguments["kind"])
    matching = [item for item in tools.list_reminders()
                if (kind == "aviso" or item.kind == kind) and reminder_id in (0, item.id)]
    if not matching:
        what = f"un aviso pendiente con el número {reminder_id}" if reminder_id else f"un {kind} pendiente"
        if reminder_id == 0 and kind == "alarma":
            what = "una alarma pendiente"
        return ToolOutcome(2, "no_encontrada", f"No tengo {what}.")
    if len(matching) > 1:  # nunca adivina cuál borrar
        rows = [f"{item.id}. {describe_reminder(item)}." for item in matching]
        return ToolOutcome(2, "ambigua", "\n".join(["Hay varios pendientes:", *rows, "¿Qué número cancelo?"]))
    cancelled = tools.cancel_reminder(matching[0].id)
    if cancelled is None:
        return ToolOutcome(2, "no_encontrada", "Ese aviso ya no está pendiente.")
    return ToolOutcome(0, "completada", f"Cancelado: {describe_reminder(cancelled)}.")


POWER_LABELS = {"shutdown": "Apagaré el equipo", "restart": "Reiniciaré el equipo",
                "sleep": "Suspenderé el equipo"}
POWER_PREVIEWS = {"shutdown": "Apagar el equipo", "restart": "Reiniciar el equipo",
                  "sleep": "Suspender el equipo"}


def _power(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    action = cast(str, arguments["action"])
    tools.power(action)
    return ToolOutcome(0, "solicitada", (
        f"{POWER_LABELS[action]} en {POWER_DELAY_SECONDS[action]} segundos. "
        "Diga «cancela el apagado» si cambia de opinión."
    ))


def _lock(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    tools.lock()
    return ToolOutcome(0, "completada", "Equipo bloqueado.")


def _cancel_power(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    if tools.cancel_power():
        return ToolOutcome(0, "completada", "Cancelado. El equipo seguirá encendido.")
    return ToolOutcome(2, "no_encontrada", "No había ningún apagado, reinicio ni suspensión pendiente.")


def _agenda_text(tools: LocalTools, period: str, kind: str) -> str:
    from .agenda import describe, period_range

    now = datetime.now()
    start, end, label = period_range(period, now)
    data = tools.agenda(start, end, now)
    return describe(data.items, label, kind, now, warnings=data.warnings, calendars=data.calendars)


def _agenda(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    return ToolOutcome(0, "completada", _agenda_text(
        tools, cast(str, arguments["period"]), cast(str, arguments["kind"])))


def _day_summary(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    parts = [describe_time(datetime.now())]
    city = tools.get_city()
    if city:
        try:
            parts.append(tools.weather(city, "hoy"))
        except (OSError, LookupError):
            parts.append("No pude consultar el clima.")
    parts.append(_agenda_text(tools, "hoy", "todo"))
    return ToolOutcome(0, "completada", " ".join(parts))


def _add_task(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    from .agenda import describe_when

    raw = cast(str, arguments["due"]).strip()
    now = datetime.now()
    due, has_time = None, "T" in raw
    if raw:
        try:
            due = datetime.fromisoformat(raw)
        except ValueError:
            return ToolOutcome(2, "denegada", "No entendí la fecha.")
        if due.date() < now.date() or (has_time and due <= now):
            return ToolOutcome(2, "denegada", "Esa fecha ya pasó. Indíqueme una futura.")
    text = " ".join(cast(str, arguments["text"]).split())
    kind = cast(str, arguments["kind"])
    if kind == "evento" and due is None:
        return ToolOutcome(2, "falta_dato", f"¿Para qué día anoto «{text}», señor?")
    try:
        task_id = tools.add_task(text, due, has_time, kind)
    except TasksFull:
        return ToolOutcome(2, "denegada", "Tiene demasiadas tareas abiertas. Termine o borre alguna primero.")
    when = f", {describe_when(due, not has_time, now)}" if due else ", sin fecha"
    return ToolOutcome(0, "completada", f"Anotado ({kind} {task_id}): «{text}»{when}.")


def _pick_task(tools: LocalTools, wanted: str, *, fuzzy: bool = True) -> "Task | ToolOutcome":
    from .agenda import describe_task, find_tasks

    found = find_tasks(tools.open_tasks(), wanted, fuzzy=fuzzy)
    if not found:
        return ToolOutcome(2, "no_encontrada", f"No encontré «{wanted.strip()}» entre lo que anotó aquí. "
                           "Lo que viene de un calendario no se puede cambiar desde JARVIS.")
    if len(found) > 1:
        now = datetime.now()
        rows = [f"{task.id}. {describe_task(task, now)}." for task in found[:5]]
        return ToolOutcome(2, "ambigua", "\n".join(["Hay varias parecidas:", *rows, "¿Qué número?"]))
    return found[0]


def _finish_task(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    task = _pick_task(tools, cast(str, arguments["task"]))
    if isinstance(task, ToolOutcome):
        return task
    if not tools.finish_task(task.id):
        return ToolOutcome(2, "no_encontrada", "Esa tarea ya no está abierta.")
    return ToolOutcome(0, "completada", f"Marcada como hecha: «{task.text}».")


def _remove_task(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    task = _pick_task(tools, cast(str, arguments["task"]), fuzzy=False)
    if isinstance(task, ToolOutcome):
        return task
    if not tools.remove_task(task.id):
        return ToolOutcome(2, "no_encontrada", "Esa tarea ya no está abierta.")
    return ToolOutcome(0, "completada", f"Borrada: «{task.text}».")


def _connect_calendar(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    if not tools.request_calendar():
        return ToolOutcome(2, "no_disponible", "Abra la ventana de JARVIS para conectar un calendario.")
    return ToolOutcome(0, "solicitada", "Le abro la ventana para conectar el calendario. Pegue ahí el enlace; es privado.")


def _list_calendars(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    names = tools.calendar_names()
    if not names:
        return ToolOutcome(0, "completada", "No hay calendarios conectados. Diga «conecta un calendario».")
    return ToolOutcome(0, "completada", f"Calendarios conectados: {', '.join(names)}.")


def _remove_calendar(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    wanted = normalize(cast(str, arguments["name"]))
    name = next((item for item in tools.calendar_names() if normalize(item) == wanted), None)
    if name is None or not tools.remove_calendar(name):
        return ToolOutcome(2, "no_encontrada", f"No hay un calendario llamado «{arguments['name']}».")
    return ToolOutcome(0, "completada", f"Desconecté {name} y borré su enlace.")


def _weather(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    city = cast(str, arguments["city"]).strip() or tools.get_city()
    if not city:
        return ToolOutcome(2, "falta_dato", "¿De qué ciudad, señor? Si dice «mi ciudad es…», la recordaré.")
    try:
        return ToolOutcome(0, "completada", tools.weather(city, cast(str, arguments["day"])))
    except LookupError:
        return ToolOutcome(2, "no_encontrada", f"No encontré la ciudad «{city}».")


def _set_city(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    city = " ".join(cast(str, arguments["city"]).split())
    tools.set_city(city)
    return ToolOutcome(0, "completada", f"Usaré {city} para el clima.")


def _calculate(tools: LocalTools, arguments: ToolArguments) -> ToolOutcome:
    from .calc import CalcError, calculate

    try:
        return ToolOutcome(0, "completada", f"El resultado es {calculate(cast(str, arguments['expression']))}.")
    except CalcError as error:
        return ToolOutcome(2, "denegada", str(error))


TOOLS: Mapping[ToolId, ToolSpec] = MappingProxyType({
    tool.id: tool for tool in (
        ToolSpec(
            id=ToolId.OPEN_APP,
            permission=Permission.OPEN_APP,
            error_target="abrir la aplicación",
            handler=_open_app,
            parameters=(ArgumentSpec("app", str, pattern=NAME_PATTERN),),
            invalid_arguments="Indica el nombre de una aplicación instalada, sin rutas. Ejemplo «abre Discord».",
        ),
        ToolSpec(
            id=ToolId.FOCUS_WINDOW,
            permission=Permission.MANAGE_WINDOWS,
            error_target="traer la ventana al frente",
            handler=_focus_window,
            parameters=(ArgumentSpec("app", str, pattern=NAME_PATTERN),),
            invalid_arguments="Indica el nombre de la aplicación. Ejemplo «cambia a Discord».",
        ),
        ToolSpec(
            id=ToolId.CLOSE_WINDOW,
            permission=Permission.CLOSE_WINDOWS,
            error_target="cerrar la ventana",
            handler=_close_window,
            parameters=(ArgumentSpec("app", str, pattern=NAME_PATTERN),),
            invalid_arguments="Indica el nombre de la aplicación. Ejemplo «cierra Discord».",
            approval_preview=lambda args: (
                f"Cerrar la ventana de «{args['app']}». Si tiene cambios sin guardar, la aplicación lo preguntará."
            ),
        ),
        ToolSpec(
            id=ToolId.MINIMIZE_ALL,
            permission=Permission.MINIMIZE_WINDOWS,
            error_target="minimizar las ventanas",
            handler=_minimize_all,
        ),
        ToolSpec(
            id=ToolId.MEDIA,
            permission=Permission.MEDIA,
            error_target="enviar la tecla multimedia",
            handler=_media,
            parameters=(ArgumentSpec("action", str, allowed_values=MEDIA_ACTIONS),),
            invalid_arguments="Acción multimedia no válida.",
        ),
        ToolSpec(
            id=ToolId.OPEN_SITE,
            permission=Permission.OPEN_SITE,
            error_target="abrir el sitio",
            handler=_open_site,
            parameters=(ArgumentSpec("site", str, allowed_values=SITE_KEYS),),
            invalid_arguments="Ese sitio no está en la lista permitida.",
        ),
        ToolSpec(
            id=ToolId.WEB_SEARCH,
            permission=Permission.WEB_SEARCH,
            error_target="abrir la búsqueda",
            handler=_web_search,
            parameters=(
                ArgumentSpec("engine", str, allowed_values=SEARCH_ENGINE_KEYS),
                ArgumentSpec("query", str, max_length=200),
            ),
            invalid_arguments="Indica qué buscar y dónde: Google, YouTube o Wikipedia.",
        ),
        ToolSpec(
            id=ToolId.DISMISS,
            permission=Permission.DISMISS,
            error_target="retirarme",
            handler=_dismiss,
        ),
        ToolSpec(
            id=ToolId.TIME,
            permission=Permission.READ_TIME,
            error_target="consultar la hora",
            handler=_read_time,
        ),
        ToolSpec(
            id=ToolId.PLAY_YOUTUBE,
            permission=Permission.PLAY_YOUTUBE,
            error_target="reproducir en YouTube",
            handler=_play_youtube,
            parameters=(ArgumentSpec("query", str, max_length=200),),
            invalid_arguments="Indica qué reproducir. Ejemplo «reproduce Back in Black en YouTube».",
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
        ToolSpec(
            id=ToolId.GPU,
            permission=Permission.READ_GPU,
            error_target="consultar la GPU",
            handler=_read_gpu,
        ),
        ToolSpec(
            id=ToolId.TOP_PROGRAMS,
            permission=Permission.READ_TOP_PROGRAMS,
            error_target="medir los programas en ejecución",
            handler=_top_programs,
            parameters=(ArgumentSpec("by", str, allowed_values=("ram", "cpu")),),
            invalid_arguments="Indica si ordenar por RAM o por CPU.",
        ),
        ToolSpec(
            id=ToolId.ADD_REMINDER,
            permission=Permission.ADD_REMINDER,
            error_target="guardar el aviso",
            handler=_add_reminder,
            parameters=(
                ArgumentSpec("text", str, pattern=rf"[^\n]{{0,{MAX_REMINDER_TEXT}}}"),
                ArgumentSpec("due", str, pattern=DUE_PATTERN),
                ArgumentSpec("kind", str, allowed_values=REMINDER_KINDS),
            ),
            invalid_arguments="Indica qué recordar y cuándo. Ejemplo «recuérdame en 20 minutos sacar la ropa».",
        ),
        ToolSpec(
            id=ToolId.LIST_REMINDERS,
            permission=Permission.READ_REMINDERS,
            error_target="leer los avisos",
            handler=_list_reminders,
        ),
        ToolSpec(
            id=ToolId.CANCEL_REMINDER,
            permission=Permission.CANCEL_REMINDER,
            error_target="cancelar el aviso",
            handler=_cancel_reminder,
            parameters=(
                ArgumentSpec("reminder_id", int, bounds=(0, 2**31 - 1)),
                ArgumentSpec("kind", str, allowed_values=(*REMINDER_KINDS, "aviso")),
            ),
            invalid_arguments="Indica el número del aviso. Ejemplo «cancela el recordatorio 2».",
        ),
        ToolSpec(
            id=ToolId.POWER,
            permission=Permission.POWER,
            error_target="cambiar el estado de energía",
            handler=_power,
            parameters=(ArgumentSpec("action", str, allowed_values=POWER_ACTIONS),),
            invalid_arguments="Acción de energía no válida: apagar, reiniciar o suspender.",
            approval_preview=lambda args: (
                f"{POWER_PREVIEWS[cast(str, args['action'])]} dentro de "
                f"{POWER_DELAY_SECONDS[cast(str, args['action'])]} segundos."
            ),
        ),
        ToolSpec(
            id=ToolId.LOCK,
            permission=Permission.LOCK,
            error_target="bloquear el equipo",
            handler=_lock,
        ),
        ToolSpec(
            id=ToolId.CANCEL_POWER,
            permission=Permission.CANCEL_POWER,
            error_target="cancelar el apagado",
            handler=_cancel_power,
        ),
        ToolSpec(
            id=ToolId.AGENDA,
            permission=Permission.READ_AGENDA,
            error_target="consultar la agenda",
            handler=_agenda,
            parameters=(
                ArgumentSpec("period", str, allowed_values=AGENDA_PERIODS),
                ArgumentSpec("kind", str, allowed_values=AGENDA_KINDS),
            ),
            invalid_arguments="Indica un periodo: hoy, mañana, un día de la semana, esta semana, la próxima o este mes.",
        ),
        ToolSpec(
            id=ToolId.DAY_SUMMARY,
            permission=Permission.DAY_SUMMARY,
            error_target="preparar el resumen del día",
            handler=_day_summary,
        ),
        ToolSpec(
            id=ToolId.ADD_TASK,
            permission=Permission.ADD_TASK,
            error_target="anotar la tarea",
            handler=_add_task,
            parameters=(
                ArgumentSpec("text", str, max_length=MAX_REMINDER_TEXT),
                ArgumentSpec("due", str, pattern=TASK_DUE_PATTERN),
                ArgumentSpec("kind", str, allowed_values=TASK_KINDS),
            ),
            invalid_arguments="Indica qué anotar. Ejemplo «anota la tarea leer el capítulo 3».",
        ),
        ToolSpec(
            id=ToolId.FINISH_TASK,
            permission=Permission.FINISH_TASK,
            error_target="marcar la tarea",
            handler=_finish_task,
            parameters=(ArgumentSpec("task", str, max_length=MAX_REMINDER_TEXT),),
            invalid_arguments="Indica qué tarea terminó. Ejemplo «marca como hecha la tarea 2».",
        ),
        ToolSpec(
            id=ToolId.REMOVE_TASK,
            permission=Permission.REMOVE_TASK,
            error_target="borrar la tarea",
            handler=_remove_task,
            parameters=(ArgumentSpec("task", str, max_length=MAX_REMINDER_TEXT),),
            invalid_arguments="Indica qué tarea borrar. Ejemplo «borra la tarea 2».",
        ),
        ToolSpec(
            id=ToolId.CONNECT_CALENDAR,
            permission=Permission.CONNECT_CALENDAR,
            error_target="abrir la ventana de calendarios",
            handler=_connect_calendar,
        ),
        ToolSpec(
            id=ToolId.LIST_CALENDARS,
            permission=Permission.READ_CALENDARS,
            error_target="leer los calendarios",
            handler=_list_calendars,
        ),
        ToolSpec(
            id=ToolId.REMOVE_CALENDAR,
            permission=Permission.REMOVE_CALENDAR,
            error_target="desconectar el calendario",
            handler=_remove_calendar,
            parameters=(ArgumentSpec("name", str, max_length=40),),
            invalid_arguments="Indica qué calendario desconectar.",
            approval_preview=lambda args: f"Desconectar el calendario «{args['name']}» y borrar su enlace.",
        ),
        ToolSpec(
            id=ToolId.WEATHER,
            permission=Permission.READ_WEATHER,
            error_target="consultar el clima",
            handler=_weather,
            parameters=(
                ArgumentSpec("city", str, pattern=rf"(?:{CITY_PATTERN})?"),
                ArgumentSpec("day", str, allowed_values=("hoy", "manana")),
            ),
            invalid_arguments="Solo puedo dar el clima de hoy o de mañana en una ciudad.",
        ),
        ToolSpec(
            id=ToolId.SET_CITY,
            permission=Permission.SET_CITY,
            error_target="recordar la ciudad",
            handler=_set_city,
            parameters=(ArgumentSpec("city", str, pattern=CITY_PATTERN),),
            invalid_arguments="Indica una ciudad. Ejemplo «mi ciudad es Medellín».",
        ),
        ToolSpec(
            id=ToolId.CALCULATE,
            permission=Permission.CALCULATE,
            error_target="calcular",
            handler=_calculate,
            parameters=(ArgumentSpec("expression", str, pattern=r"[\d+\-*/().,%^ sqrt×÷]{1,120}"),),
            invalid_arguments="Solo puedo calcular con números y operaciones.",
        ),
        ToolSpec(
            id=ToolId.CPU_TEMPERATURE,
            permission=Permission.READ_CPU_TEMPERATURE,
            error_target="leer la temperatura",
            handler=_read_cpu_temperature,
        ),
        ToolSpec(
            id=ToolId.ADD_WISH,
            permission=Permission.ADD_WISH,
            error_target="anotar lo que falta",
            handler=_add_wish,
            parameters=(ArgumentSpec("text", str, max_length=120),),
            invalid_arguments="Indica en pocas palabras qué falta.",
        ),
        ToolSpec(
            id=ToolId.LIST_WISHES,
            permission=Permission.READ_WISHES,
            error_target="leer lo que falta",
            handler=_list_wishes,
        ),
    )
})
