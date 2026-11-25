"""Ventana para conectar un calendario: el enlace se escribe aquí y no pasa por la IA ni el historial."""

import tkinter as tk
from queue import Empty, SimpleQueue
from threading import Thread
from tkinter import ttk

from .tools import CalendarError

BACKGROUND, SURFACE, TEXT, MUTED, CYAN, GREEN, RED = (
    "#0a1016", "#0f1a23", "#e8f5fc", "#6c8696", "#4fd8ff", "#5ee6a0", "#ff6b6b")
NAMES = ("SAVIO", "Outlook", "Google")
HELP = (
    "SAVIO: Calendario → Importar o exportar calendarios → Exportar → Obtener URL.\n"
    "Outlook web: Configuración → Calendario → Calendarios compartidos → Publicar → enlace ICS.\n"
    "Google: Configuración del calendario → Dirección secreta en formato iCal."
)


class CalendarDialog:
    def __init__(self, root: tk.Tk, calendars):
        self._calendars = calendars
        self._results: SimpleQueue = SimpleQueue()
        self._busy = False
        self._after: str | None = None
        self.window = window = tk.Toplevel(root)
        window.title("JARVIS: conectar un calendario")
        window.configure(background=BACKGROUND, padx=22, pady=18)
        window.attributes("-topmost", True)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.close)

        def label(text, color=TEXT, size=10):
            tk.Label(window, text=text, background=BACKGROUND, foreground=color, font=("Segoe UI", size),
                     justify="left", anchor="w", wraplength=520).pack(fill="x", pady=(6, 2))

        label("Conectar un calendario", CYAN, 14)
        label(HELP, MUTED, 9)
        label("Nombre")
        self.name = ttk.Combobox(window, values=NAMES)
        self.name.set(NAMES[0])
        self.name.pack(fill="x")
        label("Enlace privado (se oculta al pegarlo)")
        self.link = tk.Entry(window, show="•", width=64, background=SURFACE, foreground=TEXT, insertbackground=CYAN,
                             relief="flat", font=("Segoe UI", 10))
        self.link.pack(fill="x", ipady=5)
        self.link.bind("<Return>", lambda event: self.save())
        window.bind("<Escape>", lambda event: self.close())
        label("Se guarda cifrado en el Administrador de credenciales de Windows.", MUTED, 9)
        self.status = tk.Label(window, text="", background=BACKGROUND, foreground=MUTED, font=("Segoe UI", 10),
                               justify="left", anchor="w", wraplength=520)
        self.status.pack(fill="x", pady=(6, 4))
        self.button = ttk.Button(window, text="Conectar", command=self.save)
        self.button.pack(anchor="e")
        self.show()

    @property
    def alive(self) -> bool:
        return self.window is not None

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.link.focus_force()

    def save(self) -> None:
        if self._busy or self.window is None:
            return
        name, link = self.name.get(), self.link.get()
        if not link.strip():
            self.status.configure(text="Pegue el enlace del calendario.", foreground=RED)
            return
        self._busy = True
        self.button.configure(state="disabled")
        self.status.configure(text="Comprobando el calendario…", foreground=MUTED)
        Thread(target=self._check, args=(name, link), name="jarvis-calendario", daemon=True).start()
        self._after = self.window.after(80, self._poll)

    def _check(self, name: str, link: str) -> None:
        try:
            count = self._calendars.add(name, link)
            what = "1 actividad" if count == 1 else f"{count} actividades"
            self._results.put((True, f"Conectado: {what} en los próximos 60 días."))
        except CalendarError as error:
            self._results.put((False, f"No pude conectarlo: {error}."))
        except Exception:
            self._results.put((False, "No pude conectarlo."))

    def _poll(self) -> None:
        self._after = None
        if self.window is None:
            return
        try:
            good, message = self._results.get_nowait()
        except Empty:
            self._after = self.window.after(80, self._poll)
            return
        self._busy = False
        self.button.configure(state="normal")
        self.status.configure(text=message, foreground=GREEN if good else RED)
        if good:
            self.link.delete(0, "end")

    def close(self) -> None:
        if self.window is None:
            return
        window, self.window = self.window, None
        try:
            if self._after is not None:
                window.after_cancel(self._after)
            window.destroy()
        except tk.TclError:
            pass
