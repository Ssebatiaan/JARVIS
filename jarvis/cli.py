import argparse
import os
import signal
import sys
from pathlib import Path
from threading import current_thread, main_thread

from .audit import Audit
from .brain import ClaudeBrain, find_claude_executable
from .core import HELP, SEQUENCE_SEPARATOR, ApprovalRequest, Brain, Result, normalize, run_command
from .memory import MemoryStore
from .tools import LocalTools
from .windows import WindowsTools


def _confirm_approval(request: ApprovalRequest) -> bool:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise EOFError
    step = f"Paso {request.step_index}. " if request.step_index is not None else ""
    answer = input(
        f"{step}{request.summary}\nEscribe «sí» para aprobar en {request.timeout_seconds} segundos. "
    )
    return normalize(answer) == "si"


def _run_console_command(
    text: str, tools: LocalTools, audit: Audit, brain: Brain | None = None
) -> Result:
    if SEQUENCE_SEPARATOR not in normalize(text) or current_thread() is not main_thread():
        return run_command(text, tools, audit, approve=_confirm_approval, brain=brain)

    cancelled = False
    awaiting_approval = False

    def interrupt(signum, frame):
        nonlocal cancelled
        cancelled = True
        if awaiting_approval:
            raise KeyboardInterrupt

    def approve(request: ApprovalRequest) -> bool:
        nonlocal awaiting_approval
        try:
            awaiting_approval = True
            if cancelled:
                raise KeyboardInterrupt
            return _confirm_approval(request)
        finally:
            awaiting_approval = False

    previous_handler = signal.signal(signal.SIGINT, interrupt)
    try:
        print("Tarea en curso. Pulsa Ctrl+C para cancelar los pasos pendientes.")
        return run_command(text, tools, audit, approve=approve, cancel_requested=lambda: cancelled)
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def _create_brain(memory: MemoryStore, data_dir: Path) -> ClaudeBrain | None:
    executable = find_claude_executable()
    if executable is None:
        return None
    return ClaudeBrain([executable], memory, data_dir / "cerebro")


def main(argv: list[str] | None = None, *, audit_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="JARVIS, asistente local para Windows.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("orden", nargs="?", help="Orden entre comillas. Sin orden abre la consola.")
    mode.add_argument("--ventana", action="store_true", help="Abre la ventana de escritorio.")
    parser.add_argument("--sin-ia", action="store_true", help="Usa solo órdenes fijas, sin IA.")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("JARVIS requiere Windows.")
        return 1

    if audit_path is None:
        app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        audit_path = app_data / "JARVIS" / "auditoria.sqlite3"
    audit = Audit(audit_path)
    memory = MemoryStore(audit_path.parent / "memoria.sqlite3")

    try:
        tools = WindowsTools(memory)
    except OSError:
        print("No se pudo inicializar el acceso a las herramientas de Windows.")
        return 1

    brain = None if args.sin_ia else _create_brain(memory, audit_path.parent)
    try:
        return _run_mode(args, tools, audit, brain)
    finally:
        if brain is not None:
            brain.close()


def _run_mode(args: argparse.Namespace, tools: LocalTools, audit: Audit, brain: ClaudeBrain | None) -> int:
    if args.ventana:
        try:
            from .desktop import run_window
        except ImportError:
            print("La ventana requiere una instalación de Python con Tkinter.")
            return 1
        return run_window(tools, audit, brain)

    if args.orden is not None:
        result = _run_console_command(args.orden, tools, audit, brain)
        print(result.message)
        return result.code

    status = "IA conectada: puedes hablarme con naturalidad." if brain else "IA desactivada."
    print(f"JARVIS listo. {status} {HELP}")
    while True:
        try:
            text = input("Tú> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        command = normalize(text)
        if command == "salir":
            return 0
        if command:
            print(_run_console_command(text, tools, audit, brain).message)
