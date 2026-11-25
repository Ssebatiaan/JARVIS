"""Calendarios por enlace iCal de solo lectura (SAVIO/Moodle, Outlook, Google).

El enlace lleva una clave personal: se guarda en el Administrador de credenciales de Windows y nunca
pasa por la IA, el historial ni los registros. Los títulos de los eventos son datos, no órdenes.
"""

import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from time import monotonic

from .tools import AgendaItem, CalendarError

TIMEOUT = 8.0
MAX_BYTES = 5 * 2**20
MAX_CALENDARS = 8
CACHE_SECONDS = 15 * 60
STALE_SECONDS = 24 * 3600
MAX_TITLE = 120
MAX_EVENTS = 300
USER_AGENT = "JARVIS-calendario/1.0"


class _OnlyHttps(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise CalendarError("el enlace redirige a una dirección no segura")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def clean_url(text: str) -> str:
    url = text.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname or any(char.isspace() for char in url):
        raise CalendarError("el enlace debe empezar por https:// o webcal://")
    return url


def fetch(url: str) -> bytes:
    """Descarga el calendario. Los errores nunca incluyen el enlace, que lleva una clave."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/calendar, */*"})
    try:
        with urllib.request.build_opener(_OnlyHttps()).open(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        raise CalendarError(f"el servidor rechazó el enlace (código {error.code})") from None
    except CalendarError:
        raise
    except (OSError, ValueError):
        raise CalendarError("no hubo conexión con el servidor") from None
    if len(raw) > MAX_BYTES:
        raise CalendarError("el calendario es demasiado grande")
    return raw


def read_events(raw: bytes, source: str, start: datetime, end: datetime) -> list[AgendaItem]:
    """Eventos entre dos horas locales, con las repeticiones ya desplegadas."""
    if b"BEGIN:VCALENDAR" not in raw[:2000].upper():
        raise CalendarError("el enlace no entrega un calendario (quizá pide iniciar sesión)")
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        raise CalendarError("falta instalar la librería de calendarios") from None
    try:
        calendar = icalendar.Calendar.from_ical(raw)
        # Moodle publica entregas y cierres; los demás calendarios, reuniones y clases.
        kind = "tarea" if "moodle" in str(calendar.get("PRODID", "")).lower() else "evento"
        found = recurring_ical_events.of(calendar).between(start.astimezone(), end.astimezone())
        today = start.replace(hour=0, minute=0, second=0, microsecond=0)
        items = []
        for event in found[:MAX_EVENTS]:
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            moment = event["DTSTART"].dt
            all_day = not isinstance(moment, datetime)
            if all_day:
                when = max(datetime.combine(moment, datetime.min.time()), today)
            else:
                when = moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
            if not all_day and not start <= when < end:
                continue
            title = " ".join(str(event.get("SUMMARY", "") or "Sin título").split())
            items.append(AgendaItem(when, all_day, title[:MAX_TITLE], kind, source))
        return items
    except Exception:
        raise CalendarError("no pude leer el contenido del calendario") from None


class Calendars:
    def __init__(self, vault, *, download: Callable[[str], bytes] = fetch, clock: Callable[[], float] = monotonic):
        self._vault = vault
        self._download = download
        self._clock = clock
        self._cache: dict[str, tuple[float, datetime, bytes]] = {}
        self._lock = threading.Lock()

    def names(self) -> list[str]:
        return self._vault.names()

    def add(self, name: str, link: str) -> int:
        """Comprueba el enlace antes de guardarlo. Devuelve los eventos de los próximos 60 días."""
        name = " ".join(name.split())
        if not 0 < len(name) <= 40 or not all(char.isalnum() or char in " -_" for char in name):
            raise CalendarError("póngale un nombre corto, sin símbolos")
        if name not in self.names() and len(self.names()) >= MAX_CALENDARS:
            raise CalendarError(f"ya hay {MAX_CALENDARS} calendarios conectados")
        url = clean_url(link)
        raw = self._download(url)
        now = datetime.now()
        count = len(read_events(raw, name, now, now + timedelta(days=60)))
        try:
            self._vault.set(name, url)
        except (OSError, ValueError):
            raise CalendarError("no pude guardar el enlace en Windows") from None
        with self._lock:
            self._cache[name] = (self._clock(), now, raw)
        return count

    def remove(self, name: str) -> bool:
        with self._lock:
            self._cache.pop(name, None)
        return self._vault.delete(name)

    def _events(self, name: str, start: datetime, end: datetime) -> tuple[list[AgendaItem], str | None]:
        with self._lock:
            cached = self._cache.get(name)
        warning = None
        try:
            if cached is None or self._clock() - cached[0] > CACHE_SECONDS:
                url = self._vault.get(name)
                if url is None:
                    return [], f"No pude consultar {name}: el enlace ya no está guardado."
                raw = self._download(clean_url(url))
                read_events(raw, name, start, end)  # una página de error no sustituye lo último bueno
                cached = (self._clock(), datetime.now(), raw)
                with self._lock:
                    self._cache[name] = cached
        except (CalendarError, OSError) as error:
            reason = str(error) if isinstance(error, CalendarError) else "Windows no entregó el enlace"
            if cached is None or self._clock() - cached[0] > STALE_SECONDS:
                return [], f"No pude consultar {name}: {reason}. Puede faltar algo."
            warning = f"No pude actualizar {name}: {reason}. Uso lo que vi a las {cached[1]:%H:%M}."
        try:
            return read_events(cached[2], name, start, end), warning
        except CalendarError as error:
            return [], f"No pude consultar {name}: {error}. Puede faltar algo."

    def events(self, start: datetime, end: datetime) -> tuple[list[AgendaItem], list[str]]:
        names = self.names()
        if not names:
            return [], []
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            results = list(pool.map(lambda name: self._events(name, start, end), names))
        return [item for items, _ in results for item in items], [warning for _, warning in results if warning]
