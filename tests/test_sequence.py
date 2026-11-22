import io
import os
import signal
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch

from jarvis.audit import Audit
from jarvis.cli import main
from jarvis.core import SEQUENCE_ACTION, SEQUENCE_SEPARATOR, run_command
from jarvis.tools import LocalTools, SystemUsage, VolumeStatus


class SequenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "auditoria.sqlite3"
        self.audit = Audit(self.path)
        self.tools = Mock(spec=LocalTools)
        self.tools.read_usage.return_value = SystemUsage(12.5, 8 * 1024**3, 3 * 1024**3)
        self.tools.read_volume.return_value = VolumeStatus(35, False)

    def audit_rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT request_id, action, status, step_index FROM events ORDER BY id"
            ).fetchall()

    def test_sequence_numbering_identifies_repeated_actions_and_preserves_order(self):
        result = run_command(
            "consulta CPU y RAM y luego consulta el volumen y luego consulta CPU y RAM",
            self.tools,
            self.audit,
        )
        self.assertEqual(result.code, 0)
        self.assertEqual(
            self.tools.mock_calls, [call.read_usage(), call.read_volume(), call.read_usage()]
        )
        self.assertIn("Pasos confirmados 3 de 3", result.message)
        rows = self.audit_rows()
        self.assertEqual(len({row[0] for row in rows}), 1)
        self.assertEqual(
            [(action, step) for _, action, status, step in rows if status == "completada"],
            [
                ("system.metrics", 1), ("audio.volume", 2), ("system.metrics", 3),
                (SEQUENCE_ACTION, None),
            ],
        )

    def test_preflight_rejects_invalid_later_step_and_sequences_over_the_limit(self):
        bad_order = "consulta CPU y RAM y luego pon el volumen al 101 %"
        long_order = SEQUENCE_SEPARATOR.join(["consulta CPU y RAM"] * 6)
        for text, diagnostic in ((bad_order, "Paso 2."), (long_order, "como máximo 5")):
            with self.subTest(text=text):
                result = run_command(text, self.tools, self.audit)
                self.assertEqual(result.code, 2)
                self.assertIn(diagnostic, result.message)
        self.assertEqual(self.tools.mock_calls, [])
        self.assertTrue(all(row[1] == SEQUENCE_ACTION for row in self.audit_rows()))

    def test_sequence_stops_after_declining_one_step_and_preserves_earlier_results(self):
        approve = Mock(return_value=False)
        result = run_command(
            "consulta CPU y RAM y luego pon el volumen al 35 % y luego lista procesos",
            self.tools,
            self.audit,
            approve=approve,
        )
        self.assertEqual(result.code, 2)
        self.assertIn("Pasos confirmados 1 de 3", result.message)
        self.assertEqual(self.tools.mock_calls, [call.read_usage()])
        request = approve.call_args.args[0]
        self.assertEqual(request.step_index, 2)
        self.assertEqual(request.arguments, {"percent": 35})
        rows = self.audit_rows()
        self.assertEqual({row[0] for row in rows}, {request.request_id})
        self.assertEqual(
            [(action, status, step) for _, action, status, step in rows][-4:],
            [
                ("audio.set_volume", "recibida", 2),
                ("audio.set_volume", "aprobacion_solicitada", 2),
                ("audio.set_volume", "aprobacion_rechazada", 2),
                (SEQUENCE_ACTION, "denegada", None),
            ],
        )

    def test_cancellation_after_step_start_blocks_the_adapter(self):
        audit = Mock(wraps=self.audit)
        cancelled = False

        def record(request_id, action, status, elapsed_ms=None, *, step_index=None):
            nonlocal cancelled
            self.audit.record(request_id, action, status, elapsed_ms, step_index=step_index)
            if step_index == 1 and status == "recibida":
                cancelled = True

        audit.record.side_effect = record
        result = run_command(
            "consulta CPU y RAM y luego lista procesos",
            self.tools,
            audit,
            cancel_requested=lambda: cancelled,
        )
        self.assertEqual(result.code, 130)
        self.assertEqual(self.tools.mock_calls, [])
        self.assertIn("Pasos confirmados 0 de 2", result.message)
        self.assertEqual([(row[2], row[3]) for row in self.audit_rows()][-2:], [
            ("cancelada", 1), ("cancelada", None)
        ])

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_cli_ctrl_c_finishes_active_step_before_stopping_pending_steps(self):
        previous_handler = signal.getsignal(signal.SIGINT)

        def active_step():
            signal.raise_signal(signal.SIGINT)
            return self.tools.read_usage.return_value

        self.tools.read_usage.side_effect = active_step
        output = io.StringIO()
        with patch("jarvis.cli.WindowsTools", return_value=self.tools), redirect_stdout(output):
            code = main(["consulta CPU y RAM y luego lista procesos"], audit_path=self.path)
        self.assertEqual(code, 130)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous_handler)
        self.assertEqual(self.tools.mock_calls, [call.read_usage()])
        self.assertIn("Tarea cancelada. Pasos confirmados 1 de 2.", output.getvalue())
        self.assertEqual([(row[2], row[3]) for row in self.audit_rows()][-2:], [
            ("completada", 1), ("cancelada", None)
        ])

    @unittest.skipUnless(os.name == "nt", "Requiere Windows")
    def test_cli_ctrl_c_interrupts_approval_and_keeps_the_console_available(self):
        input_calls = iter(["consulta CPU y RAM y luego pon el volumen al 35 % y luego lista procesos", "salir"])

        def interrupt_approval(request):
            self.assertEqual(request.step_index, 2)
            signal.raise_signal(signal.SIGINT)
            self.fail("La señal debió interrumpir la espera")

        output = io.StringIO()
        with (
            patch("jarvis.cli.WindowsTools", return_value=self.tools),
            patch("builtins.input", side_effect=input_calls) as prompt,
            patch("jarvis.cli._confirm_approval", side_effect=interrupt_approval),
            redirect_stdout(output),
        ):
            self.assertEqual(main([], audit_path=self.path), 0)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(self.tools.mock_calls, [call.read_usage()])
        self.assertIn("Aprobación interrumpida", output.getvalue())
        self.assertIn("Pasos confirmados 1 de 3", output.getvalue())
        self.assertEqual([(row[2], row[3]) for row in self.audit_rows()][-3:], [
            ("aprobacion_solicitada", 2), ("interrumpida", 2), ("cancelada", None)
        ])

    def test_existing_audit_keeps_its_history_when_two_writers_add_step_numbers(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE events (id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, "
                "occurred_at TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, elapsed_ms REAL)"
            )
            connection.execute(
                "INSERT INTO events (request_id, occurred_at, action, status) "
                "VALUES ('anterior', '2026-11-20', 'apps.open', 'completada')"
            )
        with ThreadPoolExecutor(max_workers=2) as workers:
            writes = [
                workers.submit(
                    Audit(self.path).record, "secuencia", "system.metrics", "completada", step_index=index
                )
                for index in (1, 2)
            ]
            for write in writes:
                write.result()
        rows = self.audit_rows()
        self.assertEqual(rows[0], ("anterior", "apps.open", "completada", None))
        self.assertCountEqual(rows[1:], [
            ("secuencia", "system.metrics", "completada", 1),
            ("secuencia", "system.metrics", "completada", 2),
        ])
