import re
import sqlite3
from collections.abc import Callable, Set
from dataclasses import dataclass
from time import monotonic, perf_counter
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from .audit import Audit
from .brain import BrainUnavailable, Decision
from .web import site_key
from . import persona as jarvis_persona
from .tools import (
    DEFAULT_PERMISSIONS,
    TOOLS,
    AudioOutputUnavailable,
    FolderRedirected,
    LocalFolder,
    LocalTools,
    MemoryFull,
    Permission,
    ToolArguments,
    ToolId,
    ToolOutcome,
    ToolSpec,
    normalize,
)
from .tools import ProcessInfo as ProcessInfo
from .tools import SystemUsage as SystemUsage
from .tools import VolumeStatus as VolumeStatus

FOLDER_ALIASES = {
    "del proyecto": LocalFolder.PROJECT,
    "proyecto": LocalFolder.PROJECT,
    "de documentacion": LocalFolder.DOCUMENTATION,
    "documentacion": LocalFolder.DOCUMENTATION,
    "docs": LocalFolder.DOCUMENTATION,
}
METRIC_COMMANDS = {"consulta cpu y ram", "estado del sistema"}
VOLUME_COMMANDS = {"consulta el volumen", "consulta volumen", "estado del volumen"}
VOLUME_SET_PREFIXES = ("pon el volumen", "ajusta el volumen")
VOLUME_SET_PATTERN = re.compile(r"(?:pon|ajusta) el volumen al ([0-9]{1,3})(?: ?%)?")
PROCESS_COMMANDS = {"lista procesos", "listar procesos"}
REMEMBER_PREFIXES = ("recuerda que ", "recuerda: ")
REMEMBER_PATTERN = re.compile(
    r"\s*(?:jarvis[\s,:]+)?[¿¡]?\s*recuerda(?:\s+que|\s*:)\s*(.*?)\s*[.!]?\s*$", re.IGNORECASE | re.DOTALL
)
WAKE_PREFIX = re.compile(r"^(?:oye |hey |ok )?jarvis[\s,:.!]+(?=\S)")
MEDIA_COMMANDS = {
    "play_pause": {"pausa", "pausa la musica", "pausa la cancion", "pausa el video", "reanuda",
                   "reanuda la musica", "continua la musica", "play", "dale play", "pon play"},
    "next": {"siguiente", "siguiente cancion", "la siguiente", "pasa la cancion", "salta la cancion",
             "siguiente tema", "cambia la cancion"},
    "previous": {"anterior", "cancion anterior", "la anterior", "vuelve a la cancion anterior"},
    "mute": {"silencia", "silencia el audio", "mutea", "quita el silencio"},
}
# «Eso sería todo, Jarvis»: se quita el nombre y la cortesía y se compara lo esencial.
DISMISS_PATTERN = re.compile(
    r"(?:(?:muchas )?gracias,? )?(?:eso (?:seria|es|fue|era) todo|eso nomas|es todo|nada mas|"
    r"puedes retirarte|retirate|descansa|ocultate|escondete|desaparece|adios|chao|hasta luego|"
    r"hasta manana|nos vemos|"
    r"(?:esconde|oculta|quita) (?:el overlay|la interfaz|el orbe|todo))"
    r"(?: por (?:hoy|ahora|el momento))?(?:,? (?:muchas )?gracias)?"
    r"|(?:bueno |listo |ya )?me voy a (?:dormir|descansar|acostar)(?: .*)?"
)
TIME_COMMANDS = {
    "que hora es", "dime la hora", "que hora tenemos", "que dia es hoy", "que fecha es hoy",
    "a que estamos hoy", "que dia es", "que fecha es",
}
MINIMIZE_COMMANDS = {"minimiza todo", "minimiza todas las ventanas", "muestra el escritorio"}
YOUTUBE_PLAY = (
    re.compile(r"(?:abre youtube y )?(?:reproduce|pon|ponme|reproduceme)(?: la cancion| el video| la musica)? (.+?) en youtube"),
    re.compile(r"abre youtube y (?:reproduce|pon|ponme|reproduceme)(?: la cancion| el video| la musica)? (.+)"),
)
SEARCH_PATTERN = re.compile(r"(?:busca|buscame) (.+?) en (google|youtube|wikipedia)")
YOUTUBE_SEARCH = re.compile(r"abre youtube y busca (.+)")
FOCUS_PATTERNS = (
    re.compile(r"(?:cambia a|cambiate a|ve a|trae al frente|pon al frente|muestrame) (.+)"),
    re.compile(r"(?:pon|trae) (.+?) al frente"),
)
CLOSE_PATTERN = re.compile(r"cierra (?:la ventana de |la ventana del |el |la )?(.+)")
MEMORY_LIST_COMMANDS = {
    "que recuerdas", "que recuerdas de mi", "que sabes de mi", "lista recuerdos",
    "muestra tu memoria", "muestra tus recuerdos",
}
FORGET_PATTERN = re.compile(r"olvida (?:el )?recuerdo (?:numero |n\.?o )?([0-9]{1,9})")
CLEAR_MEMORY_COMMANDS = {"olvida todo", "olvida todo lo que sabes de mi", "borra tu memoria"}
MEMORY_MUTATIONS = {ToolId.SAVE_MEMORY, ToolId.FORGET_MEMORY, ToolId.CLEAR_MEMORIES}
SENSITIVE_PATTERN = re.compile(r"contrasen|password|clave|\bpin\b|tarjeta|cvv|token|cuenta bancaria")
APPROVAL_TIMEOUT_SECONDS = 60
SEQUENCE_SEPARATOR = " y luego "
MAX_SEQUENCE_STEPS = 5
SEQUENCE_ACTION = "workflow.sequence"
BRAIN_ACTION = "assistant.brain"
CANCELLED_BEFORE_EXECUTION = ToolOutcome(130, "cancelada", "Paso cancelado antes de ejecutar.")

