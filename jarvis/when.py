"""Temporizadores, alarmas y recordatorios en español: «en 20 minutos», «mañana a las 7 y media».

Trabaja sobre texto normalizado (minúsculas, sin tildes). Lo que no entienda lo resuelve la IA.
"""

import re
from datetime import datetime, timedelta

NUMBERS = {
    "un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veinticinco": 25,
    "treinta": 30, "cuarenta": 40, "cuarenta y cinco": 45, "cincuenta": 50, "sesenta": 60, "noventa": 90,
}
WEEKDAYS = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
WEEKDAY_NAMES = ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")
_NUMBER = r"\d+|" + "|".join(sorted((re.escape(word) for word in NUMBERS), key=len, reverse=True))
_UNIT = r"segundos?|segs?|minutos?|mins?|horas?|hrs?"
_DURATION = rf"(?:media hora|un cuarto de hora|(?:{_NUMBER}) (?:{_UNIT})(?: y media| y cuarto)?)"
DURATION_EXPR = re.compile(rf"(?:{_DURATION})(?:,? y (?:{_DURATION}))*")
RELATIVE = re.compile(rf"\b(?:en|dentro de) (?P<duration>{DURATION_EXPR.pattern})")
CLOCK = re.compile(
    r"\b(?:a las?|para las?) (?P<hour>\d{1,2}|" + "|".join(w for w in NUMBERS if NUMBERS[w] <= 12) + r")\b"
    r"(?::(?P<minute>\d{2})| y (?P<fraction>media|cuarto)| menos cuarto(?P<minus>)| y (?P<minutes>\d{1,2}|"
    + "|".join(sorted((w for w in NUMBERS if 1 < NUMBERS[w] < 60), key=len, reverse=True)) + r")\b)?"
    r"(?: (?:de la |en la |por la )?(?P<period>manana|tarde|noche|madrugada)| (?P<ampm>am|pm|a ?m|p ?m))?"
)
NOON = re.compile(r"\b(?:al|a) (?P<word>mediodia|medianoche)\b")
DAY = re.compile(r"\b(?P<day>pasado manana|(?<!la )manana|hoy|esta noche|esta tarde|el (?:" + "|".join(WEEKDAYS) + r"))\b")
TRIGGER = re.compile(
    r"^(?:por favor )?(?:avisame|recuerdame|recordarme|despiertame|alertame|"
    r"pon(?:me)? (?:una alarma|un recordatorio|un aviso)|crea (?:una alarma|un recordatorio)|"
    r"programa (?:una alarma|un recordatorio))\b"
)
TIMER = re.compile(r"^(?:(?:pon|ponme|crea|programa|inicia|activa)(?: un| el)? )?temporizador(?: de| por| para)? (?P<rest>.+)$")
# Fechas y repeticiones que este intérprete no sabe calcular: mejor la IA que una fecha equivocada.
UNSUPPORTED = re.compile(
    r"\b(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|sep?tiembre|octubre|noviembre|diciembre|"
    r"proxim[oa]s?|que viene|siguiente|cada|todos los|todas las|diario|diariamente|dias|semanas?|mes|meses|"
    r"fin de semana|el (?:dia )?\d{1,2}|(?:" + "|".join(WEEKDAYS) + r") \d{1,2})\b"
)
ALARM = re.compile(r"\balarma\b|^despiertame")


def _number(word: str) -> int:
    return int(word) if word.isdigit() else NUMBERS.get(word, 0)


def parse_duration(text: str) -> timedelta | None:
    total = timedelta()
    found = False
    for part in re.finditer(_DURATION, text):
        piece = part.group(0)
        found = True
        if piece == "media hora":
            total += timedelta(minutes=30)
            continue
        if piece == "un cuarto de hora":
            total += timedelta(minutes=15)
            continue
        match = re.match(rf"(?P<n>{_NUMBER}) (?P<unit>{_UNIT})(?P<extra> y media| y cuarto)?", piece)
        amount, unit = _number(match["n"]), match["unit"]
        if unit.startswith("h"):
            total += timedelta(hours=amount)
            if match["extra"]:
                total += timedelta(minutes=30 if "media" in match["extra"] else 15)
        elif unit.startswith("m"):
            total += timedelta(minutes=amount)
        else:
            total += timedelta(seconds=amount)
    return total if found and total > timedelta() else None


PERIOD = re.compile(r"\b(?:por|en|de) la (?P<period>manana|tarde|noche|madrugada)\b")


