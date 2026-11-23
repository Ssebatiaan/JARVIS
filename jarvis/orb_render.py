"""Dibujo del orbe de JARVIS con Pillow: bordes suaves, brillo difuso y transparencia real.

Las capas fijas (halo, disco, aros) se dibujan una vez por tamaño y tema. Cada cuadro solo
dibuja la capa animada al doble de resolución, la reduce (suavizado) y le aplica el brillo.
"""

import math
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SUPERSAMPLE = 2
# El reposo se repite exactamente cada 4 s para poder reutilizar sus cuadros.
IDLE_PERIOD = 4.0


@dataclass(frozen=True)
class Theme:
    name: str
    primary: tuple[int, int, int]
    secondary: tuple[int, int, int]
    core: tuple[int, int, int]
    edge: tuple[int, int, int]


THEMES = {
    "jarvis": Theme("jarvis", (64, 214, 255), (40, 110, 255), (14, 34, 52), (3, 7, 12)),
    "carmesí": Theme("carmesí", (255, 92, 92), (255, 160, 64), (40, 14, 16), (10, 3, 4)),
    "esmeralda": Theme("esmeralda", (80, 240, 170), (40, 180, 255), (10, 36, 30), (3, 9, 8)),
    "minimal": Theme("minimal", (235, 240, 245), (150, 160, 175), (26, 28, 32), (8, 9, 11)),
}
STATE_COLORS = {"aprobacion": (255, 181, 71), "error": (255, 99, 99)}


def _mix(a, b, amount: float):
    return tuple(round(x + (y - x) * amount) for x, y in zip(a, b))