HELP = (
    "Puedo abrir aplicaciones («abre Discord»), sitios («abre YouTube»), reproducir en YouTube "
    "(«reproduce Back in Black en YouTube»), buscar («busca recetas en Google»), controlar la música "
    "(«pausa», «siguiente canción»), manejar ventanas («cambia a Opera», «cierra Discord», "
    "«minimiza todo»), consultar CPU, RAM y volumen, ajustar el volumen, listar procesos y "
    "recordar cosas («recuerda que…», «qué recuerdas», «olvida el recuerdo N»). "
    "Encadena hasta cinco órdenes con «y luego»."
)


class Brain(Protocol):
    def decide(self, text: str) -> Decision: ...
    def observe(self, user_text: str, outcome: str, *, source: str) -> None: ...
    def notify_memory_changed(self) -> None: ...


@dataclass(frozen=True)
class Result:
    code: int
    message: str
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    tool_id: ToolId
    arguments: ToolArguments
    summary: str
    timeout_seconds: int
    step_index: int | None = None


Approver = Callable[[ApprovalRequest], bool]
RoutedCommand = tuple[ToolId | None, ToolArguments]


@dataclass(frozen=True)
class _Execution:
    request_id: str
    audit: Audit
    permissions: Set[Permission]
    approve: Approver | None
    cancel_requested: Callable[[], bool] | None

    def cancelled(self) -> bool:
        return self.cancel_requested is not None and self.cancel_requested()

    def record(
        self,
        action: str,
        status: str,
        elapsed_ms: float | None = None,
        *,
        step_index: int | None = None,
    ) -> None:
        self.audit.record(self.request_id, action, status, elapsed_ms, step_index=step_index)


