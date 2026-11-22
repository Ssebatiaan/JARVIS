"""Catálogo de aplicaciones instaladas según el menú Inicio de Windows.

Solo se pueden abrir aplicaciones que aparecen en el menú Inicio: es la lista de lo que el
usuario tiene instalado. Se abren con ``shell:AppsFolder``, el mismo mecanismo que usa Windows
para apps de escritorio y de la Microsoft Store. Nunca se ejecutan rutas ni argumentos.
"""

import json
import re
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from time import monotonic

from .text import normalize
from .tools import TargetAmbiguous, TargetNotFound

REFRESH_SECONDS = 3600
EXCLUDED_NAME = re.compile(r"uninstall|desinstal|support center|readme|léame|leame|help|ayuda de", re.I)
EXCLUDED_ID = re.compile(r"^https?://|uninstall|unins\d*\.exe", re.I)
# Nombres coloquiales frecuentes.
ALIASES = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "code": "visual studio code",
    "notepad": "bloc de notas",
    "el bloc de notas": "bloc de notas",
    "explorador": "explorador de archivos",
    "calculadora": "calculadora",
    "edge": "microsoft edge",
    "word": "word",
    "excel": "excel",
}
_LIST_APPS = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"
)


@dataclass(frozen=True)
class InstalledApp:
    name: str
    app_id: str


AppNotFound = TargetNotFound
AppAmbiguous = TargetAmbiguous


def _read_start_apps() -> list[InstalledApp]:
    output = subprocess.run(
        ["powershell", "-NoProfile", "-Command", _LIST_APPS], capture_output=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("utf-8", errors="replace").strip()
    rows = json.loads(output) if output else []
    if isinstance(rows, dict):
        rows = [rows]
    return [InstalledApp(str(row["Name"]), str(row["AppID"])) for row in rows
            if row.get("Name") and row.get("AppID")]


def _clean(name: str) -> str:
    text = normalize(name)
    for prefix in ("la aplicacion ", "la app ", "el programa ", "el ", "la ", "mi "):
        text = text.removeprefix(prefix)
    return text.strip(" .¿?¡!")


class AppCatalog:
    def __init__(self, loader: Callable[[], list[InstalledApp]] = _read_start_apps):
        self._loader = loader
        self._apps: list[InstalledApp] | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def preload(self) -> None:
        threading.Thread(target=self._safe_refresh, name="jarvis-apps", daemon=True).start()

    def _safe_refresh(self) -> None:
        try:
            self.apps(refresh=True)
        except Exception:
            pass

    def apps(self, *, refresh: bool = False) -> list[InstalledApp]:
        with self._lock:
            stale = monotonic() - self._loaded_at > REFRESH_SECONDS
            if self._apps is None or refresh or stale:
                apps = [app for app in self._loader()
                        if not EXCLUDED_NAME.search(app.name) and not EXCLUDED_ID.search(app.app_id)]
                self._apps = sorted(apps, key=lambda app: app.name.casefold())
                self._loaded_at = monotonic()
            return list(self._apps)

    @staticmethod
    def _score(query: str, name: str) -> float:
        target = normalize(name)
        if target == query:
            return 1.0
        words = target.split()
        if target.startswith(query + " ") or query in words:
            return 0.92
        if all(word in words for word in query.split()):
            return 0.88
        if query in target:
            return 0.8
        # Tolera errores de tipeo o de transcripción («discorf», «spotifai»).
        return SequenceMatcher(None, query, target).ratio() * 0.9

    def find(self, query: str) -> InstalledApp:
        wanted = _clean(query)
        wanted = ALIASES.get(wanted, wanted)
        if not wanted:
            raise AppNotFound(query, [])
        for attempt in (False, True):
            apps = self.apps(refresh=attempt)
            scored = sorted(((self._score(wanted, app.name), app) for app in apps),
                            key=lambda item: (-item[0], len(item[1].name)))
            if scored and scored[0][0] >= 0.75:
                best_score, best = scored[0]
                rivals = [app.name for score, app in scored[1:4] if best_score - score < 0.02 and best_score < 1.0]
                if rivals:
                    raise AppAmbiguous(query, [best.name, *rivals])
                return best
        suggestions = [app.name for score, app in scored[:3] if score >= 0.45] if scored else []
        raise AppNotFound(query, suggestions)


def launch(app: InstalledApp) -> None:
    """Pide a Windows que abra la app por su identificador del menú Inicio."""
    subprocess.Popen(
        ["explorer.exe", f"shell:AppsFolder\\{app.app_id}"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
