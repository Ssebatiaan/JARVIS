import tkinter as tk
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from threading import Event, Thread
from time import monotonic
from tkinter import ttk

from .audit import Audit
from .core import ApprovalRequest, Brain, Result, normalize, run_command
from .tools import LocalTools
from .tray import SystemTray

BACKGROUND = "#111922"
PANEL = "#17232f"
OUTLINE = "#38495c"
TEXT = "#edf3f8"
MUTED = "#aebdcc"
ACCENT = "#a9e578"
APPROVAL_BACKGROUND = "#2b241b"
APPROVAL_TEXT = "#ffe0ab"
FONT = "Segoe UI"


@dataclass
class _Approval:
    request: ApprovalRequest
    ready: Event = field(default_factory=Event)
    accepted: bool = False


@dataclass
class _Task:
    cancelled: Event = field(default_factory=Event)
    events: SimpleQueue[_Approval | Result] = field(default_factory=SimpleQueue)

    def approve(self, request: ApprovalRequest) -> bool:
        approval = _Approval(request)
        deadline = monotonic() + request.timeout_seconds
        self.events.put(approval)
        while not self.cancelled.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return False
            if approval.ready.wait(min(remaining, 0.05)):
                if self.cancelled.is_set():
                    break
                return approval.accepted
        raise KeyboardInterrupt


def _run_task(
    text: str, tools: LocalTools, audit: Audit, task: _Task, brain: Brain | None = None
) -> None:
    try:
        result = run_command(
            text, tools, audit, approve=task.approve, cancel_requested=task.cancelled.is_set,
            brain=brain,
        )
    except Exception:
        result = Result(1, "La orden se detuvo. Revisa la auditoría antes de repetirla.")
    task.events.put(result)