def _clock_time(match, day: datetime, explicit_day: bool, now: datetime, loose_period: str | None = None) -> datetime:
    hour = _number(match["hour"])
    minute = 0
    if match["minute"]:
        minute = int(match["minute"])
    elif match["minutes"]:
        minute = _number(match["minutes"])
    elif match["fraction"]:
        minute = 30 if match["fraction"] == "media" else 15
    elif match["minus"] is not None:
        hour, minute = hour - 1, 45
    period = match["period"] or loose_period
    ampm = (match["ampm"] or "").replace(" ", "")
    if period == "noche" and hour == 12:
        day, hour = day + timedelta(days=1), 0  # «las 12 de la noche» es la medianoche que viene
    elif period in ("tarde", "noche") or ampm == "pm":
        if hour < 12:
            hour += 12
    elif (period in ("manana", "madrugada") or ampm == "am") and hour == 12:
        hour = 0
    elif not period and not ampm and hour <= 12:
        if explicit_day:
            hour = hour + 12 if 1 <= hour <= 6 else hour  # «mañana a las 3» suele ser por la tarde
        else:
            # Hoy sin «de la mañana/tarde»: la próxima vez que llegue esa hora.
            for candidate in (hour % 12, hour % 12 + 12):
                moment = day.replace(hour=candidate, minute=minute % 60, second=0, microsecond=0)
                if moment > now:
                    return moment
            return (day + timedelta(days=1)).replace(hour=hour % 12, minute=minute % 60, second=0, microsecond=0)
    hour %= 24
    return day.replace(hour=hour, minute=minute % 60, second=0, microsecond=0)


def _resolve_day(match, now: datetime) -> tuple[datetime, bool]:
    if match is None:
        return now, False
    word = match["day"]
    if word in ("hoy", "esta noche", "esta tarde"):
        return now, False
    if word == "manana":
        return now + timedelta(days=1), True
    if word == "pasado manana":
        return now + timedelta(days=2), True
    weekday = WEEKDAYS[word.split()[1]]
    ahead = (weekday - now.weekday()) % 7 or 7
    return now + timedelta(days=ahead), True


def _task(text: str, *removed: str) -> str:
    for piece in removed:
        if piece:
            text = text.replace(piece, " ", 1)
    text = re.sub(r"\s+", " ", text).strip(" ,.")
    while True:
        cleaned = re.sub(r"^(?:que|de|para|a|el|la|lo|por favor|y)\s+", "", text)
        cleaned = re.sub(r"\s+(?:por favor|porfa)$", "", cleaned)
        if cleaned == text:
            return text
        text = cleaned


def parse_reminder(text: str, now: datetime | None = None):
    """(momento, qué recordar, tipo) o None. ``text`` debe venir normalizado."""
    now = now or datetime.now()
    text = text.strip(" .¿?¡!")
    timer = TIMER.match(text)
    if timer:
        duration = parse_duration(timer["rest"])
        return (now + duration, "", "temporizador") if duration else None
    trigger = TRIGGER.match(text)
    if not trigger:
        return None
    kind = "alarma" if ALARM.search(trigger.group(0)) else "recordatorio"
    if UNSUPPORTED.search(text):
        return None
    relative = RELATIVE.search(text)
    if relative:
        duration = parse_duration(relative["duration"])
        if duration:
            return now + duration, _task(text, trigger.group(0), relative.group(0)), kind
    clock = CLOCK.search(text)
    noon = NOON.search(text)
    rest = text
    for found in (clock, noon):
        if found:
            rest = rest.replace(found.group(0), " ", 1)
    day_match = DAY.search(rest)
    day, explicit = _resolve_day(day_match, now)
    loose = PERIOD.search(rest) if clock and not clock["period"] else None
    if clock:
        if _number(clock["hour"]) > 23 or int(clock["minute"] or 0) > 59 or _number(clock["minutes"] or "0") > 59:
            return None
        period = loose["period"] if loose else None
        if day_match and day_match["day"] in ("esta noche", "esta tarde"):
            period = period or day_match["day"].split()[1]
        moment = _clock_time(clock, day, explicit, now, period)
        removed = clock.group(0)
    elif noon:
        moment = day.replace(hour=12 if noon["word"] == "mediodia" else 0, minute=0, second=0, microsecond=0)
        if noon["word"] == "medianoche" and not explicit:
            moment += timedelta(days=1)
        removed = noon.group(0)
    elif explicit:
        moment = day.replace(hour=9, minute=0, second=0, microsecond=0)  # solo el día: a las 9
        removed = ""
    else:
        return None
    if moment <= now:
        moment += timedelta(days=1)
    return moment, _task(text, trigger.group(0), removed, day_match.group(0) if day_match else "",
                         loose.group(0) if loose else ""), kind


def describe_due(moment: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    clock = f"{moment:%H:%M}"
    delta = moment - now
    if delta < timedelta(hours=1):
        minutes = max(1, round(delta.total_seconds() / 60))
        if delta < timedelta(minutes=1):
            return f"en {max(1, round(delta.total_seconds()))} segundos"
        return f"en {minutes} minuto{'s' if minutes != 1 else ''} (a las {clock})"
    if moment.date() == now.date():
        return f"hoy a las {clock}"
    if moment.date() == (now + timedelta(days=1)).date():
        return f"mañana a las {clock}"
    return f"el {WEEKDAY_NAMES[moment.weekday()]} {moment.day} a las {clock}"
