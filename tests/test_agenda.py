import gc
import importlib.util
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import Mock, patch
from uuid import uuid4

from jarvis import calendars as calendars_module
from jarvis.agenda import describe, find_tasks, period_range, select
from jarvis.audit import Audit
from jarvis.calc import CalcError, calculate, spoken_expression
from jarvis.calendars import Calendars, clean_url, fetch, read_events
from jarvis.core import _route, run_command
from jarvis.memory import MemoryStore
from jarvis.tools import AgendaData, AgendaItem, CalendarError, LocalTools, Task, ToolId, normalize
from jarvis.voice import speakable
from jarvis.weather import CityNotFound, report

HAS_ICAL = importlib.util.find_spec("recurring_ical_events") is not None
NOW = datetime(2026, 11, 25, 10, 0)  # miércoles
MOODLE = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2024//EN\r
BEGIN:VEVENT\r
UID:1@savio\r
SUMMARY:Taller 2 de F\xc3\xadsica est\xc3\xa1 en\r
  fecha de entrega\r
DTSTART:20261128T045900Z\r
DTEND:20261128T045900Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:2@savio\r
SUMMARY:Quiz viejo\r
DTSTART:20261120T150000Z\r
DTEND:20261120T150000Z\r
END:VEVENT\r
END:VCALENDAR\r
"""
OUTLOOK = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:Microsoft Exchange Server 2010\r
BEGIN:VTIMEZONE\r
TZID:SA Pacific Standard Time\r
BEGIN:STANDARD\r
DTSTART:16010101T000000\r
TZOFFSETFROM:-0500\r
TZOFFSETTO:-0500\r
END:STANDARD\r
END:VTIMEZONE\r
BEGIN:VEVENT\r
UID:a\r
SUMMARY:Reuni\xc3\xb3n de grupo\r
DTSTART;TZID=SA Pacific Standard Time:20261112T160000\r
DTEND;TZID=SA Pacific Standard Time:20261112T170000\r
RRULE:FREQ=WEEKLY;BYDAY=TH\r
EXDATE;TZID=SA Pacific Standard Time:20261203T160000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:b\r
SUMMARY:Cancelada\r
STATUS:CANCELLED\r
DTSTART;TZID=SA Pacific Standard Time:20261126T090000\r
DTEND;TZID=SA Pacific Standard Time:20261126T100000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:c\r
SUMMARY:Semana universitaria\r
DTSTART;VALUE=DATE:20261124\r
DTEND;VALUE=DATE:20261127\r
END:VEVENT\r
END:VCALENDAR\r
"""


class FakeVault(dict):
    def names(self):
        return sorted(self)

    def set(self, name, secret):
        self[name] = secret

    def delete(self, name):
        return self.pop(name, None) is not None


def route(text):
    return _route(normalize(text), text)


class QuestionTests(unittest.TestCase):
    def test_questions_about_the_agenda(self):
        cases = {
            "Jarvis, ¿tengo algún pendiente para esta semana?": ("semana", "todo"),
            "que tareas tengo pendientes.": ("semana", "tareas"),
            "¿Tengo reuniones mañana?": ("manana", "eventos"),
            "¿qué tengo para hoy?": ("hoy", "todo"),
            "¿tengo algo el lunes?": ("lunes", "todo"),
            "¿Cuáles son mis entregas de la próxima semana?": ("proxima_semana", "tareas"),
            "¿tengo alguna reunión o tarea este mes?": ("mes", "todo"),
        }
        for text, (period, kind) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text), (ToolId.AGENDA, {"period": period, "kind": kind}))

    def test_other_phrases_go_to_the_ai(self):
        for text in ("tengo reunión mañana", "qué es una reunión", "cuál es la mejor agenda", "tengo hambre hoy",
                     "agrega reunión con Juan mañana a las 3", "anota la tarea taller para el viernes",
                     "vivo en un apartamento", "cuánto es 1.500 más 2", "calcula la masa del sol"):
            with self.subTest(text=text):
                self.assertIsNone(route(text)[0])

    def test_periods(self):
        self.assertEqual(period_range("hoy", NOW), (NOW, datetime(2026, 11, 26), "para hoy"))
        self.assertEqual(period_range("semana", NOW)[1], datetime(2026, 11, 30))
        self.assertEqual(period_range("proxima_semana", NOW)[:2], (datetime(2026, 11, 30), datetime(2026, 12, 7)))
        self.assertEqual(period_range("mes", NOW)[1], datetime(2026, 12, 1))
        self.assertEqual(period_range("viernes", NOW), (datetime(2026, 11, 27), datetime(2026, 11, 28),
                                                        "para el viernes 27"))
        self.assertEqual(period_range("miercoles", NOW)[0], NOW)  # hoy mismo
        sunday = datetime(2026, 11, 29, 20, 0)
        self.assertEqual(period_range("semana", sunday)[1], datetime(2026, 11, 30))