class DesktopWindow:
    def __init__(self, root: tk.Tk, tools: LocalTools, audit: Audit, brain: Brain | None = None):
        self.root = root
        self.tools = tools
        self.audit = audit
        self.brain = brain
        self._task: _Task | None = None
        self._approval: _Approval | None = None
        self._tray: SystemTray | None = None
        self._closing = False
        self.command = tk.StringVar(root)
        self.status = tk.StringVar(root, value="Listo")

        root.title("JARVIS")
        root.geometry("840x600")
        root.minsize(620, 460)
        root.configure(background=BACKGROUND)
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        self._configure_styles()
        self._build()
        root.bind("<Escape>", lambda event: self.cancel())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.entry.focus_set()

    def _configure_styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Jarvis.TButton", background=PANEL, foreground=TEXT, bordercolor=OUTLINE,
            lightcolor=PANEL, darkcolor=PANEL, focuscolor=ACCENT, font=(FONT, 10),
            padding=(12, 7),
        )
        style.map(
            "Jarvis.TButton",
            background=[("active", OUTLINE)],
            foreground=[("disabled", "#768696")],
        )
        style.configure(
            "Accent.Jarvis.TButton", background=ACCENT, foreground="#102010",
            bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT,
            focuscolor="#102010", font=(FONT, 10, "bold"), padding=(16, 7),
        )
        style.map(
            "Accent.Jarvis.TButton",
            background=[("disabled", OUTLINE), ("active", "#c3f49d")],
            foreground=[("disabled", MUTED)],
        )

    def _build(self):
        content = tk.Frame(self.root, background=BACKGROUND, padx=26, pady=22)
        content.grid(sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(3, weight=1)

        header = tk.Frame(content, background=BACKGROUND)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        tk.Label(
            header, text="JARVIS", background=BACKGROUND, foreground=TEXT,
            font=(FONT, 26, "bold"),
        ).pack(side="left")
        tk.Label(
            header, textvariable=self.status, background=PANEL, foreground=ACCENT,
            font=(FONT, 10, "bold"), padx=11, pady=6,
        ).pack(side="right")
        self.hide_button = ttk.Button(
            header, text="Ocultar", command=self.hide_to_tray, style="Jarvis.TButton", state="disabled",
        )
        self.hide_button.pack(side="right", padx=(0, 9))

        form = tk.Frame(content, background=BACKGROUND)
        form.grid(row=1, column=0, sticky="ew", pady=(0, 15))
        form.columnconfigure(0, weight=1)
        tk.Label(
            form, text="Orden", background=BACKGROUND, foreground=TEXT, font=(FONT, 11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 7))
        self.entry = tk.Entry(
            form, textvariable=self.command, background=PANEL, foreground=TEXT,
            disabledbackground=PANEL, disabledforeground=MUTED, insertbackground=TEXT,
            highlightthickness=1, highlightbackground=OUTLINE, highlightcolor=ACCENT,
            font=(FONT, 13), relief="flat",
        )
        self.entry.grid(row=1, column=0, sticky="nsew", ipady=7)
        self.entry.bind("<Return>", lambda event: self.submit())
        self.execute_button = ttk.Button(
            form, text="Ejecutar", command=self.submit, style="Accent.Jarvis.TButton",
        )
        self.execute_button.grid(row=1, column=1, padx=(9, 0), sticky="ns")
        self.cancel_button = ttk.Button(
            form, text="Cancelar", command=self.cancel, style="Jarvis.TButton", state="disabled",
        )
        self.cancel_button.grid(row=1, column=2, padx=(7, 0), sticky="ns")

        shortcuts = tk.Frame(form, background=BACKGROUND)
        shortcuts.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.shortcuts = []
        for label, command in (
            ("CPU y RAM", "consulta CPU y RAM"),
            ("Volumen", "consulta el volumen"),
            ("Memoria", "qué recuerdas"),
            ("Ayuda", "ayuda"),
        ):
            button = ttk.Button(
                shortcuts, text=label, command=lambda value=command: self._run_example(value),
                style="Jarvis.TButton",
            )
            button.pack(side="left", padx=(0, 7))
            self.shortcuts.append(button)

        self.approval_panel = tk.Frame(
            content, background=APPROVAL_BACKGROUND, padx=13, pady=10,
        )
        self.approval_panel.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        self.prompt = tk.Label(
            self.approval_panel, background=APPROVAL_BACKGROUND, foreground=APPROVAL_TEXT,
            font=(FONT, 11, "bold"), anchor="w", justify="left", wraplength=520,
        )
        self.prompt.pack(fill="x", pady=(0, 8))
        self.approve_button = ttk.Button(
            self.approval_panel, text="Aprobar", command=lambda: self._answer(True),
            style="Accent.Jarvis.TButton",
        )
        self.approve_button.pack(side="right")
        self.reject_button = ttk.Button(
            self.approval_panel, text="Rechazar", command=lambda: self._answer(False),
            style="Jarvis.TButton",
        )
        self.reject_button.pack(side="right", padx=(0, 7))
        self.approval_panel.grid_remove()

        results = tk.Frame(content, background=BACKGROUND)
        results.grid(row=3, column=0, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(1, weight=1)
        tk.Label(
            results, text="Resultado", background=BACKGROUND, foreground=TEXT,
            font=(FONT, 11, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 7))
        self.output = tk.Text(
            results, background=PANEL, foreground=TEXT, selectbackground=OUTLINE,
            selectforeground=TEXT, highlightthickness=1, highlightbackground=OUTLINE,
            highlightcolor=ACCENT, font=("Consolas", 11), wrap="word", padx=13, pady=12,
            relief="flat", state="disabled",
        )
        self.output.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(results, orient="vertical", command=self.output.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.output.configure(yscrollcommand=scrollbar.set)

    def _run_example(self, text: str) -> None:
        self.command.set(text)
        self.submit()

    def enable_tray(self) -> None:
        try:
            self._tray = SystemTray(self.root, self.show_from_tray, self.close, self._tray_changed)
        except OSError:
            return
        self.hide_button.configure(state="normal")

    def hide_to_tray(self) -> None:
        if self._tray is not None and self._tray.available:
            self.root.withdraw()

    def show_from_tray(self) -> None:
        if not self._closing:
            self.root.deiconify()
            self.root.lift()

    def _tray_changed(self, available: bool) -> None:
        self.hide_button.configure(state="normal" if available else "disabled")
        if not available and not self._closing and self.root.state() == "withdrawn":
            self.root.deiconify()

    def release_tray(self) -> None:
        if self._tray is not None:
            self._tray.close()
            self._tray = None

    def _set_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")
        self.output.yview_moveto(0)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.entry.configure(state=state)
        self.execute_button.configure(state=state)
        for button in self.shortcuts:
            button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    def submit(self) -> None:
        text = self.command.get().strip()
        if not text or self._task is not None or self._closing:
            return
        if normalize(text) == "salir":
            self.close()
            return
        task = _Task()
        self._task = task
        self._set_busy(True)
        self._set_output("")
        self.status.set("En ejecución")
        worker = Thread(target=_run_task, args=(text, self.tools, self.audit, task, self.brain))
        try:
            worker.start()
        except RuntimeError:
            self._finish(Result(1, "No se pudo iniciar la tarea."))
            return
        self.root.after(25, self._poll)

    def _poll(self) -> None:
        task = self._task
        if task is None:
            return
        try:
            while True:
                event = task.events.get_nowait()
                if isinstance(event, Result):
                    self._finish(event)
                    return
                if not task.cancelled.is_set():
                    self._approval = event
                    request = event.request
                    step = f"Paso {request.step_index}. " if request.step_index is not None else ""
                    self.prompt.configure(text=f"{step}{request.summary} ({request.timeout_seconds} s)")
                    self.approval_panel.grid()
                    self.status.set("Requiere aprobación")
                    self.reject_button.focus_set()
        except Empty:
            self.root.after(25, self._poll)

    def _hide_approval(self) -> None:
        self._approval = None
        self.approval_panel.grid_remove()

    def _answer(self, accepted: bool) -> None:
        approval = self._approval
        if approval is None:
            return
        self._hide_approval()
        self.status.set("En ejecución")
        approval.accepted = accepted
        approval.ready.set()

    def cancel(self) -> None:
        if self._task is None:
            return
        self._task.cancelled.set()
        if self._approval is not None:
            self._approval.ready.set()
            self._hide_approval()
        self.cancel_button.configure(state="disabled")
        self.status.set("Cancelando…")

    def _finish(self, result: Result) -> None:
        self._task = None
        self._hide_approval()
        if self._closing:
            self.release_tray()
            self.root.destroy()
            return
        self._set_output(result.message)
        statuses = {0: "Completado", 1: "Error", 2: "Detenido", 130: "Cancelado"}
        self.status.set(statuses.get(result.code, "Detenido"))
        self._set_busy(False)
        self.entry.focus_set()

    def close(self) -> None:
        self._closing = True
        if self._task is None:
            self.release_tray()
            self.root.destroy()
        else:
            self.cancel()


def _warm_up(brain) -> None:
    try:
        brain.warm_up()
    except Exception:
        pass  # La primera orden libre volverá a intentarlo.


def run_window(tools: LocalTools, audit: Audit, brain=None) -> int:
    try:
        root = tk.Tk()
    except tk.TclError:
        print("No se pudo abrir la ventana de JARVIS.")
        return 1
    window = DesktopWindow(root, tools, audit, brain)
    window.enable_tray()
    if brain is not None and hasattr(brain, "warm_up"):
        Thread(target=_warm_up, args=(brain,), daemon=True).start()
    try:
        root.mainloop()
    finally:
        window.release_tray()
    return 0
