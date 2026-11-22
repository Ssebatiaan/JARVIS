import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.audit import Audit
from jarvis.brain import BrainUnavailable, ClaudeBrain, Decision, find_claude_executable, parse_decision
from jarvis.core import LocalFolder, LocalTools, SystemUsage, run_command
from jarvis.memory import HISTORY_LIMIT, MAX_FACTS, MemoryStore
from jarvis.tools import DEFAULT_PERMISSIONS, MemoryFull, MemoryItem, Permission

FAKE_CLAUDE = r'''
import json, os, sys, time
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
args = sys.argv[1:]
system = args[args.index("--system-prompt") + 1]
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
for line in sys.stdin:
    content = json.loads(line)["message"]["content"]
    if "cuelga" in content:
        time.sleep(30)
    if "error" in content:
        print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True}), flush=True)
        continue
    decision = {
        "tool": "none", "arguments": {}, "remember": None,
        "reply": json.dumps({"pid": os.getpid(), "content": content, "system": system}),
    }
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "```json\n" + json.dumps(decision) + "\n```"}), flush=True)
'''


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.memory = MemoryStore(Path(temporary.name) / "memoria.sqlite3")

    def test_saves_lists_deduplicates_and_forgets_facts(self):
        first = self.memory.add_fact("  Se llama   Sebastián ")
        second = self.memory.add_fact("Prefiere que lo tuteen")
        self.assertEqual(self.memory.add_fact("se llama sebastián"), first)
        self.assertEqual(self.memory.facts(), [MemoryItem(first, "Se llama Sebastián"),
                                               MemoryItem(second, "Prefiere que lo tuteen")])
        self.assertTrue(self.memory.forget_fact(first))
        self.assertFalse(self.memory.forget_fact(first))
        self.assertEqual([fact.id for fact in self.memory.facts()], [second])

    def test_rejects_empty_or_long_facts_and_limits_the_total(self):
        for text in ("", "   ", "x" * 301):
            with self.subTest(length=len(text)), self.assertRaises(ValueError):
                self.memory.add_fact(text)
        for index in range(MAX_FACTS):
            self.memory.add_fact(f"dato {index}")
        with self.assertRaises(MemoryFull):
            self.memory.add_fact("uno más")

    def test_history_keeps_only_the_most_recent_exchanges_in_order(self):
        for index in range(HISTORY_LIMIT + 5):
            self.memory.add_exchange(f"pregunta {index}", f"respuesta {index}")
        recent = self.memory.recent_exchanges(3)
        self.assertEqual([item.user_text for item in recent],
                         [f"pregunta {HISTORY_LIMIT + 2}", f"pregunta {HISTORY_LIMIT + 3}",
                          f"pregunta {HISTORY_LIMIT + 4}"])
        with closing(sqlite3.connect(self.memory.path)) as connection:
            (count,) = connection.execute("SELECT COUNT(*) FROM history").fetchone()
        self.assertEqual(count, HISTORY_LIMIT)

    def test_forget_everything_clears_facts_and_history(self):
        self.memory.add_fact("dato")
        self.memory.add_exchange("hola", "hola")
        self.assertEqual(self.memory.forget_everything(), 1)
        self.assertEqual(self.memory.facts(), [])
        self.assertEqual(self.memory.recent_exchanges(), [])


class ParseDecisionTests(unittest.TestCase):
    def test_accepts_code_fences_and_text_around_the_json(self):
        decision = parse_decision('Claro:\n```json\n{"tool": "audio.set_volume", '
                                  '"arguments": {"percent": 50}, "reply": "Listo", "remember": null}\n```')
        self.assertEqual(decision, Decision("audio.set_volume", {"percent": 50}, "Listo", None))

    def test_none_tool_and_blank_memory_become_none(self):
        decision = parse_decision('{"tool": "none", "arguments": null, "reply": " Hola ", "remember": " "}')
        self.assertEqual(decision, Decision(None, {}, "Hola", None))

    def test_invalid_answers_raise_brain_unavailable(self):
        for raw in ("sin json", "{roto", "[1, 2]"):
            with self.subTest(raw=raw), self.assertRaises(BrainUnavailable):
                parse_decision(raw)

    def test_non_object_arguments_are_kept_so_the_registry_rejects_them(self):
        decision = parse_decision('{"tool": "system.metrics", "arguments": [1], "reply": ""}')
        self.assertEqual(decision.arguments, {"_invalid": [1]})