class DescribeTests(unittest.TestCase):
    def items(self):
        return [
            AgendaItem(datetime(2026, 11, 27, 23, 59), False, "Taller 2", "tarea", "SAVIO"),
            AgendaItem(datetime(2026, 11, 26, 16, 0), False, "Reunión de grupo", "evento", "Outlook"),
            AgendaItem(None, True, "comprar pan", "tarea"),
            AgendaItem(datetime(2026, 11, 24), True, "leer capítulo 3", "tarea"),
        ]

    def test_short_agenda_is_one_sentence(self):
        self.assertEqual(
            describe(self.items(), "para esta semana", "todo", NOW, calendars=1),
            "Tiene 4 pendientes para esta semana: atrasada, «leer capítulo 3», era para el 24 de noviembre; mañana a "
            "las 16:00, «Reunión de grupo» (Outlook); el viernes 27 a las 23:59, «Taller 2» (SAVIO); sin fecha, "
            "«comprar pan».")

    def test_filters_and_removes_duplicates(self):
        items = self.items() + [AgendaItem(datetime(2026, 11, 27, 23, 59), False, "taller 2", "tarea", "Outlook")]
        self.assertEqual(len(select(items, "tareas")), 3)
        self.assertEqual([item.title for item in select(items, "eventos")], ["Reunión de grupo"])

    def test_long_agenda_speaks_the_first_and_shows_the_rest(self):
        items = [AgendaItem(NOW + timedelta(hours=n), False, f"cosa {n}", "evento") for n in range(1, 9)]
        message = describe(items, "para hoy", "todo", NOW, calendars=1)
        spoken = speakable(message)
        self.assertTrue(spoken.startswith("Tiene 8 pendientes para hoy. Lo primero: hoy a las 11:00, cosa 1;"), spoken)
        self.assertNotIn("cosa 4", spoken)
        self.assertIn("«cosa 8»", message)

    def test_empty_never_hides_a_failure(self):
        self.assertIn("aún no hay calendarios", describe([], "para hoy", "todo", NOW))
        self.assertEqual(describe([], "para hoy", "tareas", NOW, calendars=1), "No tiene tareas para hoy.")
        failed = describe([], "para hoy", "todo", NOW, warnings=["No pude consultar SAVIO: sin red."], calendars=1)
        self.assertNotIn("No tiene", failed)
        self.assertIn("incompleta. No pude consultar SAVIO", failed)


