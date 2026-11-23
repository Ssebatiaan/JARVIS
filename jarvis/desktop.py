"""Barra flotante de JARVIS: aparece con el atajo global, ejecuta órdenes y muestra el resultado."""

import ctypes as ct
import math
import tkinter as tk
from pathlib import Path
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from threading import Event, Thread
from time import monotonic
from tkinter import font as tkfont

from .audit import Audit
from .core import ApprovalRequest, Brain, Result, normalize, run_command
from .persona import welcome
from .hotkey import (
    HOTKEY_ID, HOTKEY_LABEL, LISTEN_HOTKEY_LABEL, VK_K, GlobalHotkey, HotkeyUnavailable,
)
from .tools import LocalTools
from .tray import SystemTray
from .voice import VoiceUnavailable

# Paleta HUD
BACKGROUND = "#0a1016"
SURFACE = "#0f1a23"
BORDER = "#1c3242"
DIVIDER = "#16262f"
TEXT = "#e8f5fc"
MUTED = "#6c8696"
CYAN = "#4fd8ff"
AMBER = "#ffb547"
GREEN = "#5ee6a0"
RED = "#ff6b6b"
LISTEN = "#ff5c8a"
APPROVAL_SURFACE = "#1f1a0f"
FONT = "Segoe UI"
# Íconos nativos de Windows 11 (Segoe Fluent Icons) o Windows 10 (Segoe MDL2 Assets).
ICON_SEND, ICON_MIC, ICON_STOP = "\ue724", "\ue720", "\ue71a"
ICON_VOLUME, ICON_MUTE, ICON_HIDE = "\ue767", "\ue74f", "\ue921"
SPEECH_SETTING = "voz.respuestas"
DISMISS_MAX_WAIT = 8.0
WAKE_SETTING = "voz.activacion"
SENSITIVITY_SETTING = "voz.sensibilidad"
SENSITIVITY_COMMANDS = {
    "sensibilidad alta": "alta", "sube la sensibilidad": "alta", "sensibilidad normal": "normal",
    "sensibilidad baja": "baja", "baja la sensibilidad": "baja",
}
ENROLL_COMMANDS = {
    "aprende mi voz", "entrena hey jarvis", "calibra hey jarvis", "entrena tu oido",
    "aprende a reconocerme", "entrena mi voz",
}
FORGET_VOICE_COMMANDS = {"olvida mi voz", "borra mi voz"}
WAKE_COMMANDS = {
    "activa hey jarvis": True, "activa la palabra de activacion": True, "escuchame siempre": True,
    "desactiva hey jarvis": False, "desactiva la palabra de activacion": False,
    "deja de escuchar hey jarvis": False, "no me escuches": False,
}

WIDTH = 720
MAX_OUTPUT_LINES = 16
PLACEHOLDER = "Pregúntame lo que quieras…"

STATE_COLORS = {
    "Listo": CYAN,
    "En ejecución": CYAN,
    "Cancelando…": AMBER,
    "Requiere aprobación": AMBER,
    "Completado": GREEN,
    "Error": RED,
    "Detenido": AMBER,
    "Cancelado": MUTED,
    "Escuchando…": LISTEN,
    "Transcribiendo…": CYAN,
    "Grabando…": LISTEN,
    "Aprendiendo…": CYAN,
}
BUSY_STATES = {"En ejecución", "Cancelando…", "Transcribiendo…", "Aprendiendo…"}
RESULT_STATUSES = {0: "Completado", 1: "Error", 2: "Detenido", 130: "Cancelado"}


def _mix(color: str, other: str, amount: float) -> str:
    """Mezcla dos colores #rrggbb; amount=0 devuelve color y 1 devuelve other."""
    a = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


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


@dataclass
class _Listen:
    stop: Event = field(default_factory=Event)
    transcribing: Event = field(default_factory=Event)
    events: SimpleQueue = field(default_factory=SimpleQueue)
    cancelled: bool = False
    level: float = 0.0

    def set_level(self, level: float) -> None:
        self.level = level


def _listen_task(voice, listen: _Listen) -> None:
    try:
        text = voice.listen(listen.stop, on_level=listen.set_level, on_transcribing=listen.transcribing.set)
        listen.events.put(("text", text))
    except VoiceUnavailable as error:
        listen.events.put(("error", str(error)))
    except Exception:
        listen.events.put(("error", "No pude usar el micrófono."))


@dataclass
class _Enrollment:
    events: SimpleQueue = field(default_factory=SimpleQueue)
    cancelled: Event = field(default_factory=Event)


def _enroll_beep(double: bool = False) -> None:
    try:
        import winsound

        winsound.Beep(988 if not double else 784, 120)
        if double:
            winsound.Beep(784, 120)
    except (ImportError, RuntimeError):
        pass