def _restore(original: str | None, fragment: str) -> str:
    """Recupera del texto original un fragmento reconocido en su forma normalizada.

    Así una búsqueda conserva tildes y mayúsculas («café», «Queen») aunque la orden se
    reconozca sin ellas.
    """
    if not original:
        return fragment
    chars, positions = [], []
    for index, char in enumerate(original):
        for piece in (" " if char.isspace() else normalize(char)):
            chars.append(piece)
            positions.append(index)
    words = [re.escape(word) for word in fragment.split()]
    match = re.search(r"\s+".join(words), "".join(chars)) if words else None
    if match is None:
        return fragment
    return original[positions[match.start()]: positions[match.end() - 1] + 1].strip()


def _route(command: str, original: str | None = None) -> RoutedCommand:
    command = WAKE_PREFIX.sub("", command.strip(" ¿?¡!.")).strip(" ¿?¡!.")
    if command.startswith(REMEMBER_PREFIXES):
        match = REMEMBER_PATTERN.match(original if original is not None else command)
        return ToolId.SAVE_MEMORY, {"text": match[1] if match else ""}
    if command in MEMORY_LIST_COMMANDS:
        return ToolId.LIST_MEMORIES, {}
    if command in CLEAR_MEMORY_COMMANDS:
        return ToolId.CLEAR_MEMORIES, {}
    forget = FORGET_PATTERN.fullmatch(command)
    if forget:
        return ToolId.FORGET_MEMORY, {"memory_id": int(forget[1])}
    if command in METRIC_COMMANDS:
        return ToolId.SYSTEM_METRICS, {}
    if command in VOLUME_COMMANDS:
        return ToolId.READ_VOLUME, {}
    if command.startswith(VOLUME_SET_PREFIXES):
        match = VOLUME_SET_PATTERN.fullmatch(command)
        return ToolId.SET_VOLUME, {"percent": int(match[1]) if match else None}
    if command in PROCESS_COMMANDS:
        return ToolId.LIST_PROCESSES, {}
    for action, phrases in MEDIA_COMMANDS.items():
        if command in phrases:
            return ToolId.MEDIA, {"action": action}
    if command in MINIMIZE_COMMANDS:
        return ToolId.MINIMIZE_ALL, {}
    if command in TIME_COMMANDS:
        return ToolId.TIME, {}
    farewell = re.sub(r"[,.!¡¿?]", " ", command)
    farewell = " ".join(word for word in farewell.split() if word not in {"jarvis", "señor", "senor"})
    if DISMISS_PATTERN.fullmatch(farewell):
        return ToolId.DISMISS, {}
    for pattern in YOUTUBE_PLAY:
        match = pattern.fullmatch(command)
        if match:
            return ToolId.PLAY_YOUTUBE, {"query": _restore(original, match[1])}
    match = YOUTUBE_SEARCH.fullmatch(command)
    if match:
        return ToolId.WEB_SEARCH, {"engine": "youtube", "query": _restore(original, match[1])}
    match = SEARCH_PATTERN.fullmatch(command)
    if match:
        return ToolId.WEB_SEARCH, {"engine": match[2], "query": _restore(original, match[1])}
    for pattern in FOCUS_PATTERNS:
        match = pattern.fullmatch(command)
        if match:
            return ToolId.FOCUS_WINDOW, {"app": match[1]}
    match = CLOSE_PATTERN.fullmatch(command)
    if match and " y " not in match[1]:
        return ToolId.CLOSE_WINDOW, {"app": match[1]}

    verb, _, argument = command.partition(" ")
    if verb in {"abre", "abrir", "abreme", "inicia", "ejecuta", "lanza"} and argument:
        target_type, _, folder_alias = argument.removeprefix("la ").partition(" ")
        if target_type == "carpeta":
            return ToolId.OPEN_FOLDER, {"folder": FOLDER_ALIASES.get(folder_alias)}
        site = site_key(argument)
        if site is not None:
            return ToolId.OPEN_SITE, {"site": site}
        if " y " in argument:
            return None, {}  # órdenes compuestas: mejor que las interprete la IA
        return ToolId.OPEN_APP, {"app": argument}
    return None, {}


