import ctypes as ct
import io
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.audit import Audit
from jarvis.cli import main
from jarvis.core import (
    AudioOutputUnavailable,
    FolderRedirected,
    LocalFolder,
    LocalTools,
    ProcessInfo,
    SystemUsage,
    VolumeStatus,
    run_command,
)
from jarvis.windows import WindowsTools
from jarvis.apps import AppCatalog, InstalledApp
from jarvis.tools import DEFAULT_PERMISSIONS, TOOLS, Permission, TargetNotFound, ToolId


class DemoTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "auditoria.sqlite3"
        self.audit = Audit(self.path)
        self.tools = Mock(spec=LocalTools)

    def audit_rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT request_id, action, status, elapsed_ms FROM events ORDER BY id"
            ).fetchall()

    def test_opens_an_installed_application_once_and_records_result(self):
        self.tools.launch_app.return_value = "Bloc de notas"
        result = run_command("  Abre   el Blóc de Notas  ", self.tools, self.audit)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.message, "Abriendo Bloc de notas.")
        self.tools.launch_app.assert_called_once_with("el bloc de notas")
        received, completed = self.audit_rows()
        self.assertEqual(received[0], completed[0])
        self.assertEqual(received[1:3], ("apps.open", "recibida"))
        self.assertEqual(completed[1:3], ("apps.open", "solicitada"))
        self.assertGreaterEqual(completed[3], 0)

    def test_paths_and_chained_commands_are_never_launched(self):
        for text in ("abre C:/Windows/notepad.exe", "abre notepad & del archivo", "abre ..\\evil"):
            with self.subTest(text=text):
                result = run_command(text, self.tools, self.audit)
                self.assertEqual(result.code, 2)
                self.assertIn("sin rutas", result.message)
        self.tools.launch_app.assert_not_called()
        self.assertEqual([row[2] for row in self.audit_rows()][1::2], ["denegada"] * 3)

    def test_unknown_application_is_reported_with_suggestions(self):
        self.tools.launch_app.side_effect = TargetNotFound("firefoz", ["Firefox"])
        result = run_command("abre firefoz", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.assertIn("No encontré «firefoz»", result.message)
        self.assertIn("¿Quizás «Firefox»?", result.message)
        self.assertEqual(self.audit_rows()[-1][1:3], ("apps.open", "no_encontrada"))

    def test_opens_only_named_project_folders_and_audits_the_request(self):
        for command, folder in (
            ("abre la carpeta del proyecto", LocalFolder.PROJECT),
            ("Abrir carpeta de Documentación", LocalFolder.DOCUMENTATION),
        ):
            with self.subTest(command=command):
                self.tools.open_folder.reset_mock()
                result = run_command(command, self.tools, self.audit)
                self.assertEqual(result.code, 0)
                self.assertIn(folder.value, result.message)
                self.tools.open_folder.assert_called_once_with(folder)
        self.assertEqual(self.audit_rows()[-1][1:3], ("folders.open", "solicitada"))

    def test_rejects_paths_and_extra_folder_arguments_without_opening_anything(self):
        for target in ("C:/Windows", "../docs", "docs/..", "docs y abre notepad", ""):
            with self.subTest(target=target):
                result = run_command(f"abre la carpeta {target}", self.tools, self.audit)
                self.assertEqual(result.code, 2)
                self.assertIn("Carpeta no autorizada", result.message)
        self.assertEqual(self.tools.mock_calls, [])
        self.assertEqual(self.audit_rows()[-1][1:3], ("folders.open", "denegada"))

    def test_audits_redirected_folder_as_denied(self):
        self.tools.open_folder.side_effect = FolderRedirected()
        result = run_command("abre carpeta docs", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.assertIn("carpeta está redirigida", result.message)
        self.assertEqual(self.audit_rows()[-1][1:3], ("folders.open", "denegada"))

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_windows_sends_only_the_absolute_named_folder_to_explorer(self):
        project = self.path.parent.resolve()
        docs = project / "docs"
        docs.mkdir()
        with patch("jarvis.windows.PROJECT_ROOT", project), patch.object(os, "startfile") as launch:
            tools = WindowsTools()
            tools.open_folder(LocalFolder.PROJECT)
            tools.open_folder(LocalFolder.DOCUMENTATION)
        self.assertEqual([invocation.args for invocation in launch.call_args_list], [
            (project, "explore"), (docs, "explore")
        ])

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_windows_rejects_redirected_folder_before_calling_explorer(self):
        attributes = SimpleNamespace(st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        with (
            patch("jarvis.windows.PROJECT_ROOT", self.path.parent),
            patch.object(Path, "lstat", return_value=attributes),
            patch.object(os, "startfile") as launch,
            self.assertRaises(FolderRedirected),
        ):
            WindowsTools().open_folder(LocalFolder.DOCUMENTATION)
        launch.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_windows_rejects_files_in_place_of_authorized_folders(self):
        project = self.path.parent
        (project / "docs").touch()
        with (
            patch("jarvis.windows.PROJECT_ROOT", project),
            patch.object(os, "startfile") as launch,
            self.assertRaises(NotADirectoryError),
        ):
            WindowsTools().open_folder(LocalFolder.DOCUMENTATION)
        launch.assert_not_called()

    def test_reports_metrics_and_unknown_commands(self):
        self.tools.read_usage.return_value = SystemUsage(12.5, 8 * 1024**3, 3 * 1024**3)
        result = run_command("consulta CPU y RAM", self.tools, self.audit)
        self.assertEqual(result.code, 0)
        self.assertIn("12.5 %", result.message)
        self.assertIn("5.00 de 8.00 GiB", result.message)
        result = run_command("borrar carpeta", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.tools.launch_app.assert_not_called()
        self.tools.read_usage.assert_called_once_with()

    def test_registry_declares_every_tool_and_its_own_permission(self):
        self.assertEqual(set(TOOLS), set(ToolId))
        self.assertEqual(DEFAULT_PERMISSIONS, {tool.permission for tool in TOOLS.values()})
        self.assertEqual(len(DEFAULT_PERMISSIONS), len(TOOLS))

    def test_registry_validates_exact_argument_names_types_and_limits(self):
        volume = TOOLS[ToolId.SET_VOLUME]
        self.assertTrue(volume.accepts_arguments({"percent": 0}))
        self.assertTrue(volume.accepts_arguments({"percent": 100}))
        for arguments in ({}, {"percent": True}, {"percent": "35"}, {"percent": 101},
                          {"percent": 35, "extra": ""}):
            with self.subTest(arguments=arguments):
                self.assertFalse(volume.accepts_arguments(arguments))
        folder = TOOLS[ToolId.OPEN_FOLDER]
        self.assertTrue(folder.accepts_arguments({"folder": LocalFolder.DOCUMENTATION}))
        self.assertFalse(folder.accepts_arguments({"folder": "docs"}))
        self.assertFalse(TOOLS[ToolId.SYSTEM_METRICS].accepts_arguments({"extra": ""}))

    def test_each_tool_without_permission_is_denied_and_audited(self):
        commands = (
            "abre el Bloc de notas", "abre la carpeta del proyecto", "consulta CPU y RAM",
            "consulta el volumen", "pon el volumen al 35 %", "lista procesos",
            "recuerda que me llamo Ana", "qué recuerdas", "olvida el recuerdo 1", "olvida todo",
            "cambia a Discord", "cierra Discord", "minimiza todo", "pausa", "abre YouTube",
            "busca gatos en Google", "reproduce lofi en YouTube", "qué hora es",
        )
        for command in commands:
            with self.subTest(command=command):
                result = run_command(command, self.tools, self.audit, permissions=frozenset())
                self.assertEqual(result.code, 2)
                self.assertIn("Permiso insuficiente", result.message)
        self.assertEqual(self.tools.mock_calls, [])
        outcomes = [row[1:3] for row in self.audit_rows()][1::2]
        self.assertCountEqual(outcomes, [(tool_id.value, "denegada") for tool_id in TOOLS])

    def test_read_permission_is_distinct_from_volume_write_permission(self):
        permissions = frozenset({Permission.READ_VOLUME})
        approve = Mock(return_value=True)
        self.tools.read_volume.return_value = VolumeStatus(35, False)
        read = run_command("consulta el volumen", self.tools, self.audit, permissions=permissions)
        write = run_command(
            "pon el volumen al 20 %", self.tools, self.audit, permissions=permissions, approve=approve
        )
        self.assertEqual(read.code, 0)
        self.assertEqual(write.code, 2)
        self.tools.read_volume.assert_called_once_with()
        self.tools.set_volume.assert_not_called()
        approve.assert_not_called()

    def test_reports_os_error_without_logging_sensitive_text(self):
        self.tools.launch_app.side_effect = OSError(5, "dato sensible")
        result = run_command("abre notepad", self.tools, self.audit)
        self.assertEqual(result.code, 1)
        self.assertIn("código 5", result.message)
        self.assertNotIn("dato sensible", result.message)
        self.assertEqual(self.audit_rows()[-1][2], "fallida")

    def test_volume_reports_output_level_and_mute_and_audits_the_read(self):
        self.tools.read_volume.side_effect = [VolumeStatus(42, True), VolumeStatus(0, False)]
        muted = run_command("consulta el volumen", self.tools, self.audit)
        unmuted = run_command("consulta volumen", self.tools, self.audit)
        self.assertEqual(muted.code, 0)
        self.assertIn("42 % (silenciado)", muted.message)
        self.assertEqual(unmuted.code, 0)
        self.assertIn("0 %.", unmuted.message)
        self.assertEqual(self.tools.read_volume.call_count, 2)
        self.assertEqual(self.audit_rows()[-1][1:3], ("audio.volume", "completada"))

    def test_volume_reports_unavailable_output(self):
        self.tools.read_volume.side_effect = AudioOutputUnavailable()
        result = run_command("consulta el volumen", self.tools, self.audit)
        self.assertEqual(result.code, 1)
        self.assertIn("no tiene una salida de audio predeterminada", result.message)
        self.assertEqual(self.audit_rows()[-1][1:3], ("audio.volume", "fallida"))

    def test_volume_changes_do_not_call_local_tools(self):
        result = run_command("sube el volumen", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.assertEqual(self.tools.mock_calls, [])

    def test_explicit_volume_setting_accepts_limits_and_reports_mute(self):
        for order, percent in (
            ("pon el volumen al 0 %", 0),
            ("ajusta el volumen al 35%", 35),
            ("pon el volumen al 100", 100),
        ):
            with self.subTest(order=order):
                self.tools.set_volume.reset_mock()
                self.tools.set_volume.return_value = VolumeStatus(percent, True)
                result = run_command(order, self.tools, self.audit, approve=lambda request: True)
                self.assertEqual(result.code, 0)
                self.assertIn(f"{percent} % (silenciado)", result.message)
                self.tools.set_volume.assert_called_once_with(percent)
                self.tools.read_volume.assert_not_called()
        self.assertEqual(self.audit_rows()[-1][1:3], ("audio.set_volume", "completada"))

    def test_invalid_volume_setting_never_calls_the_adapter(self):
        approve = Mock(return_value=True)
        for value in ("-1", "101", "35.5", "35 % y abre notepad", "50 %%", "alto"):
            with self.subTest(value=value):
                result = run_command(
                    f"pon el volumen al {value}", self.tools, self.audit, approve=approve
                )
                self.assertEqual(result.code, 2)
                self.assertIn("entero de 0 a 100", result.message)
        self.assertEqual(self.tools.mock_calls, [])
        approve.assert_not_called()
        self.assertEqual(self.audit_rows()[-1][1:3], ("audio.set_volume", "denegada"))

    def test_volume_setting_reports_different_level_from_windows(self):
        self.tools.set_volume.return_value = VolumeStatus(34, False)
        result = run_command(
            "pon el volumen al 35 %", self.tools, self.audit, approve=lambda request: True
        )
        self.assertEqual(result.code, 1)
        self.assertIn("Windows reportó 34 %", result.message)
        self.assertEqual(self.audit_rows()[-1][1:3], ("audio.set_volume", "nivel_distinto"))

    def test_volume_setting_reports_unverified_change_without_retry(self):
        self.tools.set_volume.side_effect = OSError(5, "detalle interno")
        result = run_command(
            "pon el volumen al 35 %", self.tools, self.audit, approve=lambda request: True
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(result.message, "Windows no pudo confirmar el ajuste de volumen (código 5).")
        self.tools.set_volume.assert_called_once_with(35)
        self.assertEqual(self.audit_rows()[-1][1:3], ("audio.set_volume", "fallida"))

    def test_approval_is_bound_to_immutable_arguments_and_recorded_before_execution(self):
        def approve(request):
            self.assertEqual(request.tool_id, ToolId.SET_VOLUME)
            self.assertEqual(request.arguments, {"percent": 35})
            self.assertIn("35 %", request.summary)
            with self.assertRaises(TypeError):
                request.arguments["percent"] = 99
            self.tools.set_volume.assert_not_called()
            self.assertEqual(
                [(row[0], row[2]) for row in self.audit_rows()],
                [(request.request_id, "recibida"), (request.request_id, "aprobacion_solicitada")],
            )
            return True

        def set_volume(percent):
            self.assertEqual(self.audit_rows()[-1][2], "aprobada")
            return VolumeStatus(percent, False)

        self.tools.set_volume.side_effect = set_volume
        result = run_command("pon el volumen al 35 %", self.tools, self.audit, approve=approve)
        self.assertEqual(result.code, 0)
        self.tools.set_volume.assert_called_once_with(35)
        self.assertEqual(
            [row[2] for row in self.audit_rows()],
            ["recibida", "aprobacion_solicitada", "aprobada", "completada"],
        )

    def test_each_volume_change_requires_a_new_approval(self):
        approve = Mock(side_effect=[True, False])
        self.tools.set_volume.return_value = VolumeStatus(35, False)
        first = run_command("pon el volumen al 35 %", self.tools, self.audit, approve=approve)
        second = run_command("pon el volumen al 35 %", self.tools, self.audit, approve=approve)
        self.assertEqual(first.code, 0)
        self.assertEqual(second.code, 2)
        self.tools.set_volume.assert_called_once_with(35)
        self.assertEqual(approve.call_count, 2)
        self.assertEqual(self.audit_rows()[-1][2], "aprobacion_rechazada")

    def test_late_approval_does_not_execute(self):
        with patch("jarvis.core.monotonic", side_effect=[100, 161]):
            result = run_command(
                "pon el volumen al 35 %", self.tools, self.audit, approve=lambda request: True
            )
        self.assertEqual(result.code, 2)
        self.assertIn("venció", result.message)
        self.tools.set_volume.assert_not_called()
        self.assertEqual(self.audit_rows()[-1][2], "aprobacion_vencida")

    def test_missing_approval_handler_denies_the_action(self):
        result = run_command("pon el volumen al 35 %", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.tools.set_volume.assert_not_called()
        self.assertEqual(self.audit_rows()[-1][2], "aprobacion_no_disponible")

    def test_interruption_during_approval_denies_the_action(self):
        approve = Mock(side_effect=KeyboardInterrupt())
        result = run_command("pon el volumen al 35 %", self.tools, self.audit, approve=approve)
        self.assertEqual(result.code, 130)
        self.tools.set_volume.assert_not_called()
        self.assertEqual(self.audit_rows()[-1][2], "interrumpida")

    def test_audit_failure_during_approval_prevents_execution(self):
        audit = Mock(spec=Audit)
        audit.record.side_effect = [None, None, OSError(5, "sin escritura"), None]
        result = run_command(
            "pon el volumen al 35 %", self.tools, audit, approve=lambda request: True
        )
        self.assertEqual(result.code, 1)
        self.assertIn("No se pudo guardar la aprobación", result.message)
        self.tools.set_volume.assert_not_called()
        self.assertEqual(audit.record.call_args.args[2], "fallida")

    def test_approval_does_not_override_a_revoked_permission(self):
        permissions = {Permission.SET_VOLUME}

        def approve(request):
            permissions.clear()
            return True

        result = run_command(
            "pon el volumen al 35 %", self.tools, self.audit, permissions=permissions, approve=approve
        )
        self.assertEqual(result.code, 2)
        self.tools.set_volume.assert_not_called()
        self.assertEqual(self.audit_rows()[-1][2], "denegada")

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_cli_accepts_explicit_yes_and_presents_the_exact_percentage(self):
        self.tools.set_volume.return_value = VolumeStatus(35, False)
        with (
            patch("jarvis.cli.WindowsTools", return_value=self.tools),
            patch("builtins.input", return_value="  SÍ  ") as prompt,
            patch("sys.stdin", Mock(isatty=Mock(return_value=True))),
            redirect_stdout(Mock(isatty=Mock(return_value=True))),
        ):
            self.assertEqual(main(["pon el volumen al 35 %"], audit_path=self.path), 0)
        self.assertIn("35 %", prompt.call_args.args[0])
        self.tools.set_volume.assert_called_once_with(35)

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_cli_rejects_empty_answer_and_handles_eof(self):
        for response, expected in (("", "aprobacion_rechazada"), (EOFError(), "aprobacion_no_disponible")):
            with (
                self.subTest(response=response),
                patch("jarvis.cli.WindowsTools", return_value=self.tools),
                patch("builtins.input", side_effect=[response]),
                patch("sys.stdin", Mock(isatty=Mock(return_value=True))),
                redirect_stdout(Mock(isatty=Mock(return_value=True))),
            ):
                self.assertEqual(main(["pon el volumen al 35 %"], audit_path=self.path), 2)
                self.assertEqual(self.audit_rows()[-1][2], expected)
        self.tools.set_volume.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_cli_denies_approval_from_redirected_input(self):
        with (
            patch("jarvis.cli.WindowsTools", return_value=self.tools),
            patch("sys.stdin", io.StringIO("si\n")),
            patch("builtins.input") as prompt,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["pon el volumen al 35 %"], audit_path=self.path), 2)
        prompt.assert_not_called()
        self.tools.set_volume.assert_not_called()
        self.assertEqual(self.audit_rows()[-1][2], "aprobacion_no_disponible")

    def test_process_list_sorts_names_and_pids_and_audits_the_read(self):
        self.tools.list_processes.return_value = [
            ProcessInfo(50, "Zeta.exe"),
            ProcessInfo(20, "Árbol.exe"),
            ProcessInfo(10, "Árbol.exe"),
        ]
        result = run_command("  Lista   procesos ", self.tools, self.audit)
        self.assertEqual(result.code, 0)
        rows = result.message.splitlines()
        self.assertEqual([row.split() for row in rows[2:-1]], [
            ["10", "Árbol.exe"], ["20", "Árbol.exe"], ["50", "Zeta.exe"],
        ])
        self.assertEqual(rows[-1], "Total de procesos: 3.")
        self.tools.list_processes.assert_called_once_with()
        self.assertEqual(self.audit_rows()[-1][1:3], ("system.processes", "completada"))

    def test_process_list_reports_windows_failure(self):
        self.tools.list_processes.side_effect = OSError(5, "detalle interno")
        result = run_command("lista procesos", self.tools, self.audit)
        self.assertEqual(result.code, 1)
        self.assertEqual(result.message, "Windows no pudo listar los procesos (código 5).")
        self.assertEqual(self.audit_rows()[-1][1:3], ("system.processes", "fallida"))

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_windows_process_snapshot_contains_the_current_process(self):
        processes = WindowsTools().list_processes()
        process = next(item for item in processes if item.pid == os.getpid())
        self.assertTrue(process.name)

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_windows_releases_the_snapshot_when_enumeration_fails(self):
        tools = WindowsTools()

        def fail_read(handle, entry):
            ct.set_last_error(5)
            return False

        with (
            patch.object(tools, "_process_snapshot", return_value=42),
            patch.object(tools, "_process_first", side_effect=fail_read),
            patch.object(tools, "_close_handle") as close_handle,
            self.assertRaises(OSError) as error,
        ):
            tools.list_processes()
        self.assertEqual(error.exception.winerror, 5)
        close_handle.assert_called_once_with(42)

    def test_interruption_is_recorded(self):
        self.tools.read_usage.side_effect = KeyboardInterrupt()
        result = run_command("consulta CPU y RAM", self.tools, self.audit)
        self.assertEqual(result.code, 130)
        self.assertEqual(self.audit_rows()[-1][2], "interrumpida")

    def test_failed_initial_audit_prevents_execution(self):
        self.path.mkdir()
        result = run_command("abre notepad", self.tools, self.audit)
        self.assertEqual(result.code, 1)
        self.assertIn("Orden detenida", result.message)
        self.tools.launch_app.assert_not_called()

    def test_failed_final_audit_preserves_known_action_result(self):
        self.tools.launch_app.return_value = "Bloc de notas"
        audit = Mock(spec=Audit)
        audit.record.side_effect = [None, OSError(5, "sin escritura")]
        result = run_command("abre notepad", self.tools, audit)
        self.assertEqual(result.code, 1)
        self.assertIn("Abriendo Bloc de notas", result.message)
        self.assertIn("No se pudo guardar", result.message)
        self.tools.launch_app.assert_called_once_with("notepad")

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_windows_reads_real_metrics_and_launches_only_start_menu_apps(self):
        tools = WindowsTools()
        usage = tools.read_usage()
        self.assertLessEqual(0, usage.cpu_percent)
        self.assertLessEqual(usage.cpu_percent, 100)
        self.assertLessEqual(0, usage.ram_available_bytes)
        self.assertLessEqual(usage.ram_available_bytes, usage.ram_total_bytes)
        self.assertGreater(usage.ram_total_bytes, 0)

        tools._apps = AppCatalog(loader=lambda: [
            InstalledApp("Bloc de notas", "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
            InstalledApp("Uninstall Zoom", "Microsoft.AutoGenerated.{2F48}"),
        ])
        with patch("jarvis.apps.subprocess.Popen") as launch:
            self.assertEqual(tools.launch_app("el bloc de notas"), "Bloc de notas")
            with self.assertRaises(TargetNotFound):
                tools.launch_app("uninstall zoom")
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0],
                         ["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"])
        with self.assertRaises(PermissionError):
            tools.open_url("file:///C:/Windows/System32")

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_console_continues_after_denial_and_exits(self):
        output = io.StringIO()
        with patch("builtins.input", side_effect=["abre C:/Windows/notepad.exe", "consulta CPU y RAM", "salir"]):
            with redirect_stdout(output):
                self.assertEqual(main(["--sin-ia"], audit_path=self.path), 0)
        self.assertIn("Me temo que no ha sido posible, señor", output.getvalue())
        self.assertIn("sin rutas", output.getvalue())
        self.assertIn("GiB", output.getvalue())