def _enroll_task(enrollment: _Enrollment, folder, recorder, trainer) -> None:
    """Guía la grabación (6 «Hey Jarvis» y una lectura) y entrena el verificador personal."""
    from time import sleep

    from .wake import ENROLL_PHRASE, ENROLL_SAMPLES, ENROLL_SECONDS, READING_SECONDS

    post = enrollment.events.put
    try:
        positives = []
        for index in range(1, ENROLL_SAMPLES + 1):
            post(("prompt", f"Diga «Hey Jarvis» con su tono de siempre, justo después del pitido "
                            f"({index} de {ENROLL_SAMPLES}). Esc cancela."))
            sleep(0.9)
            if enrollment.cancelled.is_set():
                return
            _enroll_beep()
            positives.append(recorder(ENROLL_SECONDS))
        post(("prompt", f"Ahora lea en voz alta, con calma, y repita si le sobra tiempo:\n«{ENROLL_PHRASE}»\n"
                        "Empiece después del doble pitido."))
        sleep(4)
        if enrollment.cancelled.is_set():
            return
        _enroll_beep(double=True)
        reading = recorder(READING_SECONDS)
        if enrollment.cancelled.is_set():
            return
        post(("learning", "Aprendiendo su voz, señor. La primera vez puede tardar alrededor de un minuto."))
        report = trainer(folder, positives, [reading])
        extra = "" if report.with_references else (
            " Sin internet lo entrené en modo estricto; repítalo con conexión para afinarlo más.")
        post(("done", f"Listo, señor. Ya conozco su voz: reconocí {report.detected} de {report.total} "
                      f"grabaciones. Pruebe a decir «Hey Jarvis».{extra}"))
    except Exception as error:
        post(("error", f"No pude aprender su voz: {error}"))


def _run_task(
    text: str, tools: LocalTools, audit: Audit, task: _Task, brain: Brain | None = None
) -> None:
    try:
        result = run_command(
            text, tools, audit, approve=task.approve, cancel_requested=task.cancelled.is_set,
            brain=brain, persona=True,
        )
    except Exception:
        result = Result(1, "La orden se detuvo. Revisa la auditoría antes de repetirla.")
    task.events.put(result)