def _request_approval(
    request: ApprovalRequest, execution: _Execution
) -> ToolOutcome | None:
    if execution.approve is None:
        return ToolOutcome(2, "aprobacion_no_disponible", "Esta acción requiere aprobación.")

    try:
        execution.record(request.tool_id.value, "aprobacion_solicitada", step_index=request.step_index)
    except (OSError, sqlite3.Error):
        return ToolOutcome(
            1, "fallida", "No se pudo registrar la solicitud de aprobación. Orden detenida."
        )

    if execution.cancelled():
        return CANCELLED_BEFORE_EXECUTION
    deadline = monotonic() + request.timeout_seconds
    try:
        decision = execution.approve(request)
    except KeyboardInterrupt:
        return ToolOutcome(130, "interrumpida", "Aprobación interrumpida. Acción cancelada.")
    except (EOFError, OSError):
        return ToolOutcome(
            2, "aprobacion_no_disponible", "La aprobación requiere una respuesta desde la terminal."
        )
    if execution.cancelled():
        return CANCELLED_BEFORE_EXECUTION
    if monotonic() >= deadline:
        return ToolOutcome(
            2, "aprobacion_vencida", "La solicitud venció. Vuelve a dar la orden si deseas aplicarla."
        )
    if decision is not True:
        return ToolOutcome(2, "aprobacion_rechazada", "Acción rechazada por el usuario.")

    try:
        execution.record(request.tool_id.value, "aprobada", step_index=request.step_index)
    except (OSError, sqlite3.Error):
        return ToolOutcome(1, "fallida", "No se pudo guardar la aprobación. Orden detenida.")
    return None


def _validate(
    tool_id: ToolId | None,
    arguments: ToolArguments,
    permissions: Set[Permission],
) -> ToolSpec | ToolOutcome:
    tool = TOOLS.get(tool_id) if tool_id is not None else None
    if tool is None:
        return ToolOutcome(2, "no_reconocida", f"Orden no reconocida. {HELP}")
    if tool.permission not in permissions:
        return ToolOutcome(2, "denegada", "Permiso insuficiente para esta herramienta.")
    if not tool.accepts_arguments(arguments):
        return ToolOutcome(2, "denegada", tool.invalid_arguments)
    return tool


def _execute(
    tool_id: ToolId | None,
    arguments: ToolArguments,
    tools: LocalTools,
    execution: _Execution,
    step_index: int | None,
) -> ToolOutcome:
    if execution.cancelled():
        return CANCELLED_BEFORE_EXECUTION
    tool = _validate(tool_id, arguments, execution.permissions)
    if isinstance(tool, ToolOutcome):
        return tool
    if tool.approval_preview is not None:
        arguments = MappingProxyType(dict(arguments))
        request = ApprovalRequest(
            execution.request_id, tool.id, arguments, tool.approval_preview(arguments),
            APPROVAL_TIMEOUT_SECONDS, step_index,
        )
        refusal = _request_approval(request, execution)
        if refusal is not None:
            return refusal
        if tool.permission not in execution.permissions:
            return ToolOutcome(2, "denegada", "Permiso insuficiente para esta herramienta.")

    if execution.cancelled():
        return CANCELLED_BEFORE_EXECUTION
    try:
        return tool.handler(tools, arguments)
    except AudioOutputUnavailable:
        return ToolOutcome(1, "fallida", "Windows no tiene una salida de audio predeterminada.")
    except FolderRedirected:
        return ToolOutcome(2, "denegada", "La carpeta está redirigida. Apertura denegada.")
    except MemoryFull:
        return ToolOutcome(
            2, "denegada", "Mi memoria está llena (50 recuerdos). Olvida alguno antes de guardar otro."
        )
    except sqlite3.Error:
        return ToolOutcome(1, "fallida", f"No pude {tool.error_target}: falló la base de datos local.")
    except OSError as error:
        error_code = getattr(error, "winerror", None) or error.errno
        detail = f" (código {error_code})" if error_code is not None else ""
        return ToolOutcome(1, "fallida", f"Windows no pudo {tool.error_target}{detail}.")