def _font(size: int):
    for name in ("segoeuib.ttf", "seguisb.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=8)
def _static_layer(size: int, theme: Theme) -> Image.Image:
    """Halo, disco con degradado radial, borde y pista de los aros (se cachea)."""
    big = size * SUPERSAMPLE
    center = big / 2
    disc = big * 0.40
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    halo = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse(
        [center - disc * 1.06, center - disc * 1.06, center + disc * 1.06, center + disc * 1.06],
        fill=(*theme.secondary, 70),
    )
    layer.alpha_composite(halo.filter(ImageFilter.GaussianBlur(big * 0.045)))

    draw = ImageDraw.Draw(layer)
    steps = 40
    for step in range(steps):
        fraction = step / (steps - 1)
        radius = disc * (1 - fraction)
        color = _mix(theme.edge, theme.core, fraction ** 0.8)
        draw.ellipse([center - radius, center - radius, center + radius, center + radius], fill=(*color, 255))
    draw.ellipse([center - disc, center - disc, center + disc, center + disc],
                 outline=(*_mix(theme.primary, theme.edge, 0.55), 255), width=max(2, big // 140))
    ring = big * 0.455
    draw.ellipse([center - ring, center - ring, center + ring, center + ring],
                 outline=(*_mix(theme.primary, theme.edge, 0.78), 200), width=max(2, big // 90))
    return layer.resize((size, size), Image.LANCZOS)


class OrbRenderer:
    """Devuelve imágenes RGBA del orbe para cada estado."""

    def __init__(self, size: int = 180, theme: str = "jarvis"):
        self.size = size
        self.theme = THEMES.get(theme, THEMES["jarvis"])

    def _accent(self, state: str):
        return STATE_COLORS.get(state, self.theme.primary)

    def render(self, state: str, t: float, level: float = 0.0) -> Image.Image:
        """``state``: reposo, escuchando, procesando, hablando, aprobacion o error."""
        size, big = self.size, self.size * SUPERSAMPLE
        center = big / 2
        accent = self._accent(state)
        motion = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        draw = ImageDraw.Draw(motion)

        self._arcs(draw, center, big, state, t, level, accent)
        if state in ("escuchando", "hablando"):
            self._wave(draw, center, big, t, level, accent, speaking=state == "hablando")
        else:
            if state == "procesando":
                self._dots(draw, center, big, t, accent)
            self._emblem(draw, center, big, t, accent, state)

        motion = motion.resize((size, size), Image.LANCZOS)
        glow = motion.filter(ImageFilter.GaussianBlur(size * 0.03))
        frame = _static_layer(size, self.theme).copy()
        frame.alpha_composite(glow)
        frame.alpha_composite(glow)
        frame.alpha_composite(motion)
        return frame

    # --- partes animadas -------------------------------------------------------------------

    def _arcs(self, draw, center, big, state, t, level, accent):
        ring = big * 0.455
        box = [center - ring, center - ring, center + ring, center + ring]
        speed = {"procesando": 140, "escuchando": 50, "hablando": 60}.get(state, 0)
        idle = 2 * math.pi / IDLE_PERIOD
        breathe = 0.55 + 0.45 * (math.sin(t * idle) + 1) / 2 if state == "reposo" else 1.0
        width = max(3, big // 48)
        base = (t * speed) % 360
        for offset, length in ((0, 70), (125, 40), (215, 95)):
            start = base + offset
            color = (*accent, round(255 * breathe))
            draw.arc(box, start, start + length, fill=color, width=width)
        # Borde reactivo: el arco inferior se enciende con la voz.
        if level > 0.01 and state in ("escuchando", "hablando"):
            glow = min(1.0, level * 9)
            span = 40 + 90 * glow
            draw.arc(box, 90 - span / 2, 90 + span / 2, fill=(*_mix(accent, (255, 255, 255), 0.35), 255),
                     width=width + round(width * glow))

    def _wave(self, draw, center, big, t, level, accent, speaking):
        """Ondas luminosas con volumen: capas rellenas y translúcidas, más una línea brillante."""
        half = big * 0.34
        energy = min(1.0, level * 10) if level else 0.0
        energy = max(energy, 0.22 if speaking else 0.10)
        layers = (
            (self.theme.secondary, 1.00, 0.0, 70),
            (accent, 0.80, 1.7, 90),
            (_mix(accent, (255, 255, 255), 0.35), 0.55, 3.1, 110),
        )
        for color, scale, phase, alpha in layers:
            upper, lower = [], []
            for i in range(73):
                x = -1 + 2 * i / 72
                envelope = math.cos(x * math.pi / 2) ** 2  # se apaga hacia los bordes
                y = (math.sin(x * 6.5 + t * 5.5 + phase) * 0.55
                     + math.sin(x * 12 - t * 8.0 + phase) * 0.25
                     + math.sin(x * 2.8 + t * 2.2 + phase) * 0.45)
                height = (0.35 + abs(y)) * envelope * big * 0.17 * energy * scale
                px = center + x * half
                upper.append((px, center - height))
                lower.append((px, center + height * 0.55))
            draw.polygon(upper + lower[::-1], fill=(*color, alpha))
            draw.line(upper, fill=(*_mix(color, (255, 255, 255), 0.25), min(255, alpha * 3)),
                      width=max(2, big // 120), joint="curve")
        draw.line([(center - half, center), (center + half, center)],
                  fill=(*_mix(accent, (255, 255, 255), 0.6), 150), width=max(1, big // 220))

    def _dots(self, draw, center, big, t, accent):
        radius = big * 0.33
        count = 48
        for i in range(count):
            angle = 2 * math.pi * i / count
            head = (t * 1.4) % 1.0
            distance = (i / count - head) % 1.0
            brightness = max(0.15, 1 - distance * 1.6)
            dot = big * 0.0075 * (0.7 + 0.6 * brightness)
            x, y = center + radius * math.cos(angle), center + radius * math.sin(angle)
            draw.ellipse([x - dot, y - dot, x + dot, y + dot], fill=(*accent, round(255 * brightness)))

    def _emblem(self, draw, center, big, t, accent, state):
        """Emblema original: hexágono con una «J» estilizada."""
        pulse = 0.75 + 0.25 * (math.sin(t * (4.0 if state == "procesando" else 2 * math.pi / IDLE_PERIOD)) + 1) / 2
        color = (*accent, round(255 * pulse))
        radius = big * 0.115
        hexagon = [(center + radius * math.cos(math.radians(60 * k - 90)),
                    center + radius * math.sin(math.radians(60 * k - 90))) for k in range(6)]
        draw.line(hexagon + [hexagon[0]], fill=color, width=max(3, big // 80), joint="curve")
        inner = radius * 0.62
        draw.ellipse([center - inner, center - inner, center + inner, center + inner],
                     outline=(*accent, round(120 * pulse)), width=max(1, big // 200))
        font = _font(round(big * 0.12))
        draw.text((center, center + big * 0.004), "J", font=font, fill=color, anchor="mm")

    def menu_icon(self) -> Image.Image:
        """Icono de 32×32 para la bandeja del sistema."""
        return self.render("reposo", 0.9).resize((32, 32), Image.LANCZOS)
