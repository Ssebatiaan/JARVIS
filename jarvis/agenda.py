"""Agenda: entiende «¿tengo algún pendiente esta semana?» y redacta la respuesta para leerla en voz alta."""

import re
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher

from .text import normalize
from .tools import AgendaItem, Task

WEEKDAYS = ("lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo")
WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")
SPOKEN_ITEMS = 4  # con más, se dicen los primeros y el resto queda en pantalla
DETAIL = "\nDetalle:\n"

# --- preguntas -----------------------------------------------------------------------------

_PERIODS = (
    (r"\b(?:proxima semana|semana que viene|otra semana|semana proxima)\b", "proxima_semana"),
    (r"\bsemana\b", "semana"), (r"\bmes\b", "mes"), (r"\bhoy\b", "hoy"), (r"\bmanana\b", "manana"),
    *((rf"\b{day}\b", day) for day in WEEKDAYS),
)
_TASK_NOUNS = {"tarea", "tareas", "entrega", "entregas", "taller", "talleres", "trabajo", "trabajos", "examen",
               "examenes", "parcial", "parciales", "quiz", "quices", "actividad", "actividades"}
_EVENT_NOUNS = {"reunion", "reuniones", "clase", "clases", "evento", "eventos", "cita", "citas"}
_NOUNS = {"pendiente", "pendientes", "agenda", "compromiso", "compromisos", "calendario"} | _TASK_NOUNS | _EVENT_NOUNS
_FILL = set(
    "que cual cuales cuantas cuantos tengo tenemos hay son es mi mis el la los las lo de del en para por hacer entregar "
    "algo alguna algun algunas algunos un una unos unas y o u e ni otra otro me dime di dices muestrame revisa consulta "
    "mira lee leeme si proxima proximo proximas proximos este esta estas que viene semana mes hoy manana dia tarde noche "
    "general todo todas todos programada programadas programado programados universidad savio outlook por favor a".split()
) | set(WEEKDAYS)
_STARTS = {"que", "cual", "cuales", "cuantas", "cuantos", "hay", "mi", "mis", "agenda", "dime", "muestrame", "revisa",
           "consulta", "mira", "lee", "leeme"}


def question(command: str, asked: bool = False) -> dict | None:
    """{"period", "kind"} si la orden normalizada pregunta por la agenda. ``asked``: traía «?»."""
    words = re.sub(r"[,.;:!¡¿?]", " ", command).split()
    if not words or not all(word in _FILL or word in _NOUNS for word in words):
        return None
    nouns = _NOUNS.intersection(words)
    period = next((name for pattern, name in _PERIODS if re.search(pattern, command)), None)
    if not nouns and not ({"que", "algo"}.intersection(words) and {"tengo", "tenemos"}.intersection(words) and period):
        return None
    # «tengo reunión mañana» sin pregunta puede ser un dato: lo decide la IA.
    asks = words[0] in _STARTS or asked or (words[0] in {"tengo", "tenemos"} and len(words) > 1
                                            and words[1] in {"algo", "algun", "alguna", "algunos", "algunas"})
    if not asks or (words[0] in {"que", "cual"} and len(words) > 1 and words[1] == "es"):
        return None
    tasks, events = bool(_TASK_NOUNS & nouns), bool(_EVENT_NOUNS & nouns)
    general = bool(nouns - _TASK_NOUNS - _EVENT_NOUNS - {"pendientes"}) or tasks == events
    return {"period": period or "semana", "kind": "todo" if general else "tareas" if tasks else "eventos"}


