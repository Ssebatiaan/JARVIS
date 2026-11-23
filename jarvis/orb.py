"""Orbe flotante de JARVIS: ventana redonda con transparencia por píxel (UpdateLayeredWindow).

El orbe refleja el estado real de JARVIS (reposo, escuchando, procesando, hablando), se puede
arrastrar, abre el chat con un clic y ofrece un menú rápido con clic derecho. Las preferencias
(tamaño, tema, opacidad, siempre encima, posición) se guardan en la memoria local.
"""

import ctypes as ct
import tkinter as tk
from collections.abc import Callable
from ctypes import wintypes
from time import monotonic

from .orb_render import IDLE_PERIOD, THEMES, OrbRenderer

SIZES = {"pequeño": 120, "mediano": 160, "grande": 210}
OPACITIES = {"100 %": 1.0, "85 %": 0.85, "70 %": 0.7}
IDLE_FPS = 10
ACTIVE_FPS = 25
DRAG_THRESHOLD = 4
ERROR_SECONDS = 2.5

MENU_BACKGROUND = "#0d1620"
MENU_HOVER = "#16283a"
MENU_BORDER = "#1f3a4f"
MENU_TEXT = "#e8f5fc"
MENU_MUTED = "#7f98a8"
MENU_ACCENT = "#4fd8ff"
ICON_CHAT, ICON_QUICK, ICON_SETTINGS = "", "", ""
ICON_HIDE, ICON_POWER, ICON_CHECK = "", "", ""

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
GA_ROOT = 2


