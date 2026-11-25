import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.audit import Audit
from jarvis.brain import TIME_WORDS
from jarvis.core import _route, run_command
from jarvis.memory import MAX_PENDING_REMINDERS, MemoryStore
from jarvis.reminders import ReminderClock, announcement, describe_duration
from jarvis.tools import GpuReading, LocalTools, ProgramUsage, Reminder, RemindersFull, ToolId, normalize
from jarvis.when import describe_due, parse_duration, parse_reminder
from jarvis.windows import WindowsTools

NOW = datetime(2026, 11, 22, 21, 30)  # domingo


def parse(text, now=NOW):
    return parse_reminder(normalize(text), now)


def route(text):
    return _route(normalize(text), text)


class ParserTests(unittest.TestCase):
    def test_relative_times_and_timers(self):
        cases = {
            "pon un temporizador de 20 minutos": (NOW + timedelta(minutes=20), "", "temporizador"),
            "temporizador de media hora": (NOW + timedelta(minutes=30), "", "temporizador"),
            "pon un temporizador de una hora y media": (NOW + timedelta(minutes=90), "", "temporizador"),
            "temporizador de 45 segundos": (NOW + timedelta(seconds=45), "", "temporizador"),
            "recuérdame en 20 minutos sacar la ropa": (NOW + timedelta(minutes=20), "sacar la ropa", "recordatorio"),
            "avísame dentro de 2 horas y 15 minutos": (NOW + timedelta(minutes=135), "", "recordatorio"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse(text), expected)

    def test_clock_times_and_days(self):
        cases = {
            "recuérdame a las 10 estudiar": (datetime(2026, 11, 22, 22, 0), "estudiar"),
            "recuérdame a las 8 de la noche que tengo clase": (datetime(2026, 11, 23, 20, 0), "tengo clase"),
            "recuérdame mañana a las 7 que tengo examen": (datetime(2026, 11, 23, 7, 0), "tengo examen"),
            "recuérdame mañana a las 3 ir al gimnasio": (datetime(2026, 11, 23, 15, 0), "ir al gimnasio"),
            "recuérdame mañana por la tarde a las 4 llamar": (datetime(2026, 11, 23, 16, 0), "llamar"),
            "recuérdame el viernes a las 2 de la tarde entregar el taller": (datetime(2026, 11, 27, 14, 0),
                                                                           "entregar el taller"),
            "recuérdame el lunes pagar el arriendo": (datetime(2026, 11, 23, 9, 0), "pagar el arriendo"),
            "avísame a las once menos cuarto": (datetime(2026, 11, 22, 22, 45), ""),
            "recuérdame al mediodía almorzar": (datetime(2026, 11, 23, 12, 0), "almorzar"),
        }
        for text, (due, task) in cases.items():
            with self.subTest(text=text):
                moment, what, kind = parse(text)
                self.assertEqual((moment, what, kind), (due, task, "recordatorio"))

    def test_tricky_hours(self):
        morning = datetime(2026, 11, 25, 8, 0)
        cases = {
            "recuérdame a la una de la tarde almorzar": (datetime(2026, 11, 25, 13, 0), "almorzar"),
            "recuérdame a las siete y veinte de la noche salir": (datetime(2026, 11, 25, 19, 20), "salir"),
            "recuérdame esta noche a las 9 estudiar": (datetime(2026, 11, 25, 21, 0), "estudiar"),
            "recuérdame a las 12 de la noche dormir": (datetime(2026, 11, 26, 0, 0), "dormir"),
            "recuérdame a las 15:30 reunión": (datetime(2026, 11, 25, 15, 30), "reunion"),
        }
        for text, (due, task) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse(text, morning)[:2], (due, task))

    def test_alarms(self):
        self.assertEqual(parse("pon una alarma a las 6:30 de la mañana"), (datetime(2026, 11, 23, 6, 30), "", "alarma"))
        self.assertEqual(parse("despiértame a las 6 y media"), (datetime(2026, 11, 23, 6, 30), "", "alarma"))

    def test_leaves_the_rest_to_the_ai(self):
        for text in ("recuérdame algo importante", "recuerda que me gusta el café", "pon el volumen al 35",
                     "temporizador de cocina", "recuérdame el 30 de noviembre a las 3 de la tarde entregar",
                     "recuérdame en 3 días a las 5 llamar", "recuérdame todos los días a las 8 tomar agua",
                     "recuérdame la próxima semana a las 9 ir al banco", "recuérdame el viernes 4 a las 2 el parcial",
                     "recuérdame el 30 a las 3 entregar", "avísame a las 45"):
            with self.subTest(text=text):
                self.assertIsNone(parse(text))

    def test_durations_and_descriptions(self):
        self.assertEqual(parse_duration("3 horas"), timedelta(hours=3))
        self.assertIsNone(parse_duration("un rato"))
        self.assertEqual(describe_due(NOW + timedelta(minutes=20), NOW), "en 20 minutos (a las 21:50)")
        self.assertEqual(describe_due(datetime(2026, 11, 23, 7, 0), NOW), "mañana a las 07:00")
        self.assertEqual(describe_due(datetime(2026, 11, 27, 14, 0), NOW), "el viernes 27 a las 14:00")
        self.assertEqual(describe_duration(timedelta(minutes=90)), "1 hora y 30 minutos")
        self.assertEqual(describe_duration(timedelta(seconds=45)), "45 segundos")

    def test_the_brain_gets_the_time_for_reminders(self):
        self.assertTrue(TIME_WORDS.search("recuérdame el viernes"))
        for text in ("pon una alarma", "en 3 horas", "en dos días", "recuérdame eso", "el 30 de noviembre"):
            self.assertTrue(TIME_WORDS.search(text), text)
        self.assertFalse(TIME_WORDS.search("hola Jarvis"))


