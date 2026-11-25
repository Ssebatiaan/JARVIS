"""Personalidad de JARVIS: un mayordomo refinado y sereno que trata al usuario de «señor».

Se aplica a las órdenes que no pasan por la IA. Las respuestas de la IA siguen el mismo estilo
desde su propio prompt (brain.py). El resultado real de cada herramienta nunca se altera: solo
se le antepone una frase breve.
"""

import random
from datetime import datetime

from .tools import ToolId

REPORTS = {ToolId.SYSTEM_METRICS, ToolId.READ_VOLUME, ToolId.LIST_PROCESSES, ToolId.LIST_MEMORIES,
           ToolId.TIME, ToolId.GPU, ToolId.TOP_PROGRAMS, ToolId.LIST_REMINDERS, ToolId.AGENDA,
           ToolId.DAY_SUMMARY, ToolId.LIST_CALENDARS, ToolId.WEATHER, ToolId.CALCULATE, ToolId.CPU_TEMPERATURE,
           ToolId.LIST_WISHES}
REPORT_OPENERS = ("Aquí tiene, señor.", "Con mucho gusto, señor.", "Enseguida, señor.")
ACTION_OPENERS = ("Enseguida, señor.", "Por supuesto, señor.", "Ahora mismo, señor.", "Como desee, señor.")
DENIED = "Un detalle, señor."
FAILED = "Un contratiempo, señor."
REJECTED = "Como usted diga, señor. No haré cambios."
NOT_UNDERSTOOD = "Disculpe, señor, no he comprendido la orden."


def greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if 5 <= hour < 12:
        return "Buenos días"
    if 12 <= hour < 19:
        return "Buenas tardes"
    return "Buenas noches"


def welcome(now: datetime | None = None) -> str:
    return f"{greeting(now)}, señor. Todos los sistemas en línea."


def dress(tool_id: ToolId | None, code: int, message: str, *, rng: random.Random | None = None) -> str:
    """Antepone una frase de JARVIS al resultado real de una orden directa."""
    choose = (rng or random).choice
    if tool_id is None:
        return f"{NOT_UNDERSTOOD} {message}"
    if tool_id is ToolId.DISMISS and code == 0:
        return message
    if code == 0:
        opener = choose(REPORT_OPENERS if tool_id in REPORTS else ACTION_OPENERS)
        return f"{opener} {message}"
    if message.startswith("¿"):  # falta un dato: se pregunta sin más
        return message
    if message.startswith("Acción rechazada"):
        return REJECTED
    if code == 2:
        return f"{DENIED} {message}"
    if code == 130:
        return message
    return f"{FAILED} {message}"