class FindExecutableTests(unittest.TestCase):
    def test_picks_the_newest_vscode_extension(self):
        with tempfile.TemporaryDirectory() as home:
            for version in ("2.1.9", "2.1.330", "2.1.40"):
                binary = (Path(home) / ".vscode" / "extensions" / f"anthropic.claude-code-{version}-win32-x64"
                          / "resources" / "native-binary" / "claude.exe")
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"")
            with patch.dict(os.environ, {"JARVIS_CLAUDE_PATH": ""}), \
                 patch("jarvis.brain.shutil.which", return_value=None):
                found = find_claude_executable(Path(home))
        self.assertIn("2.1.330", found)

    def test_returns_none_when_claude_is_missing(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"JARVIS_CLAUDE_PATH": ""}), \
             patch("jarvis.brain.shutil.which", return_value=None):
            self.assertIsNone(find_claude_executable(Path(home)))


class FakeBrain:
    def __init__(self, decision=None, error=None):
        self.decision = decision
        self.error = error
        self.asked = []
        self.observed = []
        self.memory_changes = 0

    def decide(self, text):
        self.asked.append(text)
        if self.error:
            raise self.error
        return self.decision

    def observe(self, user_text, outcome, *, source):
        self.observed.append((user_text, source))

    def notify_memory_changed(self):
        self.memory_changes += 1


class CoreWithBrainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "auditoria.sqlite3"
        self.audit = Audit(self.path)
        self.tools = Mock(spec=LocalTools)

    def actions(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT action, status FROM events ORDER BY id").fetchall()

    def test_free_text_runs_the_tool_proposed_by_the_model_and_reports_real_result(self):
        self.tools.read_usage.return_value = SystemUsage(10.0, 8 * 1024**3, 4 * 1024**3)
        brain = FakeBrain(Decision("system.metrics", {}, "Voy a mirar la RAM"))
        result = run_command("¿cuánta RAM estoy usando?", self.tools, self.audit, brain=brain)
        self.assertEqual(result.code, 0)
        self.assertIn("4.00 de 8.00 GiB", result.message)
        self.assertNotIn("Voy a mirar", result.message)
        self.assertEqual(brain.asked, ["¿cuánta RAM estoy usando?"])
        self.assertEqual(brain.observed, [("¿cuánta RAM estoy usando?", "tool")])
        self.assertIn(("assistant.brain", "completada"), self.actions())
        self.assertIn(("system.metrics", "completada"), self.actions())

    def test_known_commands_never_ask_the_model(self):
        self.tools.read_usage.return_value = SystemUsage(10.0, 8 * 1024**3, 4 * 1024**3)
        brain = FakeBrain(Decision(None, {}, "no debería usarse"))
        run_command("consulta CPU y RAM", self.tools, self.audit, brain=brain)
        self.assertEqual(brain.asked, [])
        self.assertEqual(brain.observed, [("consulta CPU y RAM", "direct")])

    def test_conversation_reply_is_shown_without_running_tools(self):
        brain = FakeBrain(Decision(None, {}, "¡Hola, Sebastián!"))
        result = run_command("hola jarvis", self.tools, self.audit, brain=brain)
        self.assertEqual((result.code, result.message), (0, "¡Hola, Sebastián!"))
        self.assertEqual(self.tools.mock_calls, [])

    def test_model_folder_names_are_mapped_to_authorized_folders_only(self):
        result = run_command("ábreme el proyecto", self.tools, self.audit,
                             brain=FakeBrain(Decision("folders.open", {"folder": "proyecto"}, "")))
        self.assertEqual(result.code, 0)
        self.tools.open_folder.assert_called_once_with(LocalFolder.PROJECT)
        result = run_command("abre windows", self.tools, self.audit,
                             brain=FakeBrain(Decision("folders.open", {"folder": "C:/Windows"}, "")))
        self.assertEqual(result.code, 2)
        self.tools.open_folder.assert_called_once()

    def test_unknown_tools_and_invalid_arguments_from_the_model_are_denied(self):
        for decision in (
            Decision("shell.run", {"command": "del *"}, ""),
            Decision("apps.open", {"app_id": "cmd"}, ""),
            Decision("audio.set_volume", {"percent": "50"}, ""),
            Decision("system.metrics", {"_invalid": [1]}, ""),
        ):
            with self.subTest(tool=decision.tool):
                result = run_command("haz algo", self.tools, self.audit, brain=FakeBrain(decision))
                self.assertEqual(result.code, 2)
        self.assertEqual(self.tools.mock_calls, [])

    def test_model_cannot_skip_approval_or_permissions(self):
        brain = FakeBrain(Decision("audio.set_volume", {"percent": 50}, ""))
        result = run_command("baja el volumen a la mitad", self.tools, self.audit, brain=brain)
        self.assertEqual(result.code, 2)
        self.assertIn("aprobación", result.message)
        permissions = DEFAULT_PERMISSIONS - {Permission.SET_VOLUME}
        result = run_command("baja el volumen a la mitad", self.tools, self.audit,
                             brain=brain, permissions=permissions, approve=lambda request: True)
        self.assertIn("Permiso insuficiente", result.message)
        self.tools.set_volume.assert_not_called()

    def test_unavailable_model_degrades_to_help(self):
        brain = FakeBrain(error=BrainUnavailable("caído"))
        result = run_command("hola", self.tools, self.audit, brain=brain)
        self.assertEqual(result.code, 1)
        self.assertIn("no pude consultar a la IA", result.message)
        self.assertIn(("assistant.brain", "fallida"), self.actions())

    def test_model_memories_are_saved_but_secrets_are_refused(self):
        self.tools.save_memory.return_value = 7
        brain = FakeBrain(Decision(None, {}, "Encantado", "Se llama Sebastián y prefiere que lo tuteen"))
        result = run_command("me llamo Sebastián, tutéame", self.tools, self.audit, brain=brain)
        self.tools.save_memory.assert_called_once_with("Se llama Sebastián y prefiere que lo tuteen")
        self.assertIn("Lo recordaré (recuerdo 7)", result.message)
        brain = FakeBrain(Decision(None, {}, "Ok", "Su contraseña del banco es 1234"))
        result = run_command("mi contraseña es 1234", self.tools, self.audit, brain=brain)
        self.assertEqual(self.tools.save_memory.call_count, 1)
        self.assertIn("No guardo contraseñas", result.message)


class MemoryCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.audit = Audit(Path(temporary.name) / "auditoria.sqlite3")
        self.tools = Mock(spec=LocalTools)

    def test_remember_keeps_the_original_text_with_accents(self):
        self.tools.save_memory.return_value = 1
        brain = FakeBrain()
        result = run_command("Recuerda que mi proyecto está en Documentos\\Tesis.", self.tools, self.audit,
                             brain=brain)
        self.assertEqual(result.code, 0)
        self.tools.save_memory.assert_called_once_with("mi proyecto está en Documentos\\Tesis")
        self.assertEqual(brain.asked, [])
        self.assertEqual(brain.memory_changes, 1)

    def test_lists_and_forgets_memories_by_number(self):
        self.tools.list_memories.return_value = [MemoryItem(1, "Se llama Ana")]
        result = run_command("¿Qué recuerdas de mí?", self.tools, self.audit)
        self.assertIn("1. Se llama Ana", result.message)
        self.tools.forget_memory.return_value = False
        result = run_command("olvida el recuerdo 9", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.tools.forget_memory.assert_called_once_with(9)

    def test_forgetting_everything_requires_approval(self):
        result = run_command("olvida todo", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.tools.clear_memories.assert_not_called()
        self.tools.clear_memories.return_value = 3
        result = run_command("olvida todo", self.tools, self.audit, approve=lambda request: True)
        self.assertEqual(result.code, 0)
        self.assertIn("Olvidé 3 recuerdos", result.message)

    def test_full_memory_is_reported(self):
        self.tools.save_memory.side_effect = MemoryFull()
        result = run_command("recuerda que me gusta el café", self.tools, self.audit)
        self.assertEqual(result.code, 2)
        self.assertIn("memoria está llena", result.message)


class ClaudeBrainProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.fake = root / "fake_claude.py"
        self.fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.memory = MemoryStore(root / "memoria.sqlite3")
        self.workdir = root / "cerebro"

    def brain(self, **options):
        brain = ClaudeBrain([sys.executable, "-I", str(self.fake)], self.memory, self.workdir, **options)
        self.addCleanup(brain.close)
        return brain

    @staticmethod
    def reply(decision):
        return json.loads(decision.reply)

    def test_reuses_one_process_and_sends_memory_in_the_system_prompt(self):
        self.memory.add_fact("Se llama Ana")
        self.memory.add_exchange("hola", "Hola, Ana")
        brain = self.brain()
        first = self.reply(brain.decide("hola"))
        second = self.reply(brain.decide("¿qué tal?"))
        self.assertEqual(first["pid"], second["pid"])
        self.assertIn("1. Se llama Ana", first["system"])
        self.assertIn("Usuario: hola\nJARVIS: Hola, Ana", first["system"])
        self.assertEqual(second["content"], "¿qué tal?")

    def test_system_prompt_drops_old_history_to_fit_the_command_line(self):
        for index in range(20):
            self.memory.add_exchange(f"pregunta {index} " + "x" * 450, "respuesta " + "y" * 450)
        for index in range(50):
            self.memory.add_fact(f"dato {index} " + "z" * 280)
        prompt = self.brain()._system_prompt()
        self.assertLessEqual(len(prompt), 24_000)
        self.assertIn("50. dato 49", prompt)
        self.assertIn("pregunta 19", prompt)
        self.assertNotIn("pregunta 10 ", prompt)

    def test_direct_results_and_memory_changes_reach_the_next_request(self):
        brain = self.brain()
        brain.decide("hola")
        brain.observe("consulta el volumen", "Volumen: 40 %.", source="direct")
        self.memory.add_fact("Le gusta el rock")
        brain.notify_memory_changed()
        content = self.reply(brain.decide("súbelo un poco"))["content"]
        self.assertIn("ejecutó directamente «consulta el volumen». Resultado: Volumen: 40 %.", content)
        self.assertIn("Ahora recuerdas: 1. Le gusta el rock", content)
        self.assertTrue(content.endswith("Petición del usuario: súbelo un poco"))
        self.assertEqual(self.reply(brain.decide("otra"))["content"], "otra")

    def test_restarts_after_the_turn_limit_and_closes_when_idle(self):
        brain = self.brain(max_turns=2, idle_seconds=0.5)
        pids = [self.reply(brain.decide(f"orden {index}"))["pid"] for index in range(3)]
        self.assertEqual(pids[0], pids[1])
        self.assertNotEqual(pids[1], pids[2])
        deadline = time.monotonic() + 5
        while brain.running and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(brain.running)

    def test_hung_or_failed_process_raises_after_one_retry(self):
        brain = self.brain(timeout=0.5)
        with self.assertRaises(BrainUnavailable):
            brain.decide("cuelga")
        self.assertFalse(brain.running)
        with self.assertRaises(BrainUnavailable):
            brain.decide("error")
        self.assertEqual(self.reply(brain.decide("hola"))["content"], "hola")


if __name__ == "__main__":
    unittest.main()
