"""Vigila los avisos pendientes y redacta lo que JARVIS dice cuando vencen.

Consulta SQLite solo cuando llega la hora del próximo aviso (o una vez por minuto, por si otra
instancia añadió uno), así que comprobarlo cada segundo no cuesta nada.
"""

from datetime import datetime, timedelta
from time import monotonic

from .tools import Reminder

RECHECK_SECONDS = 60
LATE_AFTER = timedelta(minutes=5)


class ReminderClock:
    def __init__(self, store, recheck: float = RECHECK_SECONDS):
        self._store = store
        self._recheck = recheck
        self._next: datetime | None = None
        self._checked = float("-inf")

    def refresh(self) -> None:
        self._next = self._store.next_due()
        self._checked = monotonic()

    def due(self, now: datetime | None = None) -> list[Reminder]:
        if monotonic() - self._checked >= self._recheck:
            self.refresh()
        now = now or datetime.now()
        if self._next is None or now < self._next:
            return []
        reminders = self._store.take_due(now)
        self.refresh()
        return reminders


def describe_duration(span: timedelta) -> str:
    seconds = max(1, round(span.total_seconds()))
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours == 0 and minutes == 0:
        return f"{seconds} segundo{'s' if seconds != 1 else ''}"
    if seconds >= 30 and hours == 0:
        minutes += 1
    parts = []
    if hours:
        parts.append(f"{hours} hora{'s' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} minuto{'s' if minutes != 1 else ''}")
    return " y ".join(parts)


def announcement(reminder: Reminder, now: datetime | None = None) -> str:
    now = now or datetime.now()
    text = reminder.text.rstrip(" .")
    if now - reminder.due > LATE_AFTER:
        what = f": {text}" if text else ""
        return f"Señor, quedó pendiente un {reminder.kind} de las {reminder.due:%H:%M}{what}."
    if reminder.kind == "temporizador":
        if reminder.created is not None:
            return f"Señor, su temporizador de {describe_duration(reminder.due - reminder.created)} ha terminado."
        return "Señor, su temporizador ha terminado."
    if reminder.kind == "alarma":
        article = "Es la" if reminder.due.hour % 12 == 1 else "Son las"
        return f"Señor, {article.lower()} {reminder.due:%H:%M}. " + (f"{text[0].upper()}{text[1:]}." if text else
                                                                     "Su alarma, tal como pidió.")
    if text:
        return f"Señor, le recuerdo: {text}."
    return "Señor, me pidió que le avisara a esta hora."
