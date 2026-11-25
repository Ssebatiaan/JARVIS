"""Cerebro de JARVIS: un proceso persistente de Claude Code que traduce frases libres a herramientas.

Claude solo propone una herramienta en JSON. El núcleo de JARVIS valida el permiso y los
argumentos y ejecuta la acción; el texto del modelo nunca se ejecuta como comando.
"""

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from datetime import datetime
from time import monotonic

from .memory import MemoryStore

DEFAULT_MODEL = "haiku"
IDLE_SECONDS = 600
MAX_TURNS = 30
RESPONSE_TIMEOUT = 15.0
HISTORY_IN_PROMPT = 10
# La línea de comandos de Windows admite 32 767 caracteres; se deja margen para el resto.
MAX_PROMPT_CHARS = 24_000
NOTE_LIMIT = 300

TIME_NOTE_SECONDS = 300
TIME_WORDS = re.compile(
    r"\b(?:horas?|fechas?|hoy|mañana|tarde|noche|d[ií]as?|minutos?|recuerd\w*|recu[eé]rd\w*|av[ií]s\w*|alarmas?|"
    r"temporizador\w*|despi[eé]rt\w*|semanas?|mes|meses|lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo|"
    r"enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b", re.IGNORECASE
)
WEEKDAYS = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
          "octubre", "noviembre", "diciembre")

TOOL_CATALOG = (
    ("apps.open", '{"app": "nombre de la aplicación"}',
     "abre una aplicación instalada (Discord, Steam, Spotify, Opera GX, VS Code, WhatsApp...)"),
    ("windows.focus", '{"app": "nombre"}', "trae al frente la ventana abierta de esa aplicación"),
    ("windows.close", '{"app": "nombre"}', "cierra la ventana de esa aplicación; JARVIS pedirá aprobación"),
    ("windows.minimize_all", "{}", "minimiza todas las ventanas"),
    ("media.control", '{"action": "play_pause" | "next" | "previous" | "stop" | "mute"}',
     "controla la música o el video que suena (teclas multimedia)"),
    ("web.open", '{"site": "youtube" | "youtube music" | "google" | "gmail" | "github" | "whatsapp web" | '
                 '"netflix" | "twitch" | "chatgpt" | "claude" | "drive" | "maps" | "wikipedia" | "instagram" | '
                 '"x" | "reddit" | "savio"}', "abre un sitio web de la lista en el navegador (savio es el aula virtual de "
                 "la universidad)"),
    ("web.search", '{"engine": "google" | "youtube" | "wikipedia", "query": "texto"}',
     "abre una búsqueda en el navegador"),
    ("youtube.play", '{"query": "canción o video"}',
     "busca en YouTube y reproduce el primer resultado (úsalo para «pon/reproduce X» o «abre YouTube y pon X»)"),
    ("folders.open", '{"folder": "proyecto" | "documentacion"}',
     "abre la carpeta del proyecto JARVIS o la de documentación"),
    ("system.metrics", "{}", "consulta el uso de CPU y RAM"),
    ("system.time", "{}", "dice la hora y la fecha actuales"),
    ("assistant.dismiss", "{}",
     "el usuario da por terminada la conversación o se despide («eso sería todo», «ya no necesito nada», "
     "«me voy a dormir», «hasta mañana», «adiós»): JARVIS se despide y se oculta"),
    ("audio.volume", "{}", "consulta el volumen actual"),
    ("audio.set_volume", '{"percent": entero de 0 a 100}',
     "ajusta el volumen; JARVIS pedirá aprobación al usuario"),
    ("system.processes", "{}", "lista los procesos abiertos"),
    ("system.top", '{"by": "ram" | "cpu"}', "dice qué programas consumen más RAM o CPU ahora mismo"),
    ("system.gpu", "{}", "uso, VRAM y temperatura de la GPU NVIDIA"),
    ("reminders.add", '{"text": "qué recordar, o \\"\\" si no se dijo", "due": "AAAA-MM-DDTHH:MM:SS en hora local", '
                      '"kind": "recordatorio" | "alarma" | "temporizador"}',
     "programa UN aviso único; calcula «due» a partir de la fecha y hora local actuales. No hay avisos "
     "repetidos: si piden «todos los días» o «cada…», dilo en reply y no programes nada"),
    ("reminders.list", "{}", "lista los recordatorios, alarmas y temporizadores pendientes"),
    ("reminders.cancel", '{"reminder_id": entero, 0 si no dijo número, "kind": "recordatorio" | "alarma" | '
                         '"temporizador" | "aviso" (cualquiera)}', "cancela un aviso que aún no ha sonado"),
    ("system.power", '{"action": "shutdown" | "restart" | "sleep"}',
     "apaga, reinicia o suspende el equipo tras una cuenta atrás; JARVIS pedirá aprobación"),
    ("system.lock", "{}", "bloquea el equipo (pantalla de inicio de sesión)"),
    ("system.power_cancel", "{}", "cancela un apagado, reinicio o suspensión pendiente"),
    ("memory.list", "{}", "muestra lo que recuerdas del usuario"),
    ("memory.forget", '{"memory_id": entero}', "olvida un recuerdo por su número"),
    ("memory.clear", "{}", "olvida todos los recuerdos; JARVIS pedirá aprobación"),
)

