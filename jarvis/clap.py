"""Detección de dos palmadas («clap clap») en el mismo audio que escucha «Hey Jarvis».

Una palmada es un transitorio: sube de golpe desde el silencio, dura menos de ~120 ms y tiene
energía en todas las frecuencias (la voz se concentra en las graves). Para despertar a JARVIS
hacen falta dos palmadas separadas entre 0,12 y 0,8 s, con silencio antes de la primera y después
de la segunda; así hablar, toser o un golpe suelto no lo activan. Solo usa numpy y casi no gasta CPU.
"""

from dataclasses import dataclass

SAMPLE_RATE = 16_000
FRAME = 160  # 10 ms
MIN_LEVEL = 0.05          # RMS mínimo de una palmada (escala 0..1)
FLOOR_RATIO = 8.0         # cuántas veces por encima del ruido de fondo
RISE_RATIO = 4.0          # subida respecto al cuadro anterior
BROADBAND = 0.55          # energía de alta frecuencia relativa (las palmadas son «chasquidos»)
MAX_DURATION = 0.12       # una palmada se apaga antes
DECAY = 0.25              # se considera terminada al bajar a este porcentaje del pico
MIN_GAP, MAX_GAP = 0.12, 0.8
QUIET_BEFORE = 0.15       # silencio requerido antes de la primera palmada
QUIET_AFTER = 0.20        # y después de la segunda
QUIET_RATIO = 3.0         # «silencio» = menos de esto × ruido de fondo


@dataclass
class ClapEvent:
    time: float
    peak: float
    broadband: float


class ClapDetector:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        import numpy as np

        self._np = np
        self._frame = int(sample_rate * 0.01)
        self._time = 0.0
        self._floor = 0.002
        self._previous = 0.0
        self._recent: list[tuple[float, float]] = []  # (tiempo, rms) de los últimos cuadros
        self._event: dict | None = None
        self._claps: list[ClapEvent] = []
        self._pending: ClapEvent | None = None  # segunda palmada esperando el silencio posterior
        self.last_candidate: ClapEvent | None = None

    def reset(self) -> None:
        self._event, self._claps, self._pending = None, [], None

    def _quiet(self, start: float, end: float) -> bool:
        frames = [rms for moment, rms in self._recent if start <= moment < end]
        return bool(frames) and max(frames) < max(self._floor * QUIET_RATIO, MIN_LEVEL * 0.3)

    def feed(self, chunk) -> bool:
        """Procesa audio int16 a 16 kHz. Devuelve True al completar «clap clap»."""
        np = self._np
        samples = np.asarray(chunk, dtype=np.float32) / 32768
        detected = False
        for start in range(0, len(samples) - self._frame + 1, self._frame):
            frame = samples[start:start + self._frame]
            rms = float(np.sqrt(np.mean(frame * frame)))
            high = float(np.sqrt(np.mean(np.diff(frame) ** 2))) / (rms + 1e-9)
            self._time += 0.01
            now = self._time
            self._recent.append((now, rms))
            self._recent = [(moment, value) for moment, value in self._recent if now - moment <= 1.2]
            if self._step(now, rms, high):
                detected = True
            if self._event is None and rms < self._floor * 4:
                self._floor = max(0.0005, self._floor * 0.995 + rms * 0.005)
            self._previous = rms
        return detected

    def _step(self, now: float, rms: float, high: float) -> bool:
        threshold = max(MIN_LEVEL, self._floor * FLOOR_RATIO)
        if self._event is None:
            if rms >= threshold and rms >= self._previous * RISE_RATIO and high >= BROADBAND:
                self._event = {"start": now, "peak": rms, "broadband": high}
        else:
            event = self._event
            event["peak"] = max(event["peak"], rms)
            if rms < event["peak"] * DECAY:
                duration = now - event["start"]
                self._event = None
                if duration <= MAX_DURATION:
                    clap = ClapEvent(event["start"], event["peak"], event["broadband"])
                    self.last_candidate = clap
                    self._register(clap)
            elif now - event["start"] > MAX_DURATION:
                self._event = None  # demasiado largo: voz, música o un golpe sostenido
                self._claps = []
        return self._confirm(now)

    def _register(self, clap: ClapEvent) -> None:
        self._claps = [old for old in self._claps if clap.time - old.time <= MAX_GAP]
        if not self._claps:
            if self._quiet(clap.time - QUIET_BEFORE - 0.02, clap.time - 0.02):
                self._claps = [clap]
            return
        first = self._claps[-1]
        gap = clap.time - first.time
        similar = max(first.peak, clap.peak) / max(1e-6, min(first.peak, clap.peak)) <= 4
        if MIN_GAP <= gap <= MAX_GAP and similar:
            self._pending = clap
        self._claps = [clap] if gap > MAX_GAP else []

    def _confirm(self, now: float) -> bool:
        pending = self._pending
        if pending is None or now - pending.time < QUIET_AFTER + 0.06:
            return False
        self._pending = None
        if self._event is None and self._quiet(pending.time + 0.06, now):
            self._claps = []
            return True
        return False  # siguió el ruido: eran más golpes o música, no «clap clap»