class RoutingTests(unittest.TestCase):
    def test_hardware_questions(self):
        cases = {
            "cuánto usa la GPU": (ToolId.GPU, {}),
            "temperatura de la gráfica": (ToolId.GPU, {}),
            "¿cómo está la tarjeta de video?": (ToolId.GPU, {}),
            "qué está consumiendo más RAM": (ToolId.TOP_PROGRAMS, {"by": "ram"}),
            "qué programa usa más CPU": (ToolId.TOP_PROGRAMS, {"by": "cpu"}),
            "qué se está comiendo la memoria": (ToolId.TOP_PROGRAMS, {"by": "ram"}),
            "abre GPU-Z": (ToolId.OPEN_APP, {"app": "gpu-z"}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text), expected)

    def test_ordinary_questions_go_to_the_ai(self):
        for text in ("qué me comentaste de la memoria", "dime cómo se come la memoria caché el procesador",
                     "qué es una gpu", "cuál gráfica me recomiendas comprar", "cuánto cuesta una gpu",
                     "qué es una alarma", "apaga la alarma"):
            with self.subTest(text=text):
                self.assertIsNone(route(text)[0])

    def test_cpu_temperature_in_the_words_of_the_user(self):
        for text in ("Me puede decir qué temperatura tiene mi CPU.", "¿Puedes decirme a qué temperatura esta mi CPU?",
                     "temperatura del procesador", "¿qué tan caliente está la CPU?"):
            with self.subTest(text=text):
                self.assertEqual(route(text), (ToolId.CPU_TEMPERATURE, {}))
        self.assertEqual(route("¿me puedes decir cuánto usa la GPU?"), (ToolId.GPU, {}))
        self.assertIsNone(route("¿qué temperatura es peligrosa para una CPU?")[0])

    def test_reminder_commands(self):
        tool, arguments = route("Jarvis, recuérdame mañana a las 7 que tengo examen de Cálculo")
        self.assertIs(tool, ToolId.ADD_REMINDER)
        self.assertEqual(arguments["text"], "tengo examen de Cálculo")  # conserva tildes y mayúsculas
        self.assertEqual(arguments["kind"], "recordatorio")
        self.assertRegex(arguments["due"], r"T07:00:00$")
        self.assertEqual(route("qué recordatorios tengo"), (ToolId.LIST_REMINDERS, {}))
        self.assertEqual(route("cuánto falta"), (ToolId.LIST_REMINDERS, {}))
        self.assertEqual(route("cancela el temporizador"), (ToolId.CANCEL_REMINDER, {"reminder_id": 0, "kind": "temporizador"}))
        self.assertEqual(route("cancela la alarma"), (ToolId.CANCEL_REMINDER, {"reminder_id": 0, "kind": "alarma"}))
        self.assertEqual(route("borra el recordatorio 3"), (ToolId.CANCEL_REMINDER, {"reminder_id": 3, "kind": "aviso"}))

    def test_power_commands(self):
        cases = {
            "apaga el pc": (ToolId.POWER, {"action": "shutdown"}),
            "reinicia el computador": (ToolId.POWER, {"action": "restart"}),
            "suspende el equipo": (ToolId.POWER, {"action": "sleep"}),
            "bloquea el pc": (ToolId.LOCK, {}),
            "cancela el apagado": (ToolId.CANCEL_POWER, {}),
            "no apagues el pc": (ToolId.CANCEL_POWER, {}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(route(text), expected)


class ToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.audit = Audit(Path(temporary.name) / "audit.sqlite3")
        self.tools = Mock(spec=LocalTools)

    def run_text(self, text, **options):
        return run_command(text, self.tools, self.audit, **options)

    def test_gpu_and_top_programs(self):
        self.tools.read_gpu.return_value = GpuReading("RTX 3050", 7, 513, 4096, 45)
        self.assertEqual(self.run_text("cuánto usa la GPU").message,
                         "RTX 3050: 7 % de uso, 0.5 de 4 GiB de VRAM y 45 °C.")
        self.tools.read_gpu.return_value = None
        self.assertEqual(self.run_text("temperatura de la GPU").code, 2)
        self.tools.read_cpu_temperature.return_value = 67.4
        self.assertEqual(self.run_text("temperatura de la CPU").message,
                         "El procesador ronda los 67 °C, según el sensor térmico del equipo.")
        self.tools.top_programs.return_value = [
            ProgramUsage("opera", 29, 5084, 3.0), ProgramUsage("Code", 14, 2222, 9.5),
            ProgramUsage("Discord", 6, 850, 0.4),
        ]
        ram = self.run_text("qué está consumiendo más RAM").message
        self.assertTrue(ram.startswith("Lo que más memoria usa ahora: opera (29 procesos): 5.0 GiB; Code"), ram)
        cpu = self.run_text("qué programa usa más CPU").message
        self.assertTrue(cpu.startswith("Lo que más CPU usa ahora: Code (14 procesos): 10 % de CPU"), cpu)

    def test_add_reminder_validates_the_time(self):
        due = (datetime.now() + timedelta(minutes=20)).isoformat(timespec="seconds")
        result = self.run_text("recuérdame en 20 minutos sacar la ropa")
        self.assertEqual(result.code, 0, result.message)
        text, moment, kind = self.tools.add_reminder.call_args.args
        self.assertEqual((text, kind), ("sacar la ropa", "recordatorio"))
        self.assertAlmostEqual(moment.timestamp(), datetime.fromisoformat(due).timestamp(), delta=5)
        self.assertIn("Le recordaré «sacar la ropa» en 20 minutos", result.message)

        from jarvis.tools import TOOLS

        spec = TOOLS[ToolId.ADD_REMINDER]
        past = {"text": "", "due": "2020-01-01T10:00:00", "kind": "alarma"}
        self.assertEqual(spec.handler(self.tools, past).code, 2)
        self.assertFalse(spec.accepts_arguments({"text": "x", "due": "mañana", "kind": "alarma"}))
        self.assertFalse(spec.accepts_arguments({"text": "x", "due": "2026-12-01T10:00", "kind": "cita"}))
        self.tools.add_reminder.side_effect = RemindersFull()
        self.assertIn("demasiados", self.run_text("pon un temporizador de 5 minutos").message)

    def test_list_and_cancel(self):
        self.tools.list_reminders.return_value = []
        self.assertIn("No hay recordatorios", self.run_text("qué recordatorios tengo").message)
        reminder = Reminder(4, "sacar la ropa", datetime.now() + timedelta(minutes=10), "recordatorio")
        self.tools.list_reminders.return_value = [reminder]
        self.assertIn("4. Recordatorio «sacar la ropa», en 10 minutos", self.run_text("mis recordatorios").message)
        self.tools.cancel_reminder.return_value = reminder
        self.assertIn("Cancelado: Recordatorio «sacar la ropa»", self.run_text("cancela el recordatorio 4").message)
        self.tools.cancel_reminder.assert_called_with(4)
        self.tools.cancel_reminder.reset_mock()
        self.assertEqual(self.run_text("cancela la alarma").message, "No tengo una alarma pendiente.")
        self.tools.list_reminders.return_value = [reminder, Reminder(5, "", reminder.due, "recordatorio")]
        self.assertIn("¿Qué número cancelo?", self.run_text("cancela el recordatorio").message)
        self.tools.cancel_reminder.assert_not_called()

    def test_power_needs_approval_and_lock_does_not(self):
        refused = self.run_text("apaga el pc")
        self.assertEqual(refused.code, 2)
        self.tools.power.assert_not_called()
        rejected = self.run_text("reinicia el pc", approve=lambda request: False)
        self.assertEqual(rejected.code, 2)
        self.tools.power.assert_not_called()
        requests = []
        accepted = self.run_text("apaga el pc", approve=lambda request: requests.append(request) or True)
        self.assertEqual(accepted.code, 0)
        self.assertEqual(requests[0].summary, "Apagar el equipo dentro de 60 segundos.")
        self.tools.power.assert_called_once_with("shutdown")
        self.assertIn("cancela el apagado", accepted.message)
        self.assertEqual(self.run_text("bloquea el pc").code, 0)
        self.tools.lock.assert_called_once()
        self.tools.cancel_power.return_value = False
        self.assertEqual(self.run_text("cancela el apagado").code, 2)


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MemoryStore(Path(temporary.name) / "memoria.sqlite3")

    def test_pending_take_due_and_cancel(self):
        now = datetime.now().replace(microsecond=0)
        late = self.store.add_reminder("estudiar", now - timedelta(seconds=1), "recordatorio")
        soon = self.store.add_reminder("", now + timedelta(minutes=5), "temporizador")
        later = self.store.add_reminder("examen", now + timedelta(days=1), "alarma")
        self.assertEqual([item.id for item in self.store.pending_reminders()], [late, soon, later])
        self.assertEqual(self.store.next_due(), now - timedelta(seconds=1))
        self.assertEqual([item.text for item in self.store.take_due(now)], ["estudiar"])
        self.assertEqual(self.store.take_due(now), [])  # no avisa dos veces
        self.assertEqual(self.store.cancel_reminder(soon).id, soon)
        self.assertIsNone(self.store.cancel_reminder(soon))
        self.assertEqual([item.id for item in self.store.pending_reminders()], [later])

    def test_limit(self):
        due = datetime.now() + timedelta(hours=1)
        for _ in range(MAX_PENDING_REMINDERS):
            self.store.add_reminder("x", due, "recordatorio")
        with self.assertRaises(RemindersFull):
            self.store.add_reminder("x", due, "recordatorio")

    def test_clock_only_reads_when_something_is_due(self):
        now = datetime.now().replace(microsecond=0)
        self.store.add_reminder("ropa", now + timedelta(minutes=1), "recordatorio")
        clock = ReminderClock(self.store)
        self.assertEqual(clock.due(now), [])
        self.store.take_due = Mock(wraps=self.store.take_due)
        self.assertEqual(clock.due(now + timedelta(seconds=30)), [])
        self.store.take_due.assert_not_called()
        self.assertEqual([item.text for item in clock.due(now + timedelta(minutes=1))], ["ropa"])
        self.assertEqual(clock.due(now + timedelta(minutes=2)), [])


class WishTests(unittest.TestCase):
    def test_counts_orders_and_forgets(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = MemoryStore(Path(temporary.name) / "m.sqlite3")
        for text in ("leer correos", "entrar a un canal de voz", "entrar a un canal de voz."):
            store.add_wish(text)
        self.assertEqual(store.wishes(), ["entrar a un canal de voz", "leer correos"])
        store.forget_everything()
        self.assertEqual(store.wishes(), [])


class PowerTests(unittest.TestCase):
    """Nunca ejecuta shutdown.exe ni suspende: se sustituye la llamada."""

    def setUp(self):
        self.windows = WindowsTools()
        self.windows._shutdown_exe = Mock(return_value=0)

    def test_cancel_asks_windows_even_after_restarting_jarvis(self):
        self.assertTrue(self.windows.cancel_power())
        self.windows._shutdown_exe.assert_called_once_with("/a")
        self.windows._shutdown_exe.return_value = 1116
        self.assertFalse(self.windows.cancel_power())
        self.windows._shutdown_exe.return_value = 5
        with self.assertRaises(OSError):
            self.windows.cancel_power()

    def test_shutdown_and_sleep(self):
        self.windows.power("shutdown")
        self.assertEqual(self.windows._shutdown_exe.call_args.args[:3], ("/s", "/t", "60"))
        self.windows._shutdown_exe.return_value = 1116
        with patch("jarvis.windows._suspend") as suspend:
            self.windows.power("sleep")
            self.assertTrue(self.windows.cancel_power())
            self.assertFalse(self.windows.cancel_power())
            suspend.assert_not_called()


class AnnouncementTests(unittest.TestCase):
    def test_messages(self):
        due = datetime(2026, 11, 22, 21, 50)
        self.assertEqual(announcement(Reminder(1, "sacar la ropa", due, "recordatorio"), due),
                         "Señor, le recuerdo: sacar la ropa.")
        timer = Reminder(2, "", due, "temporizador", due - timedelta(minutes=20))
        self.assertEqual(announcement(timer, due), "Señor, su temporizador de 20 minutos ha terminado.")
        alarm = Reminder(3, "tengo examen", datetime(2026, 11, 23, 7, 0), "alarma")
        self.assertEqual(announcement(alarm, alarm.due), "Señor, son las 07:00. Tengo examen.")
        self.assertEqual(announcement(Reminder(4, "", due, "recordatorio"), due),
                         "Señor, me pidió que le avisara a esta hora.")
        late = announcement(Reminder(5, "estudiar", due, "recordatorio"), due + timedelta(hours=2))
        self.assertEqual(late, "Señor, quedó pendiente un recordatorio de las 21:50: estudiar.")


if __name__ == "__main__":
    unittest.main()
