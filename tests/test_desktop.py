import os
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from threading import Event
from time import monotonic, sleep
from unittest.mock import Mock, patch

from jarvis.audit import Audit
from jarvis.cli import main
from jarvis.tools import LocalTools, SystemUsage, VolumeStatus

try:
    import tkinter as tk
    from jarvis.desktop import DesktopWindow
except ImportError:
    tk = None
    DesktopWindow = None

def release_tk(test):
    """Suelta las referencias a Tk y recolecta en el hilo principal.

    unittest libera cada prueba al terminar; si el recolector de basura destruyera después la
    ventana desde un hilo de trabajo, Tcl abortaría el proceso («Tcl_AsyncDelete»).
    """
    import gc

    for name in ("window", "root"):
        if hasattr(test, name):
            setattr(test, name, None)
    gc.collect()



@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class DesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.audit_path = Path(temporary.name) / "auditoria.sqlite3"
        self.tools = Mock(spec=LocalTools)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(release_tk, self)
        self.window = DesktopWindow(self.root, self.tools, Audit(self.audit_path))
        self.root.update_idletasks()
        self.addCleanup(self.close_window)

    def close_window(self):
        with suppress(tk.TclError):
            self.window.close()
            self.wait_until(lambda: self.window._task is None)

    def wait_until(self, condition):
        deadline = monotonic() + 4
        while not condition():
            self.root.update()
            if monotonic() >= deadline:
                self.fail("La ventana no alcanzó el estado esperado.")
            sleep(0.01)

    def test_window_runs_command_in_background_and_displays_the_result(self):
        started = Event()
        release = Event()
        ui_callback = Event()
        self.addCleanup(release.set)

        def read_usage():
            started.set()
            release.wait(3)
            return SystemUsage(13, 8 * 1024**3, 3 * 1024**3)

        self.tools.read_usage.side_effect = read_usage
        self.window.command.set("consulta CPU y RAM")
        self.window.execute_button.invoke()
        self.wait_until(started.is_set)
        self.root.after(0, ui_callback.set)
        self.wait_until(ui_callback.is_set)
        self.assertEqual(self.window.status.get(), "En ejecución")
        self.assertEqual(str(self.window.execute_button["state"]), "disabled")
        release.set()

        self.wait_until(lambda: self.window.status.get() == "Completado")
        self.assertIn("CPU 13.0 %", self.window.output.get("1.0", "end"))
        self.tools.read_usage.assert_called_once_with()
        self.assertEqual(str(self.window.execute_button["state"]), "normal")

    def test_window_routes_explicit_approval_and_rejection_for_the_exact_value(self):
        self.tools.set_volume.return_value = VolumeStatus(35, False)
        for approved in (False, True):
            with self.subTest(approved=approved):
                self.window.command.set("pon el volumen al 35 %")
                self.window.execute_button.invoke()
                self.wait_until(lambda: self.window.status.get() == "Requiere aprobación")
                self.assertIn("35 %", self.window.prompt["text"])
                self.tools.set_volume.assert_not_called()
                button = self.window.approve_button if approved else self.window.reject_button
                button.invoke()
                expected = "Completado" if approved else "Detenido"
                self.wait_until(lambda: self.window.status.get() == expected)
        self.tools.set_volume.assert_called_once_with(35)
        self.assertIn("Volumen ajustado a 35 %", self.window.output.get("1.0", "end"))

    def test_window_cancels_a_sequence_while_approval_is_pending(self):
        self.tools.read_usage.return_value = SystemUsage(9, 8 * 1024**3, 3 * 1024**3)
        self.window.command.set(
            "consulta CPU y RAM y luego pon el volumen al 35 % y luego lista procesos"
        )
        self.window.execute_button.invoke()
        self.wait_until(lambda: self.window.status.get() == "Requiere aprobación")
        self.assertIn("Paso 2.", self.window.prompt["text"])
        self.window.cancel_button.invoke()
        self.wait_until(lambda: self.window.status.get() == "Cancelado")
        self.assertIn("Pasos confirmados 1 de 3", self.window.output.get("1.0", "end"))
        self.tools.set_volume.assert_not_called()
        self.tools.list_processes.assert_not_called()

    def test_desktop_mode_uses_the_existing_cli_audit_path(self):
        with patch("jarvis.desktop.run_window", return_value=0) as launch:
            self.assertEqual(main(["--ventana"], audit_path=self.audit_path), 0)
        self.assertEqual(launch.call_args.args[1].path, self.audit_path)

    def test_window_exit_command_closes_without_running_a_tool(self):
        with patch.object(self.root, "destroy") as destroy:
            self.window.command.set("salir")
            self.window.execute_button.invoke()
        destroy.assert_called_once_with()
        self.assertEqual(self.tools.mock_calls, [])

    def test_tray_enables_hide_handles_reopening_and_releases_the_icon_on_exit(self):
        tray = Mock(available=True)
        with patch("jarvis.desktop.SystemTray", return_value=tray) as install:
            self.window.enable_tray()
        _, show_from_icon, exit_from_menu, tray_changed, *_ = install.call_args.args
        self.assertEqual(str(self.window.hide_button["state"]), "normal")

        with (
            patch.object(self.root, "withdraw") as hide,
            patch.object(self.root, "deiconify") as show,
            patch.object(self.root, "lift") as lift,
        ):
            self.window.hide_button.invoke()
            show_from_icon()
            hide.assert_called_once_with()
            show.assert_called_once_with()
            lift.assert_called_once_with()
            tray_changed(False)
            self.assertEqual(str(self.window.hide_button["state"]), "disabled")
            tray_changed(True)
            self.assertEqual(str(self.window.hide_button["state"]), "normal")

        with patch.object(self.root, "destroy") as destroy:
            exit_from_menu()
        tray.close.assert_called_once_with()
        destroy.assert_called_once_with()

    def test_escape_rejects_a_pending_approval_and_history_recalls_orders(self):
        self.window.command.set("pon el volumen al 35 %")
        self.window.execute_button.invoke()
        self.wait_until(lambda: self.window.status.get() == "Requiere aprobación")
        self.window._escape()
        self.wait_until(lambda: self.window.status.get() == "Detenido")
        self.tools.set_volume.assert_not_called()
        self.assertEqual(self.window.command.get(), "")
        self.window._browse_history(-1)
        self.assertEqual(self.window.command.get(), "pon el volumen al 35 %")
        self.window._browse_history(1)
        self.assertEqual(self.window.command.get(), "")

    def test_hotkey_toggles_the_bar_and_allows_hiding_without_tray(self):
        hotkey = Mock(available=True)
        with patch("jarvis.desktop.GlobalHotkey", return_value=hotkey) as install:
            self.assertTrue(self.window.enable_hotkey())
        toggle = install.call_args.args[1]
        self.assertEqual(str(self.window.hide_button["state"]), "normal")
        with patch.object(self.root, "deiconify") as show:
            toggle()
        show.assert_called_once_with()
        self.window.release_tray()
        hotkey.close.assert_called_once_with()

    def test_busy_hotkey_reports_the_conflict(self):
        with patch("jarvis.desktop.GlobalHotkey", side_effect=OSError):
            self.assertFalse(self.window.enable_hotkey())
        self.assertIn("otro programa ya lo usa", self.window.hint["text"])
        self.assertEqual(str(self.window.hide_button["state"]), "disabled")

    def test_window_stays_usable_when_the_tray_is_unavailable(self):
        with patch("jarvis.desktop.SystemTray", side_effect=OSError) as install:
            self.window.enable_tray()
        install.assert_called_once()
        self.assertEqual(str(self.window.hide_button["state"]), "disabled")
        self.assertEqual(str(self.window.execute_button["state"]), "normal")