@unittest.skipUnless(HAS_ICAL, "Requiere icalendar (entorno .venv)")
class EventTests(unittest.TestCase):
    def setUp(self):
        offset = NOW.astimezone().utcoffset()
        if offset != timedelta(hours=-5):
            self.skipTest("Las horas esperadas son de Colombia.")

    def test_moodle_deadlines_are_tasks_in_local_time(self):
        items = read_events(MOODLE, "SAVIO", NOW, datetime(2026, 11, 30))
        self.assertEqual(items, [AgendaItem(datetime(2026, 11, 27, 23, 59), False,
                                            "Taller 2 de Física está en fecha de entrega", "tarea", "SAVIO")])

    def test_outlook_repeats_exceptions_cancellations_and_all_day(self):
        items = read_events(OUTLOOK, "Outlook", NOW, datetime(2026, 12, 12))
        meetings = [item.when for item in items if item.title == "Reunión de grupo"]
        self.assertEqual(meetings, [datetime(2026, 11, 26, 16, 0), datetime(2026, 12, 10, 16, 0)])
        self.assertNotIn("Cancelada", [item.title for item in items])
        week = next(item for item in items if item.title == "Semana universitaria")
        self.assertEqual((week.when, week.all_day, week.kind), (datetime(2026, 11, 25), True, "evento"))

    def test_login_pages_and_garbage_are_rejected(self):
        for raw in (b"<html>Iniciar sesion</html>", b"", b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:hoy"):
            with self.subTest(raw=raw), self.assertRaises(CalendarError):
                read_events(raw, "X", NOW, NOW + timedelta(days=1))


@unittest.skipUnless(HAS_ICAL, "Requiere icalendar (entorno .venv)")
class CalendarsTests(unittest.TestCase):
    def setUp(self):
        self.vault, self.time = FakeVault(), 0.0
        self.download = Mock(return_value=MOODLE)
        self.calendars = Calendars(self.vault, download=self.download, clock=lambda: self.time)
        self.end = NOW + timedelta(days=5)

    def test_only_saves_links_that_work(self):
        self.download.side_effect = CalendarError("el servidor rechazó el enlace (código 403)")
        with self.assertRaises(CalendarError):
            self.calendars.add("SAVIO", "https://x.test/cal?token=SECRETO")
        self.assertEqual(self.vault, {})
        self.download.side_effect = None
        self.calendars.add("SAVIO", " webcal://x.test/cal?token=SECRETO ")
        self.assertEqual(self.vault, {"SAVIO": "https://x.test/cal?token=SECRETO"})
        for bad in ("http://x.test/a", "file:///c:/x", "hola", "https://"):
            with self.subTest(bad=bad), self.assertRaises(CalendarError):
                clean_url(bad)

    def test_caches_and_uses_old_data_when_the_server_fails(self):
        self.calendars.add("SAVIO", "https://x.test/cal?token=SECRETO")
        self.assertEqual(len(self.calendars.events(NOW, self.end)[0]), 1)
        self.assertEqual(self.download.call_count, 1)
        self.time = 16 * 60
        self.download.side_effect = CalendarError("no hubo conexión con el servidor")
        items, warnings = self.calendars.events(NOW, self.end)
        self.assertEqual(len(items), 1)
        self.assertIn("No pude actualizar SAVIO", warnings[0])
        self.time = 30 * 3600
        items, warnings = self.calendars.events(NOW, self.end)
        self.assertEqual(items, [])
        self.assertIn("Puede faltar algo", warnings[0])
        self.assertNotIn("SECRETO", "".join(warnings))

    def test_a_login_page_does_not_replace_good_data(self):
        self.calendars.add("SAVIO", "https://x.test/cal")
        self.time, self.download.return_value = 16 * 60, b"<html>login</html>"
        self.assertEqual(len(self.calendars.events(NOW, self.end)[0]), 1)

    def test_one_broken_calendar_does_not_hide_the_others(self):
        self.calendars.add("SAVIO", "https://bueno.test/cal")
        self.vault["Outlook"] = "https://malo.test/cal"
        self.download.side_effect = CalendarError("no hubo conexión con el servidor")
        items, warnings = self.calendars.events(NOW, self.end)
        self.assertEqual((len(items), len(warnings)), (1, 1))

    def test_imported_files_update_themselves_and_warn_when_old(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        downloads, folder = Path(temporary.name) / "Descargas", Path(temporary.name) / "calendarios"
        downloads.mkdir()
        calendars = Calendars(self.vault, folder=folder, download=self.download)
        first = downloads / "icalexport.ics"
        first.write_bytes(MOODLE)
        self.vault["SAVIO"] = "https://viejo.test/"
        calendars.import_file("SAVIO", first)
        self.assertEqual((calendars.names(), dict(self.vault)), (["SAVIO"], {}))  # sustituye al enlace
        self.assertEqual([len(part) for part in calendars.events(NOW, self.end)], [1, 0])
        future = datetime.now().timestamp() + 60
        (downloads / "icalexport (1).ics").write_bytes(MOODLE.replace(b"Taller 2", b"Taller 9"))
        (downloads / "otro.ics").write_bytes(MOODLE.replace(b"Taller 2", b"Ajeno"))
        (downloads / "icalexport (2).ics").write_bytes(b"<html>")
        os.utime(downloads / "icalexport (1).ics", (future, future))
        os.utime(downloads / "otro.ics", (future + 90, future + 90))
        self.assertIn("Taller 9", calendars.events(NOW, self.end)[0][0].title)
        os.utime(downloads / "icalexport (2).ics", (future + 120, future + 120))
        self.assertIn("Taller 9", calendars.events(NOW, self.end)[0][0].title)  # uno dañado no lo sustituye
        for path in downloads.iterdir():
            path.unlink()
        old = datetime.now() - timedelta(days=5)
        details = folder / "SAVIO.json"
        details.write_text(details.read_text(encoding="utf-8").replace(str(future), str(old.timestamp())), encoding="utf-8")
        items, warnings = calendars.events(NOW, self.end)
        self.assertEqual(len(items), 1)
        self.assertIn(f"es del {old:%d/%m}", warnings[0])
        self.assertTrue(calendars.remove("SAVIO"))
        self.assertEqual((calendars.names(), list(folder.iterdir())), ([], []))

    def test_rejects_files_that_are_not_calendars(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        calendars = Calendars(self.vault, folder=folder / "c", download=self.download)
        for name, content in (("a.txt", MOODLE), ("b.ics", b"<html>")):
            (folder / name).write_bytes(content)
            with self.subTest(name=name), self.assertRaises(CalendarError):
                calendars.import_file("SAVIO", folder / name)
        for bad in ("../x", "a/b", ""):
            with self.subTest(bad=bad), self.assertRaises(CalendarError):
                calendars.import_file(bad, folder / "b.ics")
        self.assertEqual(calendars.names(), [])

    def test_bot_protection_is_explained(self):
        error = urllib.error.HTTPError("https://x.test/", 403, "no", {"cf-mitigated": "challenge"}, None)
        with patch.object(calendars_module.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(CalendarError, "Importar archivo"):
                fetch("https://x.test/")

    def test_network_errors_never_reveal_the_link(self):
        error = urllib.error.HTTPError("https://x.test/?token=SECRETO", 403, "no", {}, None)
        with patch.object(calendars_module.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(CalendarError) as caught:
                fetch("https://x.test/?token=SECRETO")
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("SECRETO", str(caught.exception))


class TaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.audit, self.store = Audit(folder / "a.sqlite3"), MemoryStore(folder / "m.sqlite3")
        self.tools = Mock(spec=LocalTools)
        self.tools.add_task.side_effect = self.store.add_task
        self.tools.open_tasks.side_effect = self.store.open_tasks
        self.tools.finish_task.side_effect = lambda task_id: self.store.close_task(task_id, 1)
        self.tools.remove_task.side_effect = lambda task_id: self.store.close_task(task_id, 2)

    def run_text(self, text):
        return run_command(text, self.tools, self.audit)

    def test_add_finish_and_remove(self):
        self.assertEqual(self.run_text("anota que tengo que leer Cien Años").message,
                         "Anotado (tarea 1): «leer Cien Años», sin fecha.")
        self.run_text("anota comprar pan")
        self.assertEqual(self.run_text("ya terminé la tarea de leer cien años").message,
                         "Marcada como hecha: «leer Cien Años».")
        self.assertEqual(self.run_text("borra la tarea 2").message, "Borrada: «comprar pan».")
        self.assertEqual(self.store.open_tasks(), [])
        self.assertIn("No encontré", self.run_text("borra la tarea 2").message)

    def test_never_guesses_which_task(self):
        self.store.add_task("taller de física", None, False, "tarea")
        self.store.add_task("taller de cálculo", None, False, "tarea")
        self.assertIn("¿Qué número?", self.run_text("borra la tarea taller").message)
        self.assertEqual(len(self.store.open_tasks()), 2)
        # Parecida pero no igual: sirve para marcarla como hecha, nunca para borrarla.
        self.assertEqual(len(find_tasks(self.store.open_tasks(), "tallar de fisica")), 1)
        self.assertEqual(find_tasks(self.store.open_tasks(), "tallar de fisica", fuzzy=False), [])

    def test_dates_are_validated(self):
        from jarvis.tools import TOOLS

        spec = TOOLS[ToolId.ADD_TASK]
        past = {"text": "x", "due": "2020-01-01", "kind": "tarea"}
        self.assertEqual(spec.handler(self.tools, past).code, 2)
        self.assertEqual(spec.handler(self.tools, {"text": "Juan", "due": "", "kind": "evento"}).message,
                         "¿Para qué día anoto «Juan», señor?")
        self.assertFalse(spec.accepts_arguments({"text": "x", "due": "mañana", "kind": "tarea"}))
        self.assertTrue(spec.accepts_arguments({"text": "x", "due": "", "kind": "tarea"}))
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertIn("mañana a las 15:00", spec.handler(
            self.tools, {"text": "reunión con Juan", "due": f"{tomorrow}T15:00", "kind": "evento"}).message)

    def test_agenda_and_summary_use_the_tools(self):
        self.tools.agenda.return_value = AgendaData([AgendaItem(None, True, "comprar pan", "tarea")], [], 1)
        self.assertEqual(self.run_text("¿tengo algún pendiente esta semana?").message,
                         "Tiene 1 pendiente para esta semana: sin fecha, «comprar pan».")
        self.tools.get_city.return_value = "Medellín"
        self.tools.weather.side_effect = OSError("sin red")
        message = self.run_text("resumen del día").message
        self.assertIn("No pude consultar el clima.", message)
        self.assertIn("comprar pan", message)

    def test_calendar_commands(self):
        self.tools.request_calendar.return_value = False
        self.assertEqual(self.run_text("conecta un calendario").code, 2)
        self.tools.calendar_names.return_value = ["SAVIO"]
        self.assertIn("SAVIO", self.run_text("qué calendarios tengo").message)
        self.assertEqual(self.run_text("desconecta el calendario savio").code, 2)  # exige aprobación
        self.tools.remove_calendar.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Requiere Windows")
class WindowsAgendaTests(unittest.TestCase):
    def test_own_tasks_reminders_and_calendars_together(self):
        from jarvis.windows import WindowsTools

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = MemoryStore(Path(temporary.name) / "m.sqlite3")
        tools = WindowsTools(store)
        meeting = AgendaItem(datetime(2026, 11, 26, 16, 0), False, "Reunión", "evento", "Outlook")
        tools._calendars = Mock()
        tools._calendars.events.return_value = ([meeting], ["aviso"])
        tools._calendars.names.return_value = ["Outlook"]
        for text, due, has_time, kind in (
            ("atrasada", datetime(2026, 11, 20), False, "tarea"), ("hoy", datetime(2026, 11, 25), False, "tarea"),
            ("evento pasado", datetime(2026, 11, 25, 8, 0), True, "evento"), ("sin fecha", None, False, "tarea"),
            ("diciembre", datetime(2026, 12, 20), False, "tarea"),
        ):
            store.add_task(text, due, has_time, kind)
        store.add_reminder("llamar", datetime(2026, 11, 27, 9, 0), "recordatorio")
        store.add_reminder("", datetime(2026, 11, 25, 10, 5), "temporizador")
        data = tools.agenda(NOW, datetime(2026, 11, 30), NOW)
        self.assertEqual({item.title for item in data.items}, {"atrasada", "hoy", "sin fecha", "llamar", "Reunión"})
        self.assertEqual((data.warnings, data.calendars), (["aviso"], 1))
        friday = tools.agenda(datetime(2026, 11, 27), datetime(2026, 11, 28), NOW)
        self.assertEqual({item.title for item in friday.items}, {"llamar", "Reunión"})

    def test_secrets_survive_in_the_windows_vault(self):
        from jarvis.vault import CredentialVault

        vault = CredentialVault(f"JARVIS-prueba/{uuid4().hex}/")
        self.addCleanup(vault.delete, "SAVIO")
        self.assertEqual((vault.names(), vault.get("SAVIO"), vault.delete("SAVIO")), ([], None, False))
        vault.set("SAVIO", "https://x.test/ñ?token=abc")
        self.assertEqual((vault.names(), vault.get("SAVIO")), (["SAVIO"], "https://x.test/ñ?token=abc"))
        self.assertTrue(vault.delete("SAVIO"))
        self.assertIsNone(vault.get("SAVIO"))

    def test_dialog_checks_the_link_and_clears_it(self):
        import tkinter as tk

        from jarvis.calendar_dialog import CalendarDialog

        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        root.withdraw()
        calendars = Mock()
        calendars.add.side_effect = [CalendarError("el servidor rechazó el enlace (código 403)"), 3]
        dialog = CalendarDialog(root, calendars)
        try:
            self.assertEqual(dialog.link["show"], "•")
            for expected, kept in (("403", True), ("3 actividades", False)):
                dialog.link.delete(0, "end")
                dialog.link.insert(0, "https://x.test/cal")
                dialog.save()
                deadline = monotonic() + 3
                while dialog._busy and monotonic() < deadline:
                    root.update()
                    sleep(0.01)
                self.assertIn(expected, dialog.status["text"])
                self.assertEqual(bool(dialog.link.get()), kept)
            calendars.add.assert_called_with("SAVIO", "https://x.test/cal")
        finally:
            dialog.close()
            self.assertFalse(dialog.alive)
            root.destroy()
            del dialog, root
            gc.collect()


class CalcTests(unittest.TestCase):
    def test_exact_results(self):
        cases = {"15*8": "120", "(15/100)*80": "12", "10/4": "2,5", "20%": "0,2", "2^10": "1024", "sqrt(81)": "9",
                 "17 % 5": "2", "0.1+0.2": "0,3"}
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(calculate(expression), expected)
        self.assertEqual(spoken_expression("cuanto es el 15 por ciento de 80"), "(15/100)*80")

    def test_never_runs_code_or_huge_numbers(self):
        for expression in ("__import__('os')", "9**9**9", "1/0", "a+1", "(1).real", "2**9999", "", "[1]*9", "sqrt(-1)"):
            with self.subTest(expression=expression), self.assertRaises(CalcError):
                calculate(expression)


class WeatherTests(unittest.TestCase):
    PLACE = {"results": [{"name": "Medellín", "country": "Colombia", "latitude": 6.2, "longitude": -75.6}]}
    DATA = {"current": {"temperature_2m": 22.4, "apparent_temperature": 23.6, "weather_code": 61},
            "daily": {"weather_code": [61, 3], "temperature_2m_max": [27.1, 28], "temperature_2m_min": [17, 18],
                      "precipitation_probability_max": [80, None]}}

    def test_reports(self):
        get = Mock(side_effect=[self.PLACE, self.DATA] * 2)
        self.assertEqual(report("Medellin", get=get), "En Medellín, Colombia hay lluvia ligera y 22 grados, con sensación "
                         "de 24. Hoy, entre 17 y 27 grados y 80 % de probabilidad de lluvia.")
        self.assertEqual(report("Medellin", "manana", get=get),
                         "Mañana en Medellín, Colombia: cielo nublado, entre 18 y 28 grados.")

    def test_unknown_city_and_bad_answers(self):
        with self.assertRaises(CityNotFound):
            report("Xyz", get=Mock(return_value={}))
        with self.assertRaises(OSError):
            report("Medellin", get=Mock(side_effect=[self.PLACE, {"daily": {}}]))


if __name__ == "__main__":
    unittest.main()