def _record_outcome(
    execution: _Execution,
    action: str,
    outcome: ToolOutcome,
    started: float,
    *,
    step_index: int | None = None,
) -> Result:
    elapsed_ms = (perf_counter() - started) * 1000
    code, message = outcome.code, outcome.message
    try:
        execution.record(action, outcome.status, elapsed_ms, step_index=step_index)
    except (OSError, sqlite3.Error):
        message += " No se pudo guardar el resultado en el registro de auditoría."
        code = code or 1
    return Result(code, message, elapsed_ms)


def _run_step(
    route: RoutedCommand,
    tools: LocalTools,
    execution: _Execution,
    *,
    step_index: int | None = None,
) -> Result:
    started = perf_counter()
    tool_id, arguments = route
    action = tool_id.value if tool_id is not None else "unknown"
    try:
        execution.record(action, "recibida", step_index=step_index)
    except (OSError, sqlite3.Error):
        return Result(1, "No se pudo iniciar el registro de auditoría. Orden detenida.")

    try:
        outcome = _execute(tool_id, arguments, tools, execution, step_index)
    except KeyboardInterrupt:
        outcome = ToolOutcome(
            130, "interrumpida", "Orden interrumpida. Comprueba el estado antes de repetirla."
        )
    return _record_outcome(execution, action, outcome, started, step_index=step_index)


def _sequence_outcome(commands: list[str], tools: LocalTools, execution: _Execution) -> ToolOutcome:
    total = len(commands)
    if total > MAX_SEQUENCE_STEPS:
        return ToolOutcome(2, "denegada", f"Encadena como máximo {MAX_SEQUENCE_STEPS} órdenes.")

    routes = [_route(command) for command in commands]
    for index, (tool_id, arguments) in enumerate(routes, start=1):
        check = _validate(tool_id, arguments, execution.permissions)
        if isinstance(check, ToolOutcome):
            return ToolOutcome(
                2, "denegada", f"Paso {index}. {check.message}\nPasos confirmados 0 de {total}."
            )

    lines = []
    completed = 0
    code = 0
    try:
        for index, route in enumerate(routes, start=1):
            if execution.cancelled():
                code = 130
                break
            step = _run_step(route, tools, execution, step_index=index)
            lines.append(f"Paso {index}. {step.message}")
            if step.code:
                code = step.code
                break
            completed += 1
    except KeyboardInterrupt:
        code = 130

    if code == 130:
        status, summary = "cancelada", "Tarea cancelada."
    elif code:
        status = "denegada" if code == 2 else "fallida"
        summary = "Tarea detenida."
    else:
        status, summary = "completada", "Tarea ejecutada."
    lines.append(f"{summary} Pasos confirmados {completed} de {total}.")
    return ToolOutcome(code, status, "\n".join(lines))


def _route_from_model(tool: str, arguments: dict) -> RoutedCommand:
    """Convierte la propuesta JSON del modelo en una ruta tipada. El registro la valida después."""
    try:
        tool_id = ToolId(tool)
    except ValueError:
        return None, {}
    converted: dict[str, object] = dict(arguments)
    site = converted.get("site")
    if tool_id is ToolId.OPEN_SITE and isinstance(site, str):
        converted["site"] = site_key(site) or site
    engine = converted.get("engine")
    if tool_id is ToolId.WEB_SEARCH and isinstance(engine, str):
        converted["engine"] = normalize(engine)
    folder = converted.get("folder")
    if tool_id is ToolId.OPEN_FOLDER and isinstance(folder, str):
        converted["folder"] = FOLDER_ALIASES.get(normalize(folder).removeprefix("la carpeta "), folder)
    return tool_id, converted


