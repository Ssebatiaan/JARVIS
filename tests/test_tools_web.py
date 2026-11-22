import json
import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from jarvis.apps import AppCatalog, InstalledApp
from jarvis.audit import Audit
from jarvis.brain import TOOL_CATALOG
from jarvis.core import _route, run_command
from jarvis.persona import dress, greeting, welcome
from jarvis.tools import TOOLS, LocalTools, TargetAmbiguous, TargetNotFound, ToolId, normalize
from jarvis.web import SITES, find_youtube_video, site_key


def route(text):
    return _route(normalize(text), text)


class RoutingTests(unittest.TestCase):
    def test_youtube_music_sites_and_searches(self):
        cases = {
            "Jarvis, abre YouTube y reproduce Back in Black": (ToolId.PLAY_YOUTUBE, {"query": "Back in Black"}),
            "reproduce la canción Canción  del Mariachi en youtube": (ToolId.PLAY_YOUTUBE, {"query": "Canción  del Mariachi"}),
            "abre youtube": (ToolId.OPEN_SITE, {"site": "youtube"}),
            "abre youtube y busca tutoriales de Python": (ToolId.WEB_SEARCH, {"engine": "youtube", "query": "tutoriales de Python"}),
            "busca recetas de arepas en Google": (ToolId.WEB_SEARCH, {"engine": "google", "query": "recetas de arepas"}),
            "pausa la música": (ToolId.MEDIA, {"action": "play_pause"}),
            "siguiente canción": (ToolId.MEDIA, {"action": "next"}),
            "canción anterior": (ToolId.MEDIA, {"action": "previous"}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text), expected)

    def test_apps_and_windows(self):
        cases = {
            "Jarvis abre Discord": (ToolId.OPEN_APP, {"app": "discord"}),
            "inicia steam": (ToolId.OPEN_APP, {"app": "steam"}),
            "cambia a Opera": (ToolId.FOCUS_WINDOW, {"app": "opera"}),
            "pon discord al frente": (ToolId.FOCUS_WINDOW, {"app": "discord"}),
            "cierra la ventana de Discord": (ToolId.CLOSE_WINDOW, {"app": "discord"}),
            "minimiza todo": (ToolId.MINIMIZE_ALL, {}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text), expected)

    def test_compound_or_conversational_phrases_go_to_the_ai(self):
        for text in ("abre spotify y pon rock", "hola jarvis", "estoy aburrido", "jarvis"):
            with self.subTest(text=text):
                self.assertEqual(route(text), (None, {}))


class ToolExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.audit = Audit(Path(temporary.name) / "auditoria.sqlite3")
        self.tools = Mock(spec=LocalTools)

    def test_youtube_plays_the_found_video_or_opens_the_results(self):
        self.tools.find_video.return_value = ("https://www.youtube.com/watch?v=pAgnJDJN4VA", "AC/DC - Back In Black")
        result = run_command("reproduce back in black en youtube", self.tools, self.audit)
        self.assertEqual(result.message, "Reproduciendo «AC/DC - Back In Black» en YouTube.")
        self.tools.open_url.assert_called_once_with("https://www.youtube.com/watch?v=pAgnJDJN4VA")
        self.tools.find_video.return_value = None
        result = run_command("reproduce algo raro en youtube", self.tools, self.audit)
        self.assertEqual(result.code, 0)
        self.assertEqual(self.tools.open_url.call_args.args[0],
                         "https://www.youtube.com/results?search_query=algo+raro")

    def test_sites_and_searches_only_open_allowed_https_addresses(self):
        run_command("abre github", self.tools, self.audit)
        self.tools.open_url.assert_called_with("https://github.com/")
        run_command("busca café colombiano en google", self.tools, self.audit)
        self.assertEqual(self.tools.open_url.call_args.args[0],
                         "https://www.google.com/search?q=caf%C3%A9+colombiano")
        for site in SITES.values():
            self.assertTrue(site[1].startswith("https://"))
        self.assertFalse(TOOLS[ToolId.OPEN_SITE].accepts_arguments({"site": "https://malicioso.example"}))

    def test_closing_a_window_requires_approval(self):
        self.tools.close_window.return_value = "Misiones - Discord"
        result = run_command("cierra discord", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.tools.close_window.assert_not_called()
        approvals = []
        result = run_command("cierra discord", self.tools, self.audit,
                             approve=lambda request: approvals.append(request.summary) or True)
        self.assertEqual(result.message, "Pedí cerrar «Misiones - Discord».")
        self.assertIn("«discord»", approvals[0])

    def test_media_focus_and_missing_windows(self):
        run_command("siguiente canción", self.tools, self.audit)
        self.tools.media_key.assert_called_once_with("next")
        self.tools.focus_window.side_effect = TargetNotFound("spotify", [])
        result = run_command("cambia a spotify", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.assertIn("No encontré «spotify» entre las ventanas abiertas", result.message)

    def test_every_tool_is_offered_to_the_ai_except_memory_saving(self):
        offered = {tool for tool, _, _ in TOOL_CATALOG}
        self.assertEqual(offered, {tool.value for tool in TOOLS} - {ToolId.SAVE_MEMORY.value})


class AppCatalogTests(unittest.TestCase):
    def catalog(self, *names):
        return AppCatalog(loader=lambda: [InstalledApp(name, f"id.{index}") for index, name in enumerate(names)])

    def test_matches_names_aliases_and_hides_uninstallers(self):
        catalog = self.catalog("Discord", "Visual Studio Code", "Navegador Opera GX", "Uninstall Zoom", "Steam",
                               "Steam Support Center")
        self.assertEqual(catalog.find("el discord").name, "Discord")
        self.assertEqual(catalog.find("vs code").name, "Visual Studio Code")
        self.assertEqual(catalog.find("Opera GX").name, "Navegador Opera GX")
        self.assertEqual(catalog.find("steam").name, "Steam")
        self.assertNotIn("Uninstall Zoom", [app.name for app in catalog.apps()])

    def test_unknown_and_ambiguous_names(self):
        catalog = self.catalog("Microsoft Word", "Microsoft Excel", "Discord")
        with self.assertRaises(TargetNotFound) as missing:
            catalog.find("firefox")
        self.assertEqual(missing.exception.query, "firefox")
        with self.assertRaises(TargetAmbiguous):
            catalog.find("microsoft")
        self.assertEqual(catalog.find("discorf").name, "Discord")


class YouTubeTests(unittest.TestCase):
    def page(self, data):
        return f"<script>var ytInitialData = {json.dumps(data)};</script>"

    def test_reads_the_first_video_and_its_title(self):
        data = {"contents": {"list": [{"adRenderer": {}}, {"videoRenderer": {
            "videoId": "pAgnJDJN4VA", "title": {"runs": [{"text": "AC/DC - Back In Black"}]}}}]}}
        self.assertEqual(find_youtube_video("back in black", fetch=lambda url: self.page(data)),
                         ("https://www.youtube.com/watch?v=pAgnJDJN4VA", "AC/DC - Back In Black"))

    def test_falls_back_to_any_video_id_and_survives_errors(self):
        html = 'algo "videoId":"fJ9rUzIMcZQ" más'
        self.assertEqual(find_youtube_video("queen", fetch=lambda url: html)[0],
                         "https://www.youtube.com/watch?v=fJ9rUzIMcZQ")
        self.assertIsNone(find_youtube_video("x", fetch=lambda url: "<html></html>"))

        def broken(url):
            raise OSError("sin red")

        self.assertIsNone(find_youtube_video("x", fetch=broken))

    def test_site_keys(self):
        self.assertEqual(site_key("YouTube"), "youtube")
        self.assertEqual(site_key("la página de GitHub"), "github")
        self.assertIsNone(site_key("spotify"))


class PersonaTests(unittest.TestCase):
    def test_time_is_answered_without_ai(self):
        from jarvis.tools import describe_time

        self.assertEqual(route("¿Qué hora es?"), (ToolId.TIME, {}))
        self.assertEqual(describe_time(datetime(2026, 11, 22, 18, 48)), "Son las 18:48 del domingo 22 de noviembre de 2026.")
        self.assertEqual(describe_time(datetime(2026, 11, 22, 13, 5)), "Es la 13:05 del domingo 22 de noviembre de 2026.")

    def test_greets_by_time_of_day(self):
        self.assertEqual(greeting(datetime(2026, 11, 22, 8)), "Buenos días")
        self.assertEqual(greeting(datetime(2026, 11, 22, 15)), "Buenas tardes")
        self.assertEqual(greeting(datetime(2026, 11, 22, 22)), "Buenas noches")
        self.assertEqual(welcome(datetime(2026, 11, 22, 22)), "Buenas noches, señor. Todos los sistemas en línea.")

    def test_dresses_results_without_changing_them(self):
        rng = random.Random(0)
        message = "CPU 5.9 % · RAM 9.34 de 15.34 GiB."
        dressed = dress(ToolId.SYSTEM_METRICS, 0, message, rng=rng)
        self.assertTrue(dressed.endswith(message))
        self.assertIn("señor", dressed)
        self.assertTrue(dress(ToolId.OPEN_APP, 2, "No encontré «x».").startswith("Me temo"))
        self.assertEqual(dress(ToolId.SET_VOLUME, 2, "Acción rechazada por el usuario."),
                         "Como usted diga, señor. No haré cambios.")
        self.assertTrue(dress(None, 2, "Orden no reconocida.").startswith("Disculpe, señor"))


if __name__ == "__main__":
    unittest.main()