class _BitmapInfoHeader(ct.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BlendFunction(ct.Structure):
    _fields_ = [("BlendOp", ct.c_ubyte), ("BlendFlags", ct.c_ubyte),
                ("SourceConstantAlpha", ct.c_ubyte), ("AlphaFormat", ct.c_ubyte)]


class LayeredSurface:
    """Muestra imágenes RGBA en una ventana de Tk con transparencia real por píxel."""

    def __init__(self, toplevel: tk.Toplevel):
        user, gdi = ct.WinDLL("user32.dll"), ct.WinDLL("gdi32.dll")
        self._user, self._gdi = user, gdi
        user.GetAncestor.restype = wintypes.HWND
        user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user.GetWindowLongPtrW.restype = ct.c_ssize_t
        user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ct.c_int]
        user.SetWindowLongPtrW.restype = ct.c_ssize_t
        user.SetWindowLongPtrW.argtypes = [wintypes.HWND, ct.c_int, ct.c_ssize_t]
        user.GetDC.restype = wintypes.HDC
        user.GetDC.argtypes = [wintypes.HWND]
        user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user.UpdateLayeredWindow.restype = wintypes.BOOL
        user.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ct.POINTER(wintypes.POINT), ct.POINTER(wintypes.SIZE),
            wintypes.HDC, ct.POINTER(wintypes.POINT), wintypes.COLORREF, ct.POINTER(_BlendFunction),
            wintypes.DWORD]
        gdi.CreateCompatibleDC.restype = wintypes.HDC
        gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi.CreateDIBSection.restype = wintypes.HBITMAP
        gdi.CreateDIBSection.argtypes = [wintypes.HDC, ct.c_void_p, wintypes.UINT, ct.POINTER(ct.c_void_p),
                                         wintypes.HANDLE, wintypes.DWORD]
        gdi.SelectObject.restype = wintypes.HGDIOBJ
        gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi.DeleteDC.argtypes = [wintypes.HDC]

        toplevel.update_idletasks()
        self.hwnd = user.GetAncestor(toplevel.winfo_id(), GA_ROOT)
        style = user.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_LAYERED | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        user.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, style)
        self._screen = user.GetDC(None)
        self._memory = gdi.CreateCompatibleDC(self._screen)
        self._bitmap = None
        self._previous = None
        self._bits = ct.c_void_p()
        self._size = (0, 0)

    def _prepare(self, width: int, height: int) -> None:
        if self._size == (width, height):
            return
        self._release_bitmap()
        header = _BitmapInfoHeader(ct.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        self._bitmap = self._gdi.CreateDIBSection(self._memory, ct.byref(header), DIB_RGB_COLORS,
                                                  ct.byref(self._bits), None, 0)
        self._previous = self._gdi.SelectObject(self._memory, self._bitmap)
        self._size = (width, height)

    def show(self, image, x: int, y: int, opacity: float = 1.0) -> bool:
        return self.show_raw(premultiplied(image), image.size, x, y, opacity)

    def show_raw(self, data: bytes, size: tuple[int, int], x: int, y: int, opacity: float = 1.0) -> bool:
        width, height = size
        self._prepare(width, height)
        ct.memmove(self._bits, data, len(data))
        blend = _BlendFunction(AC_SRC_OVER, 0, max(0, min(255, round(255 * opacity))), AC_SRC_ALPHA)
        return bool(self._user.UpdateLayeredWindow(
            self.hwnd, self._screen, ct.byref(wintypes.POINT(x, y)), ct.byref(wintypes.SIZE(width, height)),
            self._memory, ct.byref(wintypes.POINT(0, 0)), 0, ct.byref(blend), ULW_ALPHA))

    def _release_bitmap(self) -> None:
        if self._bitmap is not None:
            self._gdi.SelectObject(self._memory, self._previous)
            self._gdi.DeleteObject(self._bitmap)
            self._bitmap = None

    def close(self) -> None:
        self._release_bitmap()
        if self._memory:
            self._gdi.DeleteDC(self._memory)
            self._memory = None
        if self._screen:
            self._user.ReleaseDC(None, self._screen)
            self._screen = None


def premultiplied(image) -> bytes:
    """Bytes BGRA con alfa premultiplicado, el formato que pide UpdateLayeredWindow."""
    return image.convert("RGBa").tobytes("raw", "BGRa")


class OrbMenu:
    """Menú rápido oscuro con submenús, al estilo del resto de la interfaz."""

    def __init__(self, root: tk.Misc, items: list, x: int, y: int, font_family: str,
                 parent: "OrbMenu | None" = None):
        self.root = root
        self.parent = parent
        self.child: OrbMenu | None = None
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.configure(background=MENU_BORDER)
        frame = tk.Frame(self.top, background=MENU_BACKGROUND, padx=6, pady=6)
        frame.pack(padx=1, pady=1)
        for item in items:
            if item is None:
                tk.Frame(frame, background=MENU_BORDER, height=1).pack(fill="x", pady=4)
                continue
            self._row(frame, item, font_family)
        self.top.update_idletasks()
        width, height = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
        screen_w, screen_h = self.top.winfo_screenwidth(), self.top.winfo_screenheight()
        x = min(x, screen_w - width - 8)
        y = min(y, screen_h - height - 48)
        self.top.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.top.deiconify()
        self.top.lift()
        self.top.bind("<Escape>", lambda event: self.close_all())
        if parent is None:
            self.top.focus_force()
            self.top.bind("<FocusOut>", lambda event: self.top.after(120, self._close_if_unfocused))

    def _row(self, frame, item, font_family):
        label, icon, action = item[:3]
        checked = len(item) > 3 and item[3]
        row = tk.Frame(frame, background=MENU_BACKGROUND, cursor="hand2")
        row.pack(fill="x")
        icon_text = ICON_CHECK if checked else icon
        icon_label = tk.Label(row, text=icon_text, width=2, background=MENU_BACKGROUND,
                              foreground=MENU_ACCENT if checked else MENU_MUTED, font=(font_family, 11))
        icon_label.pack(side="left", padx=(6, 8), pady=6)
        text = tk.Label(row, text=label, anchor="w", background=MENU_BACKGROUND, foreground=MENU_TEXT,
                        font=("Segoe UI", 10))
        text.pack(side="left", fill="x", expand=True, padx=(0, 18))
        arrow = tk.Label(row, text="›" if isinstance(action, list) else "", background=MENU_BACKGROUND,
                         foreground=MENU_MUTED, font=("Segoe UI", 11))
        arrow.pack(side="right", padx=(0, 8))
        widgets = (row, icon_label, text, arrow)

        def hover(active):
            for widget in widgets:
                widget.configure(background=MENU_HOVER if active else MENU_BACKGROUND)
            if active and isinstance(action, list):
                self._open_submenu(action, row, font_family)
            elif active and self.child is not None:
                self.child.close()
                self.child = None

        def choose(event=None):
            if isinstance(action, list):
                self._open_submenu(action, row, font_family)
                return
            self.close_all()
            action()

        for widget in widgets:
            widget.bind("<Enter>", lambda event: hover(True))
            widget.bind("<Leave>", lambda event: hover(False))
            widget.bind("<ButtonRelease-1>", choose)

    def _open_submenu(self, items, row, font_family):
        if self.child is not None:
            self.child.close()
        x = self.top.winfo_rootx() + self.top.winfo_width() - 2
        y = row.winfo_rooty() - 7
        self.child = OrbMenu(self.root, items, x, y, font_family, parent=self)

    def _close_if_unfocused(self):
        try:
            focused = self.top.focus_get()
        except (KeyError, tk.TclError):
            focused = None
        if focused is None:
            self.close_all()

    def close(self):
        if self.child is not None:
            self.child.close()
            self.child = None
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    def close_all(self):
        menu = self
        while menu.parent is not None:
            menu = menu.parent
        menu.close()


class OrbWindow:
    """El orbe flotante. ``window`` es la barra de chat (DesktopWindow)."""

    def __init__(self, root: tk.Tk, window, preferences=None, voice=None,
                 surface_factory: Callable = LayeredSurface):
        self.root = root
        self.window = window
        self.preferences = preferences
        self.voice = voice
        self._scale = max(1.0, root.winfo_fpixels("1i") / 96)
        self.size_name = self._setting("tamano", "mediano")
        self.theme_name = self._setting("tema", "jarvis")
        self.opacity_name = self._setting("opacidad", "100 %")
        self.topmost = self._setting("encima", "1") == "1"
        self.visible = True
        self._renderer = OrbRenderer(self.pixels, self.theme_name)
        self.top = tk.Toplevel(root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", self.topmost)
        self.top.geometry(f"{self.pixels}x{self.pixels}+{-10000}+{-10000}")
        self._surface = surface_factory(self.top)
        self.x, self.y = self._initial_position()
        self._press: tuple[int, int, int, int] | None = None
        self._dragged = False
        self._started = monotonic()
        self._error_until = 0.0
        self._after: str | None = None
        self._menu: OrbMenu | None = None
        self._closed = False
        self._idle_frames: list = []
        self.state = "reposo"
        self.top.bind("<ButtonPress-1>", self._on_press)
        self.top.bind("<B1-Motion>", self._on_drag)
        self.top.bind("<ButtonRelease-1>", self._on_release)
        self.top.bind("<Button-3>", self.open_menu)
        self._draw()
        self.top.deiconify()
        self._tick()

    # --- preferencias ------------------------------------------------------------------------

    def _setting(self, key: str, default: str) -> str:
        if self.preferences is None:
            return default
        try:
            return self.preferences.get_setting(f"orbe.{key}", default) or default
        except Exception:
            return default

    def _save(self, key: str, value: str) -> None:
        if self.preferences is not None:
            try:
                self.preferences.set_setting(f"orbe.{key}", value)
            except Exception:
                pass

    @property
    def pixels(self) -> int:
        return round(SIZES.get(self.size_name, SIZES["mediano"]) * self._scale)

    def _initial_position(self) -> tuple[int, int]:
        try:
            x, y = int(self._setting("x", "")), int(self._setting("y", ""))
        except ValueError:
            x = self.root.winfo_screenwidth() - self.pixels - round(40 * self._scale)
            y = self.root.winfo_screenheight() - self.pixels - round(90 * self._scale)
        return self._clamp(x, y)

    def _clamp(self, x: int, y: int) -> tuple[int, int]:
        width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        return max(0, min(x, width - self.pixels)), max(0, min(y, height - self.pixels))

    # --- estado y animación ------------------------------------------------------------------

    def current_state(self) -> tuple[str, float]:
        window = self.window
        status = window.status.get()
        listen = getattr(window, "_listen", None)
        if getattr(window, "_enrollment", None) is not None:
            return ("procesando" if status == "Aprendiendo…" else "escuchando"), 0.04
        if listen is not None:
            if status == "Transcribiendo…":
                return "procesando", 0.0
            return "escuchando", float(listen.level)
        if getattr(window, "_approval", None) is not None:
            return "aprobacion", 0.0
        if getattr(window, "_task", None) is not None:
            return "procesando", 0.0
        if self.voice is not None and getattr(self.voice, "speaking", False):
            return "hablando", float(getattr(self.voice, "output_level", 0.0))
        if status == "Error":
            if not self._error_until:
                self._error_until = monotonic() + ERROR_SECONDS
            if monotonic() < self._error_until:
                return "error", 0.0
        else:
            self._error_until = 0.0
        return "reposo", 0.0

    def _draw(self) -> None:
        self.state, level = self.current_state()
        opacity = OPACITIES.get(self.opacity_name, 1.0)
        elapsed = monotonic() - self._started
        if self.state == "reposo":
            # El reposo se repite cada IDLE_PERIOD s: cada cuadro se dibuja una vez y se reutiliza.
            count = round(IDLE_FPS * IDLE_PERIOD)
            if len(self._idle_frames) != count:
                self._idle_frames = [None] * count
            index = int(elapsed * IDLE_FPS) % count
            if self._idle_frames[index] is None:
                self._idle_frames[index] = premultiplied(self._renderer.render("reposo", index / IDLE_FPS))
            self._surface.show_raw(self._idle_frames[index], (self.pixels, self.pixels), self.x, self.y, opacity)
            return
        image = self._renderer.render(self.state, elapsed, level)
        self._surface.show(image, self.x, self.y, opacity)

    def _tick(self) -> None:
        if self._closed:
            return
        if self.visible:
            try:
                self._draw()
            except tk.TclError:
                return
        fps = ACTIVE_FPS if self.state != "reposo" else IDLE_FPS
        try:
            self._after = self.root.after(round(1000 / fps) if self.visible else 250, self._tick)
        except tk.TclError:
            self._after = None

    # --- ratón ---------------------------------------------------------------------------

    def _on_press(self, event) -> None:
        self._press = (event.x_root, event.y_root, self.x, self.y)
        self._dragged = False

    def _on_drag(self, event) -> None:
        if self._press is None:
            return
        start_x, start_y, orb_x, orb_y = self._press
        dx, dy = event.x_root - start_x, event.y_root - start_y
        if abs(dx) + abs(dy) >= DRAG_THRESHOLD:
            self._dragged = True
        if self._dragged:
            self.x, self.y = self._clamp(orb_x + dx, orb_y + dy)
            self._draw()

    def _on_release(self, event) -> None:
        if self._dragged:
            self._save("x", str(self.x))
            self._save("y", str(self.y))
        else:
            self.toggle_chat()
        self._press = None
        self._dragged = False

    def toggle_chat(self) -> None:
        if self.window.root.state() == "withdrawn":
            self.window.show_from_tray()
        else:
            self.window.hide_to_tray()

    def chat_position(self, width: int, height: int) -> tuple[int, int]:
        """Coloca la barra junto al orbe: encima si hay sitio, si no debajo."""
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        gap = round(12 * self._scale)
        x = self.x + self.pixels // 2 - width // 2
        y = self.y - height - gap if self.y - height - gap >= 0 else self.y + self.pixels + gap
        return max(8, min(x, screen_w - width - 8)), max(8, min(y, screen_h - height - 8))

    # --- menú ----------------------------------------------------------------------------

    def _menu_items(self) -> list:
        run = self.window.run_quick
        check = lambda value, current: value == current  # noqa: E731
        return [
            ("Abrir chat", ICON_CHAT, self.toggle_chat),
            ("Comandos rápidos", ICON_QUICK, [
                ("Estado del sistema", "", lambda: run("consulta CPU y RAM")),
                ("Volumen", "", lambda: run("consulta el volumen")),
                ("Qué recuerdas", "", lambda: run("qué recuerdas")),
                ("Hora y fecha", "", lambda: run("qué hora es")),
                ("Escuchar", "", self.window.start_listening),
            ]),
            ("Configuración", ICON_SETTINGS, [
                ("Tamaño", "", [(name.capitalize(), "", lambda n=name: self.set_size(n),
                                       check(name, self.size_name)) for name in SIZES]),
                ("Tema", "", [(name.capitalize(), "", lambda n=name: self.set_theme(n),
                                     check(name, self.theme_name)) for name in THEMES]),
                ("Opacidad", "", [(name, "", lambda n=name: self.set_opacity(n),
                                         check(name, self.opacity_name)) for name in OPACITIES]),
                ("Velocidad de voz", "", [(name.capitalize(), "",
                                                 lambda n=name: self.window.run_quick(f"velocidad {n}"),
                                                 check(name, getattr(self.window, "speed", "")))
                                                for name in ("lenta", "normal", "rápida", "muy rápida")]),
                ("Siempre en primer plano", "", self.toggle_topmost, self.topmost),
                ("Aprender mi voz", "", lambda: run("aprende mi voz")),
            ]),
            None,
            ("Minimizar a bandeja", ICON_HIDE, self.minimize_to_tray),
            ("Salir", ICON_POWER, self.window.close),
        ]

    def open_menu(self, event=None) -> None:
        if self._menu is not None:
            self._menu.close()
        x = event.x_root if event is not None else self.x + self.pixels
        y = event.y_root if event is not None else self.y
        font = getattr(self.window, "_icon_font", "Segoe MDL2 Assets")
        self._menu = OrbMenu(self.root, self._menu_items(), x + 4, y + 4, font)

    # --- ajustes ---------------------------------------------------------------------------

    def set_size(self, name: str) -> None:
        center_x, center_y = self.x + self.pixels // 2, self.y + self.pixels // 2
        self.size_name = name
        self._save("tamano", name)
        self._renderer = OrbRenderer(self.pixels, self.theme_name)
        self._idle_frames = []
        self.x, self.y = self._clamp(center_x - self.pixels // 2, center_y - self.pixels // 2)
        self._draw()

    def set_theme(self, name: str) -> None:
        self.theme_name = name
        self._save("tema", name)
        self._renderer = OrbRenderer(self.pixels, name)
        self._idle_frames = []
        self._draw()

    def set_opacity(self, name: str) -> None:
        self.opacity_name = name
        self._save("opacidad", name)
        self._draw()

    def toggle_topmost(self) -> None:
        self.topmost = not self.topmost
        self._save("encima", "1" if self.topmost else "0")
        self.top.attributes("-topmost", self.topmost)

    def minimize_to_tray(self) -> None:
        self.hide()
        self.window.hide_to_tray()

    def show(self) -> None:
        if not self.visible:
            self.visible = True
            self.top.deiconify()
            self._draw()

    def hide(self) -> None:
        self.visible = False
        self.top.withdraw()

    def close(self) -> None:
        self._closed = True
        if self._after is not None:
            try:
                self.root.after_cancel(self._after)
            except tk.TclError:
                pass
        if self._menu is not None:
            self._menu.close()
        self._surface.close()
        try:
            self.top.destroy()
        except tk.TclError:
            pass
