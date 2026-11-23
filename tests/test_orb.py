import importlib.util
import os
import unittest
from unittest.mock import Mock

HAS_PIL = importlib.util.find_spec("PIL") is not None
try:
    import tkinter as tk
except ImportError:
    tk = None


def release_tk(test):
    import gc

    for name in ("orb", "root", "window"):
        if hasattr(test, name):
            setattr(test, name, None)
    gc.collect()


@unittest.skipUnless(HAS_PIL, "Requiere Pillow (entorno .venv)")
class RendererTests(unittest.TestCase):
    def test_every_state_has_soft_transparent_edges_and_an_opaque_core(self):
        from jarvis.orb_render import THEMES, OrbRenderer

        for theme in THEMES:
            renderer = OrbRenderer(120, theme)
            for state in ("reposo", "escuchando", "procesando", "hablando", "aprobacion", "error"):
                with self.subTest(theme=theme, state=state):
                    image = renderer.render(state, 1.3, 0.05)
                    self.assertEqual((image.mode, image.size), ("RGBA", (120, 120)))
                    self.assertEqual(image.getpixel((0, 0))[3], 0)        # esquina transparente
                    self.assertEqual(image.getpixel((60, 60))[3], 255)    # núcleo opaco
                    edge_alphas = {image.getpixel((x, 60))[3] for x in range(4, 16)}
                    self.assertTrue(any(0 < alpha < 255 for alpha in edge_alphas))  # borde suave


class FakeSurface:
    def __init__(self, toplevel):
        self.frames = []
        self.closed = False

    def show(self, image, x, y, opacity=1.0):
        self.frames.append((image.size, x, y, opacity))
        return True

    def show_raw(self, data, size, x, y, opacity=1.0):
        self.frames.append((size, x, y, opacity))
        return True

    def close(self):
        self.closed = True


class Preferences(dict):
    def get_setting(self, key, default=None):
        return self.get(key, default)

    def set_setting(self, key, value):
        self[key] = value


@unittest.skipUnless(HAS_PIL and os.name == "nt" and tk is not None, "Requiere Windows, Tkinter y Pillow")
class OrbWindowTests(unittest.TestCase):
    def setUp(self):
        from jarvis.orb import OrbWindow

        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(release_tk, self)
        self.window = Mock()
        self.window.status = tk.StringVar(self.root, value="Listo")
        self.window._listen = self.window._task = self.window._approval = self.window._enrollment = None
        self.window._icon_font = "Segoe MDL2 Assets"
        self.window.root.state.return_value = "withdrawn"
        self.voice = Mock(speaking=False, output_level=0.0)
        self.preferences = Preferences()
        self.orb = OrbWindow(self.root, self.window, self.preferences, self.voice, surface_factory=FakeSurface)
        self.addCleanup(self.close)

    def close(self):
        if self.orb is not None:
            self.orb.close()

    def test_state_follows_what_jarvis_is_doing(self):
        orb, window = self.orb, self.window
        self.assertEqual(orb.current_state()[0], "reposo")
        window._listen = Mock(level=0.07)
        window.status.set("Escuchando…")
        self.assertEqual(orb.current_state(), ("escuchando", 0.07))
        window.status.set("Transcribiendo…")
        self.assertEqual(orb.current_state()[0], "procesando")
        window._listen = None
        window._task = object()
        self.assertEqual(orb.current_state()[0], "procesando")
        window._task = None
        self.voice.speaking, self.voice.output_level = True, 0.05
        self.assertEqual(orb.current_state(), ("hablando", 0.05))
        self.voice.speaking = False
        window._approval = object()
        self.assertEqual(orb.current_state()[0], "aprobacion")
        window._approval = None
        window.status.set("Error")
        self.assertEqual(orb.current_state()[0], "error")

    def test_click_opens_the_chat_and_drag_moves_and_remembers_the_position(self):
        orb = self.orb
        orb._on_press(Mock(x_root=500, y_root=500))
        orb._on_release(Mock())
        self.window.show_from_tray.assert_called_once_with()
        start = (orb.x, orb.y)
        orb._on_press(Mock(x_root=500, y_root=500))
        orb._on_drag(Mock(x_root=440, y_root=470))
        orb._on_release(Mock())
        self.assertEqual((orb.x, orb.y), orb._clamp(start[0] - 60, start[1] - 30))
        self.assertEqual(self.preferences["orbe.x"], str(orb.x))
        self.window.show_from_tray.assert_called_once_with()  # arrastrar no abre el chat
        self.assertEqual(orb._surface.frames[-1][1:3], (orb.x, orb.y))

    def test_settings_are_applied_and_remembered(self):
        orb = self.orb
        orb.set_size("grande")
        orb.set_theme("carmesí")
        orb.set_opacity("70 %")
        orb.toggle_topmost()
        self.assertEqual(self.preferences["orbe.tamano"], "grande")
        self.assertEqual(self.preferences["orbe.tema"], "carmesí")
        self.assertEqual(self.preferences["orbe.encima"], "0")
        size, _, _, opacity = orb._surface.frames[-1]
        self.assertEqual((size[0], opacity), (orb.pixels, 0.7))
        from jarvis.orb import OrbWindow

        again = OrbWindow(self.root, self.window, self.preferences, self.voice, surface_factory=FakeSurface)
        self.addCleanup(again.close)
        self.assertEqual((again.size_name, again.theme_name, again.topmost), ("grande", "carmesí", False))

    def test_chat_goes_next_to_the_orb_and_menu_actions_run(self):
        orb = self.orb
        x, y = orb.chat_position(300, 100)
        self.assertLessEqual(abs(x + 150 - (orb.x + orb.pixels // 2)), orb.root.winfo_screenwidth())
        self.assertTrue(y + 100 <= orb.y or y >= orb.y + orb.pixels)
        labels = [item[0] for item in orb._menu_items() if item]
        self.assertEqual(labels, ["Abrir chat", "Comandos rápidos", "Configuración", "Minimizar a bandeja", "Salir"])
        quick = dict((item[0], item[2]) for item in orb._menu_items()[1][2])
        quick["Estado del sistema"]()
        self.window.run_quick.assert_called_once_with("consulta CPU y RAM")
        orb.open_menu(Mock(x_root=100, y_root=100))
        self.assertIsNotNone(orb._menu)
        orb.minimize_to_tray()
        self.assertFalse(orb.visible)
        self.window.hide_to_tray.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
