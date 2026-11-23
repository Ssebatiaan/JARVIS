import os
import tempfile
import unittest
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import Mock

from jarvis.core import _route, run_command
from jarvis.tools import FAREWELL, LocalTools, ToolId, normalize

try:
    import tkinter as tk
    from jarvis.desktop import DesktopWindow
except ImportError:
    tk = None


def release_tk(test):
    import gc

    for name in ("window", "root"):
        if hasattr(test, name):
            setattr(test, name, None)
    gc.collect()


class DismissRoutingTests(unittest.TestCase):
    def test_farewells_are_recognised_without_ai(self):
        for text in ("Eso sería todo, Jarvis", "Gracias Jarvis, eso es todo por hoy", "Jarvis, puedes retirarte",
                     "descansa jarvis", "esconde el overlay", "eso fue todo, señor", "adiós Jarvis", "hasta mañana", "bueno me voy a dormir, que descanses"):
            with self.subTest(text=text):
                self.assertEqual(_route(normalize(text), text), (ToolId.DISMISS, {}))
        for text in ("eso sería todo lo que necesito saber", "qué es todo esto", "abre todo", "buenas noches jarvis"):
            with self.subTest(text=text):
                self.assertNotEqual(_route(normalize(text), text)[0], ToolId.DISMISS)

    def test_the_farewell_is_said_in_character_and_asks_the_interface_to_hide(self):
        with tempfile.TemporaryDirectory() as folder:
            from jarvis.audit import Audit

            tools = Mock(spec=LocalTools)
            result = run_command("Eso sería todo, Jarvis", tools, Audit(Path(folder) / "a.sqlite3"), persona=True)
        self.assertEqual(result.message, FAREWELL)
        tools.dismiss.assert_called_once_with()


@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class DismissWindowTests(unittest.TestCase):
    def setUp(self):
        from jarvis.audit import Audit

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(release_tk, self)
        self.tools = Mock(spec=LocalTools)
        self.tools.take_dismiss = Mock(return_value=True)
        self.voice = Mock(speaking=True)
        self.window = DesktopWindow(self.root, self.tools, Audit(Path(temporary.name) / "a.sqlite3"),
                                    voice=self.voice)
        self.window.floating_orb = Mock()
        self.addCleanup(self.close)

    def close(self):
        try:
            self.window.floating_orb = None
            self.window.close()
        except (AttributeError, tk.TclError):
            pass

    def wait_until(self, condition, seconds=4):
        deadline = monotonic() + seconds
        while not condition():
            self.root.update()
            if monotonic() >= deadline:
                self.fail("La ventana no alcanzó el estado esperado.")
            sleep(0.01)

    def test_hides_orb_and_chat_only_after_finishing_the_farewell(self):
        self.root.deiconify()
        self.window.command.set("eso sería todo, jarvis")
        self.window.submit()
        self.wait_until(lambda: self.window._task is None and self.window.status.get() == "Completado")
        self.voice.say.assert_called_once_with(FAREWELL)
        deadline = monotonic() + 0.8
        while monotonic() < deadline:
            self.root.update()
            sleep(0.01)
        self.window.floating_orb.hide.assert_not_called()  # todavía está hablando
        self.voice.speaking = False
        self.wait_until(lambda: self.window.floating_orb.hide.called)
        self.assertEqual(self.root.state(), "withdrawn")
        self.window.show_from_tray()  # «Hey Jarvis», AltGr+J o la bandeja lo traen de vuelta
        self.window.floating_orb.show.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