class _Orb:
    """Orbe animado que refleja el estado de JARVIS."""

    def __init__(self, parent: tk.Widget, size: int):
        self.size = size
        self.canvas = tk.Canvas(
            parent, width=size, height=size, background=SURFACE, highlightthickness=0, bd=0,
        )
        self._glows = [self.canvas.create_oval(0, 0, 0, 0, width=0) for _ in range(3)]
        self._core = self.canvas.create_oval(0, 0, 0, 0, width=0)
        self._ring = self.canvas.create_oval(0, 0, 0, 0, width=max(1, size // 22))
        self._spinner = self.canvas.create_arc(
            0, 0, 0, 0, style="arc", width=max(2, size // 14), start=0, extent=110,
        )
        self._phase = 0.0
        self.level = 0.0
        self._smoothed = 0.0

    def draw(self, state: str, step: float) -> None:
        self._phase += step
        color = STATE_COLORS.get(state, CYAN)
        busy = state in BUSY_STATES
        speed = 5.0 if busy else 1.6 if state == "Requiere aprobación" else 1.1
        pulse = (math.sin(self._phase * speed) + 1) / 2
        if state == "Escuchando…":
            self._smoothed += (min(1.0, self.level * 14) - self._smoothed) * 0.35
            pulse = 0.25 + 0.75 * self._smoothed
        center = self.size / 2
        base = self.size * 0.2
        radius = base * (1 + 0.12 * pulse)
        for index, glow in enumerate(self._glows):
            extent = radius + self.size * (0.08 + 0.07 * index) * (0.7 + 0.3 * pulse)
            shade = _mix(color, SURFACE, 0.72 + 0.09 * index - 0.1 * pulse)
            self.canvas.coords(glow, center - extent, center - extent, center + extent, center + extent)
            self.canvas.itemconfigure(glow, fill=shade)
        self.canvas.coords(self._core, center - radius, center - radius, center + radius, center + radius)
        self.canvas.itemconfigure(self._core, fill=_mix(color, "#ffffff", 0.25 * pulse))
        ring = self.size * 0.42
        self.canvas.coords(self._ring, center - ring, center - ring, center + ring, center + ring)
        self.canvas.itemconfigure(self._ring, outline=_mix(color, SURFACE, 0.65))
        self.canvas.coords(self._spinner, center - ring, center - ring, center + ring, center + ring)
        self.canvas.itemconfigure(
            self._spinner, outline=color, state="normal" if busy else "hidden",
            start=(-self._phase * 240) % 360,
        )


class DesktopWindow:
    def __init__(self, root: tk.Tk, tools: LocalTools, audit: Audit, brain: Brain | None = None,
                 voice=None, preferences=None):
        self.root = root
        self.tools = tools
        self.audit = audit
        self.brain = brain
        self.voice = voice
        self.preferences = preferences
        self._listen: _Listen | None = None
        self._listen_hotkey: GlobalHotkey | None = None
        self._greeted = False
        self._dismiss_deadline = 0.0
        self._wake = None
        self._wake_folder = None
        self.floating_orb = None  # orbe flotante (jarvis/orb.py), si Pillow está disponible
        self.anchor = None  # función (ancho, alto) -> (x, y) para colocar la barra junto al orbe
        self._enrollment: _Enrollment | None = None
        from . import wake as wake_module

        self.enroll_recorder = wake_module.record
        self.enroll_trainer = wake_module.train_personal_verifier
        self._wake_heard = Event()
        self._wake_after: str | None = None
        stored = preferences.get_setting(SPEECH_SETTING, "1") if preferences is not None else "1"
        self.speak_enabled = voice is not None and stored == "1"
        self.hide_on_blur = False
        self._task: _Task | None = None
        self._approval: _Approval | None = None
        self._tray: SystemTray | None = None
        self._tray_available = False
        self._hotkey: GlobalHotkey | None = None
        self._closing = False
        self._animation_after: str | None = None
        self._blur_after: str | None = None
        self._history: list[str] = []
        self._history_index = 0
        self._scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.command = tk.StringVar(root)
        self.status = tk.StringVar(root, value="Listo")

        root.title("JARVIS")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(background=BORDER)
        self._build()
        self._round_corners()
        self.command.trace_add("write", lambda *args: self._update_placeholder())
        self.status.trace_add("write", lambda *args: self._update_status_color())
        root.bind("<Escape>", lambda event: self._escape())
        root.bind("<FocusOut>", lambda event: self._schedule_blur_check())
        self._update_status_color()
        self._fit()
        self._animate()

    # --- construcción -------------------------------------------------------------------

    def _px(self, value: float) -> int:
        return round(value * self._scale)

    def _flat_button(self, parent, text, command, *, background=SURFACE, foreground=MUTED,
                     active=DIVIDER, bold=False):
        return tk.Button(
            parent, text=text, command=command, background=background, foreground=foreground,
            activebackground=active, activeforeground=TEXT, disabledforeground=_mix(MUTED, SURFACE, 0.5),
            relief="flat", bd=0, highlightthickness=0, cursor="hand2", takefocus=False,
            font=(FONT, 10, "bold" if bold else "normal"), padx=self._px(12), pady=self._px(5),
        )

    def _icon_button(self, parent, icon, command, color):
        button = self._flat_button(parent, icon, command, foreground=color)
        button.configure(font=(self._icon_font, 12), padx=self._px(9))
        return button

    def _build(self) -> None:
        families = set(tkfont.families(self.root))
        self._icon_font = "Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets"
        mono = "Cascadia Mono" if "Cascadia Mono" in families else "Consolas"
        pad = self._px(14)

        self.frame = tk.Frame(self.root, background=SURFACE)
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)
        self.frame.columnconfigure(0, weight=1)

        bar = tk.Frame(self.frame, background=SURFACE)
        bar.grid(row=0, column=0, sticky="ew", padx=(pad, pad), pady=(self._px(10), self._px(10)))
        bar.columnconfigure(1, weight=1)
        self.orb = _Orb(bar, self._px(44))
        self.orb.canvas.grid(row=0, column=0, padx=(0, self._px(10)))

        self.entry = tk.Entry(
            bar, textvariable=self.command, background=SURFACE, foreground=TEXT,
            disabledbackground=SURFACE, disabledforeground=MUTED, insertbackground=CYAN,
            insertwidth=self._px(2), relief="flat", bd=0, highlightthickness=0,
            font=(FONT, 16), selectbackground=BORDER, selectforeground=TEXT,
        )
        self.entry.grid(row=0, column=1, sticky="ew", ipady=self._px(4))
        self.placeholder = tk.Label(
            bar, text=PLACEHOLDER, background=SURFACE, foreground=MUTED, font=(FONT, 16), cursor="xterm",
        )
        self.placeholder.grid(row=0, column=1, sticky="w")
        self.placeholder.bind("<Button-1>", lambda event: self.entry.focus_set())
        self.entry.bind("<Return>", lambda event: self.submit())
        self.entry.bind("<Up>", lambda event: self._browse_history(-1))
        self.entry.bind("<Down>", lambda event: self._browse_history(1))
        self.entry.bind("<Tab>", self._tab_listen)

        self.status_label = tk.Label(
            bar, textvariable=self.status, background=SURFACE, font=(FONT, 9, "bold"),
        )
        self.status_label.grid(row=0, column=2, padx=(self._px(10), self._px(4)))
        self.mic_button = self._icon_button(bar, ICON_MIC, self.start_listening, CYAN)
        self.mic_button.grid(row=0, column=3)
        self.speaker_button = self._icon_button(bar, ICON_VOLUME, self.toggle_speech, MUTED)
        self.speaker_button.grid(row=0, column=4)
        self.execute_button = self._icon_button(bar, ICON_SEND, self.submit, CYAN)
        self.execute_button.grid(row=0, column=5)
        self.hide_button = self._icon_button(bar, ICON_HIDE, self.hide_to_tray, MUTED)
        self.hide_button.configure(state="disabled")
        self.hide_button.grid(row=0, column=6)
        if self.voice is None:
            self.mic_button.configure(state="disabled")
            self.speaker_button.configure(state="disabled")
        self._refresh_speaker()

        self.divider = tk.Frame(self.frame, background=DIVIDER, height=1)
        self.divider.grid(row=1, column=0, sticky="ew")

        self.approval_panel = tk.Frame(self.frame, background=APPROVAL_SURFACE)
        self.approval_panel.grid(row=2, column=0, sticky="ew")
        self.approval_panel.columnconfigure(0, weight=1)
        self.prompt = tk.Label(
            self.approval_panel, background=APPROVAL_SURFACE, foreground=AMBER, font=(FONT, 11, "bold"),
            anchor="w", justify="left", wraplength=self._px(WIDTH - 260),
        )
        self.prompt.grid(row=0, column=0, sticky="ew", padx=(pad, self._px(8)), pady=self._px(10))
        self.reject_button = self._flat_button(
            self.approval_panel, "Rechazar", lambda: self._answer(False),
            background=APPROVAL_SURFACE, foreground=TEXT, active="#2c2516",
        )
        self.reject_button.grid(row=0, column=1, padx=(0, self._px(6)))
        self.approve_button = self._flat_button(
            self.approval_panel, "Aprobar", lambda: self._answer(True),
            background=AMBER, foreground="#1a1306", active="#ffc977", bold=True,
        )
        self.approve_button.grid(row=0, column=2, padx=(0, pad))
        self.approval_panel.grid_remove()

        self.output = tk.Text(
            self.frame, background=SURFACE, foreground=TEXT, selectbackground=BORDER,
            selectforeground=TEXT, relief="flat", bd=0, highlightthickness=0, wrap="word",
            font=(FONT, 11), padx=pad, pady=self._px(10), height=1, state="disabled",
            cursor="arrow", spacing1=self._px(2), spacing3=self._px(2),
        )
        self.output.tag_configure("mono", font=(mono, 10))
        self.output.grid(row=3, column=0, sticky="nsew")

        footer = tk.Frame(self.frame, background=BACKGROUND)
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.hint = tk.Label(
            footer, background=BACKGROUND, foreground=MUTED, font=(FONT, 9), anchor="w",
            text=(f"Enter ejecutar · Tab hablar · Esc ocultar · ↑↓ historial · "
                  f"{HOTKEY_LABEL} escribir · {LISTEN_HOTKEY_LABEL} hablar"),
        )
        self.hint.grid(row=0, column=0, sticky="ew", padx=pad, pady=self._px(6))
        self.cancel_button = self._flat_button(
            footer, "Cancelar", self.cancel, background=BACKGROUND, foreground=AMBER, active=SURFACE,
        )
        self.cancel_button.configure(state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(0, self._px(8)))
        self.entry.focus_set()

    def _round_corners(self) -> None:
        """Esquinas redondeadas nativas de Windows 11; en otras versiones no hace nada."""
        try:
            self.root.update_idletasks()
            user = ct.WinDLL("user32.dll")
            user.GetAncestor.restype = ct.c_void_p
            user.GetAncestor.argtypes = [ct.c_void_p, ct.c_uint]
            window = user.GetAncestor(self.root.winfo_id(), 2)
            preference = ct.c_int(2)  # DWMWCP_ROUND
            ct.WinDLL("dwmapi.dll").DwmSetWindowAttribute(
                ct.c_void_p(window), 33, ct.byref(preference), ct.sizeof(preference),
            )
        except (OSError, AttributeError):
            pass

    # --- presentación -------------------------------------------------------------------

    def _animate(self) -> None:
        if self._closing:
            return
        visible = self.root.state() != "withdrawn"
        if visible:
            self.orb.draw(self.status.get(), 0.033 * 2)
        try:
            self._animation_after = self.root.after(33 if visible else 400, self._animate)
        except tk.TclError:
            self._animation_after = None

    def _update_placeholder(self) -> None:
        if self.command.get():
            self.placeholder.grid_remove()
        else:
            self.placeholder.grid()

    def _update_status_color(self) -> None:
        self.status_label.configure(foreground=STATE_COLORS.get(self.status.get(), MUTED))

    def _fit(self) -> None:
        """Ajusta la altura al contenido y coloca la barra en el tercio superior de la pantalla."""
        has_output = bool(self.output.get("1.0", "end-1c"))
        if has_output:
            # El área debe estar visible y con su ancho real antes de contar líneas ajustadas.
            self.output.grid()
            self.root.geometry(f"{self._px(WIDTH)}x{max(self.root.winfo_height(), 1)}")
            self.root.update_idletasks()
            counted = self.output.count("1.0", "end", "displaylines")
            lines = counted[0] if isinstance(counted, tuple) else counted or 1
            self.output.configure(height=max(1, min(lines, MAX_OUTPUT_LINES)))
        else:
            self.output.grid_remove()
        if has_output or self._approval is not None:
            self.divider.grid()
        else:
            self.divider.grid_remove()
        self.root.update_idletasks()
        width = self._px(WIDTH)
        height = self.frame.winfo_reqheight() + 2
        if self.anchor is not None:
            x, y = self.anchor(width, height)
        else:
            x = (self.root.winfo_screenwidth() - width) // 2
            y = self.root.winfo_screenheight() // 5
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _set_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text, ("mono",) if "\n" in text and "PID" in text else ())
        self.output.configure(state="disabled")
        self.output.yview_moveto(0)
        self._fit()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.entry.configure(state=state)
        self.execute_button.configure(state=state)
        self.cancel_button.configure(state="normal" if busy else "disabled")

    # --- mostrar / ocultar --------------------------------------------------------------

    @property
    def _can_hide(self) -> bool:
        tray = self._tray is not None and self._tray_available
        hotkey = self._hotkey is not None and self._hotkey.available
        return tray or hotkey or self.floating_orb is not None

    def enable_tray(self, icon_path: str | None = None) -> None:
        try:
            self._tray = SystemTray(self.root, self.show_from_tray, self.close, self._tray_changed, icon_path)
            self._tray_available = bool(self._tray.available)
        except OSError:
            self._tray = None
            self._tray_available = False
        self._refresh_hide_button()

    def enable_hotkey(self) -> bool:
        try:
            self._hotkey = GlobalHotkey(self.root, self.toggle)
        except (OSError, HotkeyUnavailable):
            self._hotkey = None
            self.hint.configure(text=f"No pude registrar {HOTKEY_LABEL}: otro programa ya lo usa.")
        if self.voice is not None:
            try:
                self._listen_hotkey = GlobalHotkey(
                    self.root, self.listen_from_hotkey, key=VK_K, hotkey_id=HOTKEY_ID + 1,
                )
            except (OSError, HotkeyUnavailable):
                self._listen_hotkey = None
                self.hint.configure(
                    text=f"No pude registrar {LISTEN_HOTKEY_LABEL}: otro programa ya lo usa."
                )
        self._refresh_hide_button()
        return self._hotkey is not None

    def _refresh_hide_button(self) -> None:
        self.hide_button.configure(state="normal" if self._can_hide else "disabled")

    def hide_to_tray(self) -> None:
        if self._can_hide and self._approval is None:
            self.root.withdraw()

    def run_quick(self, text: str) -> None:
        """Ejecuta una orden desde el menú del orbe mostrando la barra con el resultado."""
        if self._task is not None or self._listen is not None:
            return
        if self.root.state() == "withdrawn":
            self.show_from_tray()
        self.command.set(text)
        self.submit()

    def show_from_tray(self) -> None:
        if self.floating_orb is not None:
            self.floating_orb.show()
        if self._closing:
            return
        self.root.deiconify()
        self.root.lift()
        self._fit()
        self._focus()
        self._warm_voice()
        if self.hide_on_blur:
            self.root.after(250, self.greet)
        if self.brain is not None and hasattr(self.brain, "warm_up"):
            Thread(target=_warm_up, args=(self.brain,), daemon=True).start()

    def _focus(self) -> None:
        self.root.attributes("-topmost", True)
        self.root.focus_force()
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        try:
            user = ct.WinDLL("user32.dll")
            user.GetAncestor.restype = ct.c_void_p
            user.GetAncestor.argtypes = [ct.c_void_p, ct.c_uint]
            user.SetForegroundWindow.argtypes = [ct.c_void_p]
            user.SetForegroundWindow(user.GetAncestor(self.root.winfo_id(), 2))
        except (OSError, AttributeError):
            pass

    def toggle(self) -> None:
        visible = self.root.state() != "withdrawn"
        if visible and self.root.focus_get() is not None:
            self.hide_to_tray()
        else:
            self.show_from_tray()

    def _schedule_blur_check(self) -> None:
        if self.hide_on_blur and not self._closing and self._blur_after is None:
            self._blur_after = self.root.after(150, self._hide_if_blurred)

    def _hide_if_blurred(self) -> None:
        self._blur_after = None
        if (not self.hide_on_blur or self._closing or self._task is not None or self._listen is not None
                or self._enrollment is not None):
            return
        try:
            focused = self.root.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None:
            self.hide_to_tray()

    def _tray_changed(self, available: bool) -> None:
        self._tray_available = available
        self._refresh_hide_button()
        if not self._can_hide and not self._closing and self.root.state() == "withdrawn":
            self.root.deiconify()

    def release_tray(self) -> None:
        if self._tray is not None:
            self._tray.close()
            self._tray = None
        if self._hotkey is not None:
            self._hotkey.close()
            self._hotkey = None
        if self._listen_hotkey is not None:
            self._listen_hotkey.close()
            self._listen_hotkey = None

    def _escape(self) -> None:
        if self._enrollment is not None:
            self._enrollment.cancelled.set()
        elif self._listen is not None:
            self.cancel_listening()
        elif self.voice is not None and self._task is None and self.voice.speaking:
            self.voice.stop_speaking()
        elif self._approval is not None:
            self._answer(False)
        elif self._task is not None:
            self.cancel()
        else:
            self.hide_to_tray()

    def _browse_history(self, direction: int) -> str:
        if not self._history:
            return "break"
        self._history_index = max(0, min(len(self._history), self._history_index + direction))
        value = self._history[self._history_index] if self._history_index < len(self._history) else ""
        self.command.set(value)
        self.entry.icursor("end")
        return "break"

    # --- voz ----------------------------------------------------------------------------

    def greet(self) -> None:
        """Saludo de JARVIS la primera vez que se muestra la barra."""
        if self._greeted or self._task is not None or self._listen is not None:
            return
        self._greeted = True
        message = welcome()
        if self.root.state() != "withdrawn":
            self._set_output(message)
        if self.speak_enabled and self.voice is not None:
            self.voice.say(message)

    def _warm_voice(self) -> None:
        """Abre la conexión de la voz mientras JARVIS escucha o trabaja (ahorra ~0,8 s)."""
        if self.speak_enabled and self.voice is not None and hasattr(self.voice, "warm"):
            self.voice.warm()

    def _refresh_speaker(self) -> None:
        self.speaker_button.configure(
            text=ICON_VOLUME if self.speak_enabled else ICON_MUTE,
            foreground=CYAN if self.speak_enabled else MUTED,
        )

    def toggle_speech(self) -> None:
        if self.voice is None:
            return
        self.speak_enabled = not self.speak_enabled
        if not self.speak_enabled:
            self.voice.stop_speaking()
        if self.preferences is not None:
            try:
                self.preferences.set_setting(SPEECH_SETTING, "1" if self.speak_enabled else "0")
            except Exception:
                pass
        self._refresh_speaker()

    def _tab_listen(self, event=None) -> str:
        self.start_listening()
        return "break"

    def listen_from_hotkey(self) -> None:
        self._greeted = True  # quien llama para hablar no necesita el saludo
        if self.root.state() == "withdrawn":
            self.show_from_tray()
        self.start_listening()

    def start_listening(self) -> None:
        """Empieza a escuchar; si ya escucha, termina la grabación y transcribe lo dicho."""
        if self.voice is None:
            self._set_output("La voz no está instalada. Abre JARVIS con el entorno .venv del proyecto.")
            return
        if self._listen is not None:
            self._listen.stop.set()
            return
        if self._task is not None or self._closing:
            return
        listen = _Listen()
        self._listen = listen
        if self._wake is not None:
            self._wake.pause()  # el micrófono queda para la orden
        self.voice.stop_speaking()
        self._warm_voice()
        self.command.set("")
        self._set_output("")
        self.entry.configure(state="disabled")
        self.execute_button.configure(state="disabled")
        self.mic_button.configure(text=ICON_STOP, foreground=LISTEN)
        self.status.set("Escuchando…")
        if self.brain is not None and hasattr(self.brain, "warm_up"):
            Thread(target=_warm_up, args=(self.brain,), daemon=True).start()
        Thread(target=_listen_task, args=(self.voice, listen), daemon=True).start()
        self.root.after(30, self._poll_listen)

    def cancel_listening(self) -> None:
        if self._listen is not None:
            self._listen.cancelled = True
            self._listen.stop.set()

    def _poll_listen(self) -> None:
        listen = self._listen
        if listen is None:
            return
        self.orb.level = listen.level
        if listen.transcribing.is_set() and self.status.get() == "Escuchando…":
            self.status.set("Transcribiendo…")
        try:
            kind, value = listen.events.get_nowait()
        except Empty:
            self.root.after(30, self._poll_listen)
            return
        self._listen = None
        self._diag(f"escucha terminó: tipo={kind} oyó_texto={bool(value)} cancelada={listen.cancelled}")
        if self._wake is not None:
            self._wake.resume()
        self.orb.level = 0.0
        self.mic_button.configure(text=ICON_MIC, foreground=CYAN)
        self._set_busy(False)
        if self._closing:
            return
        if listen.cancelled:
            self.status.set("Listo")
        elif kind == "error":
            self.status.set("Error")
            self._set_output(value)
        elif not value:
            self.status.set("Listo")
            self._set_output(f"No te escuché. Pulsa Tab o {LISTEN_HOTKEY_LABEL} para intentarlo de nuevo.")
        else:
            self.command.set(value)
            self.submit()

    # --- palabra de activación ------------------------------------------------------------

    def enable_wake_word(self, folder) -> None:
        """Escucha «Hey Jarvis» en segundo plano si la voz está instalada y no se desactivó."""
        self._wake_folder = folder
        if self.voice is None or folder is None or self._wake is not None:
            return
        if self.preferences is not None and self.preferences.get_setting(WAKE_SETTING, "1") != "1":
            self._refresh_hint()
            return
        from .wake import SENSITIVITY, WakeWordListener

        level = self.preferences.get_setting(SENSITIVITY_SETTING, "normal") if self.preferences else "normal"
        self._wake = WakeWordListener(folder, self._wake_heard.set)
        self._wake.threshold = SENSITIVITY.get(level, SENSITIVITY["normal"])
        self._wake.start()
        self._refresh_hint()
        self._wake_after = self.root.after(100, self._poll_wake)

    def disable_wake_word(self) -> None:
        if self._wake is not None:
            self._wake.stop()
            self._wake = None
        if self._wake_after is not None:
            try:
                self.root.after_cancel(self._wake_after)
            except tk.TclError:
                pass
            self._wake_after = None
        self._refresh_hint()

    def _set_wake_word(self, enabled: bool) -> str:
        if self.voice is None:
            return "La voz no está instalada, señor; no puedo escuchar «Hey Jarvis»."
        if self.preferences is not None:
            self.preferences.set_setting(WAKE_SETTING, "1" if enabled else "0")
        if enabled:
            self.enable_wake_word(self._wake_folder)
            return "Como desee, señor. Le escucharé cuando diga «Hey Jarvis»."
        self.disable_wake_word()
        return "Entendido, señor. Dejo de escuchar «Hey Jarvis»; puede llamarme con AltGr+K."

    def _set_sensitivity(self, level: str) -> str:
        from .wake import SENSITIVITY

        if self.preferences is not None:
            self.preferences.set_setting(SENSITIVITY_SETTING, level)
        if self._wake is not None:
            self._wake.threshold = SENSITIVITY[level]
        hint = {"alta": "le oiré con más facilidad, aunque podría activarme alguna vez sin motivo",
                "normal": "vuelvo a la sensibilidad calibrada con su voz",
                "baja": "solo me activaré si lo dice con claridad"}[level]
        return f"Muy bien, señor: sensibilidad {level}; {hint}."

    def _diag(self, message: str) -> None:
        from .wake import diagnose

        diagnose(self._wake_folder, message)

    def _poll_wake(self) -> None:
        self._wake_after = None
        if self._wake is None or self._closing:
            return
        if self._wake_heard.is_set():
            self._wake_heard.clear()
            busy = {"escuchando": self._listen is not None, "tarea": self._task is not None,
                    "aprobacion": self._approval is not None, "entrenando": self._enrollment is not None}
            self._diag(f"barra recibió la activación; estado={self.root.state()} ocupado={busy}")
            if not any(busy.values()):
                _wake_chime()
                self.listen_from_hotkey()
                self._diag(f"escucha iniciada={self._listen is not None}; estado={self.root.state()}")
        if self._wake.error and self.hint["text"] != f"«Hey Jarvis» no disponible: {self._wake.error}":
            self.hint.configure(text=f"«Hey Jarvis» no disponible: {self._wake.error}")
        self._wake_after = self.root.after(100, self._poll_wake)

    def start_enrollment(self) -> str | None:
        """Empieza el entrenamiento guiado de «Hey Jarvis» con la voz del usuario."""
        if self.voice is None or self._wake_folder is None:
            return "Necesito la voz instalada para aprender su voz, señor."
        if self._enrollment is not None or self._task is not None or self._listen is not None:
            return None
        enrollment = _Enrollment()
        self._enrollment = enrollment
        if self._wake is not None:
            self._wake.pause()
        self.voice.stop_speaking()
        self.entry.configure(state="disabled")
        self.execute_button.configure(state="disabled")
        self.mic_button.configure(state="disabled")
        self.status.set("Grabando…")
        Thread(target=_enroll_task, args=(enrollment, self._wake_folder, self.enroll_recorder,
                                          self.enroll_trainer), name="jarvis-aprender-voz", daemon=True).start()
        self.root.after(50, self._poll_enrollment)
        return None

    def _poll_enrollment(self) -> None:
        enrollment = self._enrollment
        if enrollment is None or self._closing:
            return
        finished = None
        try:
            while True:
                kind, message = enrollment.events.get_nowait()
                if kind == "prompt":
                    self._set_output(message)
                elif kind == "learning":
                    self.status.set("Aprendiendo…")
                    self._set_output(message)
                else:
                    finished = (kind, message)
                    break
        except Empty:
            pass
        if finished is None and not enrollment.cancelled.is_set():
            self.root.after(50, self._poll_enrollment)
            return
        if finished is None:
            # Cancelado: se espera a que el hilo termine la grabación en curso.
            self.root.after(50, self._finish_cancelled_enrollment)
            return
        self._end_enrollment()
        kind, message = finished
        self.status.set("Completado" if kind == "done" else "Error")
        self._set_output(message)
        if kind == "done" and self._wake is not None:
            self._wake.reload_verifier()
            self._refresh_hint()
        if self.speak_enabled:
            self.voice.say(message)

    def _finish_cancelled_enrollment(self) -> None:
        self._end_enrollment()
        self.status.set("Cancelado")
        self._set_output("Entrenamiento cancelado, señor. Todo sigue como estaba.")

    def _end_enrollment(self) -> None:
        self._enrollment = None
        if self._wake is not None:
            self._wake.resume()
        self._set_busy(False)
        self.mic_button.configure(state="normal" if self.voice is not None else "disabled")

    def _forget_voice(self) -> str:
        from .wake import forget_personal_verifier

        if self._wake_folder is None or not forget_personal_verifier(self._wake_folder):
            return "No tenía guardada su voz, señor."
        if self._wake is not None:
            self._wake.reload_verifier()
            self._refresh_hint()
        return "He olvidado su voz, señor. Vuelvo al reconocimiento genérico de «Hey Jarvis»."

    def _refresh_hint(self) -> None:
        personal = self._wake is not None and getattr(self._wake, "personal", False)
        if self._wake is None:
            wake = ""
        elif personal:
            wake = "Di «Hey Jarvis», "
        else:
            wake = "Di «Hey Jarvis» («aprende mi voz» lo afina), "
        self.hint.configure(
            text=f"{wake}{LISTEN_HOTKEY_LABEL} o Tab para hablar · {HOTKEY_LABEL} escribir · "
                 "Enter ejecutar · Esc ocultar · ↑↓ historial"
        )

    # --- tareas -------------------------------------------------------------------------

    def submit(self) -> None:
        text = self.command.get().strip()
        if not text or self._task is not None or self._closing:
            return
        if normalize(text) == "salir":
            self.close()
            return
        spoken = normalize(text).strip(" .¿?¡!")
        if spoken in ENROLL_COMMANDS:
            self.command.set("")
            problem = self.start_enrollment()
            if problem:
                self.status.set("Detenido")
                self._set_output(problem)
            return
        if spoken in FORGET_VOICE_COMMANDS:
            self.command.set("")
            message = self._forget_voice()
            self.status.set("Completado")
            self._set_output(message)
            return
        toggle = WAKE_COMMANDS.get(spoken)
        level = SENSITIVITY_COMMANDS.get(spoken)
        if toggle is not None or level is not None:
            self.command.set("")
            message = self._set_wake_word(toggle) if toggle is not None else self._set_sensitivity(level)
            self.status.set("Completado")
            self._set_output(message)
            if self.speak_enabled and self.voice is not None:
                self.voice.say(message)
            return
        if self.voice is not None:
            self.voice.stop_speaking()
            self._warm_voice()
        if not self._history or self._history[-1] != text:
            self._history.append(text)
        self._history_index = len(self._history)
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
                    self._show_approval(event)
        except Empty:
            self.root.after(25, self._poll)

    def _show_approval(self, approval: _Approval) -> None:
        self._approval = approval
        request = approval.request
        step = f"Paso {request.step_index}. " if request.step_index is not None else ""
        self.prompt.configure(text=f"{step}{request.summary} ({request.timeout_seconds} s)")
        self.approval_panel.grid()
        self.status.set("Requiere aprobación")
        if self.root.state() == "withdrawn":
            self.show_from_tray()
        self._fit()
        self.reject_button.focus_set()

    def _hide_approval(self) -> None:
        self._approval = None
        self.approval_panel.grid_remove()
        self._fit()

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
            self._destroy()
            return
        self.status.set(RESULT_STATUSES.get(result.code, "Detenido"))
        self._set_busy(False)
        self.command.set("")
        self._set_output(result.message)
        self.entry.focus_set()
        if self.speak_enabled and self.voice is not None and result.message:
            self.voice.say(result.message)
        take_dismiss = getattr(self.tools, "take_dismiss", None)
        if callable(take_dismiss) and take_dismiss() is True:
            self._dismiss_deadline = monotonic() + DISMISS_MAX_WAIT
            self.root.after(400, self._finish_dismiss)

    def _finish_dismiss(self) -> None:
        """Espera a que JARVIS termine la despedida y oculta el orbe y el chat."""
        if self._closing:
            return
        speaking = self.voice is not None and getattr(self.voice, "speaking", False)
        if speaking and monotonic() < self._dismiss_deadline:
            self.root.after(200, self._finish_dismiss)
            return
        if self._task is not None or self._listen is not None:
            return  # el usuario volvió a hablarle antes de que se ocultara
        if self.floating_orb is not None:
            self.floating_orb.hide()
        self.root.withdraw()

    def _destroy(self) -> None:
        if self.floating_orb is not None:
            self.floating_orb.close()
            self.floating_orb = None
        if self._enrollment is not None:
            self._enrollment.cancelled.set()
        self.disable_wake_word()
        self.cancel_listening()
        if self.voice is not None:
            self.voice.stop_speaking()
        for after_id in (self._animation_after, self._blur_after):
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except tk.TclError:
                    pass
        self._animation_after = self._blur_after = None
        self.release_tray()
        self.root.destroy()

    def close(self) -> None:
        self._closing = True
        if self._task is None:
            self._destroy()
        else:
            self.cancel()


def _wake_chime() -> None:
    """Pitido breve para saber que JARVIS oyó «Hey Jarvis» aunque la barra no esté a la vista."""
    try:
        import winsound

        winsound.Beep(988, 90)
    except (ImportError, RuntimeError):
        pass


def _warm_up(brain) -> None:
    try:
        brain.warm_up()
    except Exception:
        pass  # La primera orden libre volverá a intentarlo.


def _enable_dpi_awareness() -> None:
    try:
        ct.WinDLL("shcore.dll").SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass


def _create_orb(root, window, preferences, voice):
    """El orbe necesita Pillow; sin él, JARVIS usa solo la barra."""
    try:
        from .orb import OrbWindow
    except ImportError:
        return None
    try:
        return OrbWindow(root, window, preferences, voice)
    except (OSError, tk.TclError):
        return None


def _tray_icon(folder) -> str | None:
    """Icono de la bandeja con el emblema de JARVIS (se genera una vez)."""
    if folder is None:
        return None
    try:
        from .orb_render import OrbRenderer
    except ImportError:
        return None
    path = Path(folder) / "jarvis.ico"
    if not path.exists():
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
            OrbRenderer(128).render("reposo", 0.9).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
        except OSError:
            return None
    return str(path)


def run_window(tools: LocalTools, audit: Audit, brain=None, *, start_hidden: bool = False,
               voice=None, preferences=None, wake_folder=None) -> int:
    _enable_dpi_awareness()
    try:
        root = tk.Tk()
    except tk.TclError:
        print("No se pudo abrir la ventana de JARVIS.")
        return 1
    root.withdraw()
    window = DesktopWindow(root, tools, audit, brain, voice, preferences)
    window.hide_on_blur = True
    data_folder = Path(wake_folder).parent if wake_folder is not None else None
    window.enable_tray(_tray_icon(data_folder))
    window.enable_hotkey()
    window.enable_wake_word(wake_folder)
    window.floating_orb = _create_orb(root, window, preferences, voice)
    if window.floating_orb is not None:
        window.anchor = window.floating_orb.chat_position
        if start_hidden:
            window.floating_orb.hide()
        else:
            window.greet()  # el orbe saluda por voz; la barra se abre con un clic
        if brain is not None and hasattr(brain, "warm_up"):
            Thread(target=_warm_up, args=(brain,), daemon=True).start()
    elif start_hidden and window._can_hide:
        if brain is not None and hasattr(brain, "warm_up"):
            Thread(target=_warm_up, args=(brain,), daemon=True).start()
    else:
        window.show_from_tray()
        window.greet()
    try:
        root.mainloop()
    finally:
        window.release_tray()
    return 0
