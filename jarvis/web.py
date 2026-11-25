"""Sitios web permitidos, búsquedas y reproducción en YouTube.

JARVIS solo abre sitios de una lista fija o búsquedas en buscadores conocidos; nunca una URL
arbitraria propuesta por la IA. Para reproducir en YouTube lee la página pública de resultados
(no es una API oficial: si YouTube la cambia, se abren los resultados para elegir a mano).
"""

import json
import re
import urllib.parse
import urllib.request

from .text import normalize

SITES = {
    "youtube": ("YouTube", "https://www.youtube.com/"),
    "youtube music": ("YouTube Music", "https://music.youtube.com/"),
    "google": ("Google", "https://www.google.com/"),
    "gmail": ("Gmail", "https://mail.google.com/"),
    "github": ("GitHub", "https://github.com/"),
    "whatsapp web": ("WhatsApp Web", "https://web.whatsapp.com/"),
    "netflix": ("Netflix", "https://www.netflix.com/"),
    "twitch": ("Twitch", "https://www.twitch.tv/"),
    "chatgpt": ("ChatGPT", "https://chatgpt.com/"),
    "claude": ("Claude", "https://claude.ai/"),
    "drive": ("Google Drive", "https://drive.google.com/"),
    "google drive": ("Google Drive", "https://drive.google.com/"),
    "maps": ("Google Maps", "https://www.google.com/maps"),
    "google maps": ("Google Maps", "https://www.google.com/maps"),
    "wikipedia": ("Wikipedia", "https://es.wikipedia.org/"),
    "instagram": ("Instagram", "https://www.instagram.com/"),
    "x": ("X", "https://x.com/"),
    "twitter": ("X", "https://x.com/"),
    "reddit": ("Reddit", "https://www.reddit.com/"),
    "savio": ("SAVIO", "https://savio.utb.edu.co/my/courses.php"),
}
SEARCH_ENGINES = {
    "google": ("Google", "https://www.google.com/search?q={}"),
    "youtube": ("YouTube", "https://www.youtube.com/results?search_query={}"),
    "wikipedia": ("Wikipedia", "https://es.wikipedia.org/w/index.php?search={}"),
}
MAX_QUERY = 200
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
_INITIAL_DATA = re.compile(r"var ytInitialData = (\{.*?\});</script>", re.DOTALL)


def site_key(name: str) -> str | None:
    key = normalize(name).strip(" .¿?¡!").removeprefix("la pagina de ").removeprefix("el ")
    return key if key in SITES else None


def search_url(engine: str, query: str) -> str:
    return SEARCH_ENGINES[engine][1].format(urllib.parse.quote_plus(query))


def _first_video(data) -> tuple[str, str] | None:
    """Recorre ytInitialData y devuelve el primer (videoId, título) de los resultados."""
    stack = [data]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            renderer = node.get("videoRenderer")
            if isinstance(renderer, dict) and isinstance(renderer.get("videoId"), str):
                runs = renderer.get("title", {}).get("runs", [])
                title = "".join(run.get("text", "") for run in runs if isinstance(run, dict))
                return renderer["videoId"], title or "el video"
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def find_youtube_video(query: str, *, fetch=None, timeout: float = 6.0) -> tuple[str, str] | None:
    """Devuelve (url del video, título) del primer resultado, o None si no se pudo leer."""
    url = search_url("youtube", query)
    try:
        if fetch is not None:
            html = fetch(url)
        else:
            request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Language": "es"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                html = response.read().decode("utf-8", errors="replace")
        match = _INITIAL_DATA.search(html)
        found = _first_video(json.loads(match[1])) if match else None
        if found is None:
            ids = re.findall(r'"videoId":"([\w-]{11})"', html)
            found = (ids[0], "el primer resultado") if ids else None
    except (OSError, ValueError):
        return None
    if found is None or not re.fullmatch(r"[\w-]{11}", found[0]):
        return None
    return f"https://www.youtube.com/watch?v={found[0]}", found[1]