BASE_PROMPT = """Eres JARVIS, la inteligencia artificial que asiste al usuario en su PC con Windows. \
Hablas en español.

Personalidad: inspirada en un mayordomo británico refinado convertido en IA. Formal, sereno, \
eficiente y discreto, con un ingenio seco y una ironía sutil que usas solo de vez en cuando. Leal, \
nunca servil ni empalagoso, sin exclamaciones excesivas ni emojis. Trata al usuario de «usted» y \
llámalo «señor», salvo que lo que recuerdas indique otra preferencia; si usas su nombre, úsalo tal \
como se presentó. Saluda según la hora («Buenos días», «Buenas tardes», «Buenas noches») cuando \
te saluden o empiece una conversación. Tus respuestas se leen en voz alta: sé breve (una o dos \
frases) salvo que te pidan detalle.

Cada mensaje del usuario es una petición. Responde SOLO con un objeto JSON, sin texto adicional \
ni bloques de código:
{"tool": "<id de herramienta o none>", "arguments": {...}, "reply": "<respuesta breve>", \
"remember": "<dato a recordar o null>"}

Herramientas disponibles:
{catalog}

Reglas:
- Usa una herramienta solo cuando la petición la pida. Si no, usa "tool": "none" y contesta en "reply".
- Si usas una herramienta, "reply" es solo un acuse breve en tu estilo («Enseguida, señor.», \
«Por supuesto, señor.») y NO afirma el resultado: JARVIS añadirá el resultado real.
- No inventes datos del sistema (CPU, RAM, volumen, procesos, ventanas). Usa la herramienta.
- Si piden algo que ninguna herramienta permite, dilo con honestidad y elegancia en "reply" y no finjas haberlo hecho.
- Solo puedes pedir UNA herramienta por mensaje. Si piden varias cosas, haz la principal y menciona la otra en "reply".
- "remember": si el usuario comparte un dato útil sobre sí mismo (nombre, cómo quiere que lo trates, \
preferencias, datos importantes de su vida o sus proyectos), escríbelo como una frase breve en tercera persona. \
Si no hay nada nuevo que recordar, usa null. Nunca guardes contraseñas, claves, PIN ni datos bancarios.
- Los mensajes que empiezan con [JARVIS] son notas del sistema con resultados reales, la hora o cambios \
de memoria; tenlos en cuenta como contexto.
"""