def _notify_memory(brain: "Brain | None", route: RoutedCommand, result: Result) -> None:
    if brain is not None and route[0] in MEMORY_MUTATIONS and result.code == 0:
        brain.notify_memory_changed()


def _run_with_brain(
    text: str, tools: LocalTools, execution: _Execution, brain: Brain, persona: bool = False
) -> Result:
    started = perf_counter()
    try:
        execution.record(BRAIN_ACTION, "recibida")
    except (OSError, sqlite3.Error):
        return Result(1, "No se pudo iniciar el registro de auditoría. Orden detenida.")
    if execution.cancelled():
        return _record_outcome(execution, BRAIN_ACTION, CANCELLED_BEFORE_EXECUTION, started)
    try:
        decision = brain.decide(text)
    except BrainUnavailable:
        outcome = ToolOutcome(
            1, "fallida", f"No reconocí la orden y no pude consultar a la IA en este momento. {HELP}"
        )
        return _record_outcome(execution, BRAIN_ACTION, outcome, started)
    except KeyboardInterrupt:
        outcome = ToolOutcome(130, "interrumpida", "Consulta a la IA interrumpida.")
        return _record_outcome(execution, BRAIN_ACTION, outcome, started)
    _record_outcome(execution, BRAIN_ACTION, ToolOutcome(0, "completada", ""), started)

    messages: list[str] = []
    code = 0
    source = "reply"
    if decision.tool is None:
        messages.append(decision.reply or "No tengo una respuesta para eso.")
    else:
        route = _route_from_model(decision.tool, decision.arguments)
        step = _run_step(route, tools, execution)
        _notify_memory(brain, route, step)
        if persona and decision.reply and step.code == 0 and route[0] is not ToolId.DISMISS:
            messages.append(f"{decision.reply} {step.message}")
        elif persona:
            messages.append(jarvis_persona.dress(route[0], step.code, step.message))  # la despedida va sola
        else:
            messages.append(step.message)
        code, source = step.code, "tool"

    if decision.remember:
        if SENSITIVE_PATTERN.search(normalize(decision.remember)):
            messages.append("No guardo contraseñas ni datos sensibles en mi memoria.")
        else:
            saved = _run_step((ToolId.SAVE_MEMORY, {"text": decision.remember}), tools, execution)
            if saved.code == 0:
                messages.append(saved.message)

    result = Result(code, "\n".join(messages), (perf_counter() - started) * 1000)
    brain.observe(text, result.message, source=source)
    return result


def run_command(
    text: str,
    tools: LocalTools,
    audit: Audit,
    *,
    permissions: Set[Permission] = DEFAULT_PERMISSIONS,
    approve: Approver | None = None,
    cancel_requested: Callable[[], bool] | None = None,
    brain: Brain | None = None,
    persona: bool = False,
) -> Result:
    started = perf_counter()
    command = normalize(text)
    if command == "ayuda":
        return Result(0, HELP)

    execution = _Execution(uuid4().hex, audit, permissions, approve, cancel_requested)
    if SEQUENCE_SEPARATOR not in command:
        route = _route(command, text)
        if route[0] is None and brain is not None:
            return _run_with_brain(text, tools, execution, brain, persona)
        result = _run_step(route, tools, execution)
        _notify_memory(brain, route, result)
        if persona:
            result = Result(result.code, jarvis_persona.dress(route[0], result.code, result.message),
                            result.elapsed_ms)
        if brain is not None:
            brain.observe(text, result.message, source="direct")
        return result

    try:
        execution.record(SEQUENCE_ACTION, "recibida")
    except (OSError, sqlite3.Error):
        return Result(1, "No se pudo iniciar el registro de auditoría. Tarea detenida.")
    components = command.split(SEQUENCE_SEPARATOR, MAX_SEQUENCE_STEPS)
    outcome = _sequence_outcome(components, tools, execution)
    return _record_outcome(execution, SEQUENCE_ACTION, outcome, started)