def period_range(period: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(desde, hasta, «para …»). Los periodos en curso empiezan ahora mismo."""
    today = datetime.combine(now.date(), time())
    monday = today + timedelta(days=7 - today.weekday())
    if period == "hoy":
        return now, today + timedelta(days=1), "para hoy"
    if period == "manana":
        return today + timedelta(days=1), today + timedelta(days=2), "para mañana"
    if period == "semana":
        return now, monday, "para esta semana"
    if period == "proxima_semana":
        return monday, monday + timedelta(days=7), "para la próxima semana"
    if period == "mes":
        return now, (today.replace(day=1) + timedelta(days=32)).replace(day=1), "para este mes"
    ahead = (WEEKDAYS.index(period) - today.weekday()) % 7
    start = today + timedelta(days=ahead)
    return max(start, now), start + timedelta(days=1), f"para el {WEEKDAY_NAMES[start.weekday()]} {start.day}"


# --- respuesta -----------------------------------------------------------------------------

def day_name(moment: datetime, now: datetime) -> str:
    days = (moment.date() - now.date()).days
    if days == 0:
        return "hoy"
    if days == 1:
        return "mañana"
    if 1 < days < 7:
        return f"el {WEEKDAY_NAMES[moment.weekday()]} {moment.day}"
    return f"el {moment.day} de {MONTHS[moment.month - 1]}"


def describe_when(when: datetime, all_day: bool, now: datetime) -> str:
    return day_name(when, now) + ("" if all_day else f" a las {when:%H:%M}")


def describe_item(item: AgendaItem, now: datetime) -> str:
    source = f" ({item.source})" if item.source else ""
    if item.when is None:
        return f"sin fecha, «{item.title}»{source}"
    late = item.when.date() < now.date() if item.all_day else item.when < now
    when = describe_when(item.when, item.all_day, now)
    if item.kind == "tarea" and late:
        if item.when.date() == now.date():
            return f"atrasada, «{item.title}»{source}, era para {when}"
        return f"atrasada, «{item.title}»{source}, era para el {item.when.day} de {MONTHS[item.when.month - 1]}"
    return f"{when}, «{item.title}»{source}"


def select(items: list[AgendaItem], kind: str) -> list[AgendaItem]:
    """Filtra, quita lo repetido en dos calendarios y ordena: atrasado, con fecha y sin fecha."""
    wanted = {"tareas": {"tarea"}, "eventos": {"evento"}}.get(kind, {"tarea", "evento", "aviso"})
    unique: dict[tuple, AgendaItem] = {}
    for item in items:
        if item.kind in wanted:
            unique.setdefault((normalize(item.title), item.when), item)
    return sorted(unique.values(), key=lambda item: (item.when is None, item.when or datetime.max, item.title))


def describe(items: list[AgendaItem], label: str, kind: str, now: datetime, *, warnings: list[str] = (),
             calendars: int = 0) -> str:
    noun = {"tareas": ("tarea", "tareas"), "eventos": ("evento", "eventos")}.get(kind, ("pendiente", "pendientes"))
    selected = select(items, kind)
    notes = " ".join(warnings)
    if not selected:
        if warnings:
            message = f"No encontré {noun[1]} {label}, pero la consulta quedó incompleta. {notes}"
        else:
            message = f"No tiene {noun[1]} {label}."
            if calendars == 0:
                message += " Solo revisé lo anotado aquí: aún no hay calendarios conectados."
        return message
    count = f"Tiene {len(selected)} {noun[0] if len(selected) == 1 else noun[1]} {label}"
    tail = f" {notes}" if notes else ""
    if len(selected) <= SPOKEN_ITEMS:
        return f"{count}: " + "; ".join(describe_item(item, now) for item in selected) + f".{tail}"
    first = "; ".join(describe_item(item, now) for item in selected[:3])
    rows = "\n".join(f"- {describe_item(item, now)}" for item in selected)
    return f"{count}. Lo primero: {first}. El resto está en pantalla.{tail}{DETAIL}{rows}"


# --- tareas propias ------------------------------------------------------------------------

def find_tasks(tasks: list[Task], wanted: str, *, fuzzy: bool = True) -> list[Task]:
    """Tareas que coinciden con un número, con parte del texto o (``fuzzy``) parecidas."""
    wanted = normalize(wanted).strip(" .«»\"")
    if wanted.isdigit():
        return [task for task in tasks if task.id == int(wanted)]
    wanted = re.sub(r"^(?:la tarea de |la tarea |el evento |la reunion (?:con |de )?|lo de |de |el |la )", "", wanted)
    exact = [task for task in tasks if normalize(task.text) == wanted]
    if exact:
        return exact
    inside = [task for task in tasks if len(wanted) >= 3 and wanted in normalize(task.text)]
    if inside or not fuzzy:
        return inside
    return [task for task in tasks if SequenceMatcher(None, wanted, normalize(task.text)).ratio() >= 0.75]


def describe_task(task: Task, now: datetime) -> str:
    when = f", {describe_when(task.due, not task.has_time, now)}" if task.due else ""
    return f"«{task.text}»{when}"