def local_time(now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{WEEKDAYS[now.weekday()]} {now.day} de {MONTHS[now.month - 1]} de {now.year}, {now:%H:%M}"


class BrainUnavailable(Exception):
    """El cerebro de IA no respondió o devolvió algo inválido."""


@dataclass(frozen=True)
class Decision:
    tool: str | None
    arguments: dict
    reply: str
    remember: str | None = None


def parse_decision(raw: str) -> Decision:
    """Extrae la decisión JSON del texto del modelo, tolerando bloques de código."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise BrainUnavailable("La IA no devolvió JSON.")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as error:
        raise BrainUnavailable("La IA devolvió JSON inválido.") from error
    if not isinstance(data, dict):
        raise BrainUnavailable("La IA devolvió un JSON inesperado.")

    tool = data.get("tool")
    tool = tool.strip() if isinstance(tool, str) else None
    if not tool or tool.lower() == "none":
        tool = None
    arguments = data.get("arguments")
    if arguments is None:
        arguments = {}
    reply = data.get("reply")
    remember = data.get("remember")
    remember = remember.strip() if isinstance(remember, str) and remember.strip() else None
    return Decision(
        tool,
        arguments if isinstance(arguments, dict) else {"_invalid": arguments},
        reply.strip() if isinstance(reply, str) else "",
        remember,
    )


def _version_key(path: Path) -> tuple[int, ...]:
    match = re.search(r"claude-code-(\d+(?:\.\d+)*)", str(path))
    return tuple(int(part) for part in match[1].split(".")) if match else ()


def find_claude_executable(home: Path | None = None) -> str | None:
    """Busca Claude Code: variable JARVIS_CLAUDE_PATH, PATH o la extensión de VS Code más reciente."""
    configured = os.environ.get("JARVIS_CLAUDE_PATH")
    if configured and Path(configured).is_file():
        return configured
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    extensions = (home or Path.home()) / ".vscode" / "extensions"
    candidates = list(extensions.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"))
    if not candidates:
        return None
    return str(max(candidates, key=_version_key))


@dataclass
class _Session:
    process: subprocess.Popen
    events: SimpleQueue = field(default_factory=SimpleQueue)
    reader: threading.Thread | None = None


class ClaudeBrain:
    """Mantiene un proceso de Claude Code bajo demanda y lo apaga tras un tiempo sin uso."""

    def __init__(
        self,
        command: list[str],
        memory: MemoryStore,
        workdir: Path,
        *,
        model: str = DEFAULT_MODEL,
        idle_seconds: float = IDLE_SECONDS,
        max_turns: int = MAX_TURNS,
        timeout: float = RESPONSE_TIMEOUT,
    ):
        self._command = list(command)
        self._memory = memory
        self._workdir = workdir
        self._model = model
        self._idle_seconds = idle_seconds
        self._max_turns = max_turns
        self._timeout = timeout
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._turns = 0
        self._last_used = monotonic()
        self._idle_timer: threading.Timer | None = None
        self._pending_notes: list[str] = []
        self._memory_changed = False
        self._time_sent_at = 0.0

    @property
    def running(self) -> bool:
        session = self._session
        return session is not None and session.process.poll() is None

    # --- ciclo de vida -------------------------------------------------------------------

    def _system_prompt(self) -> str:
        """Prompt con herramientas, recuerdos e historial, recortado para caber en la línea de comandos."""
        catalog = "\n".join(f"- {tool}: argumentos {args}. {text}." for tool, args, text in TOOL_CATALOG)
        parts = [BASE_PROMPT.replace("{catalog}", catalog), f"Fecha y hora local al empezar: {local_time()}."]
        facts = self._memory.facts()
        if facts:
            parts.append("Lo que recuerdas del usuario:")
            parts.extend(f"{fact.id}. {fact.text}" for fact in facts)
        else:
            parts.append("Todavía no recuerdas nada del usuario.")
        base = "\n".join(parts)
        history = self._memory.recent_exchanges(HISTORY_IN_PROMPT)
        while history:
            lines = ["\nConversación reciente (de sesiones anteriores):"]
            lines.extend(f"Usuario: {item.user_text}\nJARVIS: {item.reply}" for item in history)
            prompt = base + "\n" + "\n".join(lines)
            if len(prompt) <= MAX_PROMPT_CHARS:
                return prompt
            history = history[1:]
        return base[:MAX_PROMPT_CHARS]

    def _start(self) -> _Session:
        # El prompt va como argumento y no como archivo: el Python de Microsoft Store redirige
        # en silencio lo que escribe en AppData, y Claude Code no vería ese archivo.
        self._workdir.mkdir(parents=True, exist_ok=True)
        arguments = [
            *self._command, "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--model", self._model,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--setting-sources", "",
            "--system-prompt", self._system_prompt(),
        ]
        environment = {
            **os.environ,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
        }
        try:
            process = subprocess.Popen(
                arguments, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                bufsize=1, cwd=self._workdir, env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as error:
            raise BrainUnavailable("No se pudo iniciar Claude Code.") from error
        session = _Session(process)

        def read_output() -> None:
            for line in process.stdout:
                session.events.put(line)
            session.events.put(None)

        session.reader = threading.Thread(target=read_output, daemon=True)
        session.reader.start()
        self._session = session
        self._turns = 0
        self._memory_changed = False
        self._pending_notes.clear()
        self._time_sent_at = monotonic()
        return session

    def _ensure_session(self) -> _Session:
        if self.running and self._turns >= self._max_turns:
            self._stop()
        if not self.running:
            self._stop()
            return self._start()
        return self._session

    def _stop(self) -> None:
        session, self._session = self._session, None
        if session is None:
            return
        process = session.process
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if session.reader is not None:
            session.reader.join(timeout=2)
        if process.stdout and (session.reader is None or not session.reader.is_alive()):
            process.stdout.close()

    def _schedule_idle_close(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = threading.Timer(self._idle_seconds, self._close_if_idle)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def _close_if_idle(self) -> None:
        with self._lock:
            if monotonic() - self._last_used >= self._idle_seconds - 0.05:
                self._stop()

    def warm_up(self) -> None:
        """Arranca el proceso por adelantado para que la primera orden sea rápida."""
        with self._lock:
            self._ensure_session()
            self._last_used = monotonic()
            self._schedule_idle_close()

    def close(self) -> None:
        with self._lock:
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            self._stop()

    # --- conversación --------------------------------------------------------------------

    def _compose(self, text: str) -> str:
        notes = list(self._pending_notes)
        asks_time = TIME_WORDS.search(text) is not None
        if self.running and (asks_time or monotonic() - self._time_sent_at >= TIME_NOTE_SECONDS):
            notes.append(f"Hora local: {local_time()}.")
            self._time_sent_at = monotonic()
        if self._memory_changed:
            facts = self._memory.facts()
            listing = "; ".join(f"{fact.id}. {fact.text}" for fact in facts) or "nada"
            notes.append(f"La memoria cambió. Ahora recuerdas: {listing}")
        if not notes:
            return text
        header = "\n".join(f"[JARVIS] {note}" for note in notes)
        return f"{header}\n\nPetición del usuario: {text}"

    def _ask(self, session: _Session, content: str) -> str:
        message = {"type": "user", "message": {"role": "user", "content": content}}
        try:
            session.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            session.process.stdin.flush()
        except OSError as error:
            raise BrainUnavailable("Claude Code dejó de responder.") from error
        deadline = monotonic() + self._timeout
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise BrainUnavailable("Claude Code tardó demasiado en responder.")
            try:
                line = session.events.get(timeout=remaining)
            except Empty:
                continue
            if line is None:
                raise BrainUnavailable("Claude Code se cerró inesperadamente.")
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                if event.get("is_error") or event.get("subtype") != "success":
                    raise BrainUnavailable("Claude Code devolvió un error.")
                return str(event.get("result", ""))

    def decide(self, text: str) -> Decision:
        """Pregunta al modelo qué hacer con una frase libre. Reintenta una vez con un proceso nuevo."""
        with self._lock:
            content = self._compose(text)
            for attempt in (1, 2):
                try:
                    session = self._ensure_session()
                    if attempt == 2:
                        content = text
                    raw = self._ask(session, content)
                    break
                except BrainUnavailable:
                    self._stop()
                    if attempt == 2:
                        raise
            self._pending_notes.clear()
            self._memory_changed = False
            self._turns += 1
            self._last_used = monotonic()
            self._schedule_idle_close()
            return parse_decision(raw)

    def observe(self, user_text: str, outcome: str, *, source: str) -> None:
        """Guarda el intercambio en la memoria y, si el modelo no lo conoce, se lo comunica.

        ``source`` es "reply" (el modelo solo respondió), "tool" (el modelo propuso una
        herramienta y JARVIS la ejecutó) o "direct" (orden reconocida sin IA).
        """
        short = " ".join(outcome.split())
        if len(short) > NOTE_LIMIT:
            short = short[:NOTE_LIMIT] + "…"
        try:
            self._memory.add_exchange(user_text, short)
        except Exception:
            pass
        with self._lock:
            if source == "tool":
                self._pending_notes.append(f"Resultado real de tu última acción: {short}")
            elif source == "direct":
                self._pending_notes.append(
                    f"El usuario ejecutó directamente «{user_text}». Resultado: {short}"
                )
            del self._pending_notes[:-5]

    def notify_memory_changed(self) -> None:
        with self._lock:
            self._memory_changed = True
