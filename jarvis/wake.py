"""Palabra de activación «Hey Jarvis» con los modelos de openWakeWord, sin su paquete completo.

openWakeWord (código Apache 2.0) procesa el audio en tres modelos ONNX: espectrograma mel →
incrustaciones de voz → clasificador «hey jarvis». Aquí se ejecutan directamente con
onnxruntime, que ya instala faster-whisper, para evitar los ~113 MiB que suman scipy y
scikit-learn al importar el paquete. Los modelos preentrenados tienen licencia CC BY-NC-SA 4.0
(uso no comercial): se descargan una vez y se verifican con SHA-256.

Todo el audio se procesa en memoria y en el propio PC; no se graba ni se envía a ningún sitio.
"""

import hashlib
import threading
import urllib.request
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from time import monotonic

SAMPLE_RATE = 16_000
CHUNK = 1280  # 80 ms
MEL_CONTEXT = 160 * 3
MEL_WINDOW = 76
EMBEDDING_FRAMES = 16
# Calibrado con la voz del usuario: sus «Hey Jarvis» marcaron entre 0,28 y 0,99 y las frases
# normales 0,000. Con 0,5 solo se detectaban 3 de 6; con 0,22 se detectan todos.
SENSITIVITY = {"alta": 0.15, "normal": 0.22, "baja": 0.35}
THRESHOLD = SENSITIVITY["normal"]
REFRACTORY_SECONDS = 2.0
WARMUP_PREDICTIONS = 5
MODEL_URL = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/{}"
MODELS = {
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    "hey_jarvis_v0.1.onnx": "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
}


class WakeWordUnavailable(Exception):
    """Faltan los modelos o el micrófono para la palabra de activación."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_models(folder: Path, *, download: Callable[[str, Path], None] | None = None) -> Path:
    """Descarga los modelos si faltan y comprueba que no estén alterados."""
    folder.mkdir(parents=True, exist_ok=True)
    fetch = download or (lambda url, target: urllib.request.urlretrieve(url, target))
    for name, digest in MODELS.items():
        target = folder / name
        if not target.exists() or _sha256(target) != digest:
            partial = target.with_suffix(".parcial")
            try:
                fetch(MODEL_URL.format(name), partial)
            except OSError as error:
                raise WakeWordUnavailable("No pude descargar el modelo de activación.") from error
            if _sha256(partial) != digest:
                partial.unlink(missing_ok=True)
                raise WakeWordUnavailable("El modelo descargado no coincide con el esperado.")
            partial.replace(target)
    return folder


class WakeWordModel:
    """Calcula el puntaje de «hey jarvis» bloque a bloque (80 ms de audio int16 a 16 kHz)."""

    def __init__(self, folder: Path):
        import numpy as np
        import onnxruntime as ort

        self._np = np
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1  # un hilo basta y deja la CPU libre
        options.inter_op_num_threads = 1

        def session(name):
            return ort.InferenceSession(str(folder / name), sess_options=options,
                                        providers=["CPUExecutionProvider"])

        self._mel = session("melspectrogram.onnx")
        self._embedding = session("embedding_model.onnx")
        self._classifier = session("hey_jarvis_v0.1.onnx")
        self._mel_input = self._mel.get_inputs()[0].name
        self._embedding_input = self._embedding.get_inputs()[0].name
        self._classifier_input = self._classifier.get_inputs()[0].name
        self.reset()

    def reset(self) -> None:
        np = self._np
        self._raw = np.zeros(0, dtype=np.int16)
        self._mels = np.ones((MEL_WINDOW, 32), dtype=np.float32)
        self._features = deque(maxlen=EMBEDDING_FRAMES)
        self._predictions = 0
        # Igual que openWakeWord: el historial arranca con incrustaciones de ruido aleatorio.
        noise = np.random.default_rng(0).integers(-1000, 1000, SAMPLE_RATE * 4).astype(np.int16)
        spectrogram = self._melspectrogram(noise)
        for start in range(0, spectrogram.shape[0] - MEL_WINDOW + 1, 8):
            self._features.append(self._embed(spectrogram[start:start + MEL_WINDOW]))

    def _melspectrogram(self, audio):
        output = self._mel.run(None, {self._mel_input: audio.astype(self._np.float32)[None, :]})[0]
        return self._np.squeeze(output) / 10 + 2

    def _embed(self, window):
        tensor = window.astype(self._np.float32)[None, :, :, None]
        return self._np.squeeze(self._embedding.run(None, {self._embedding_input: tensor})[0])

    def score(self, chunk) -> float:
        np = self._np
        self._raw = np.concatenate([self._raw, chunk.astype(np.int16)])[-(CHUNK + MEL_CONTEXT):]
        if len(self._raw) < CHUNK + MEL_CONTEXT:
            return 0.0
        frames = self._melspectrogram(self._raw)
        self._mels = np.vstack([self._mels, frames])[-(MEL_WINDOW + 16):]
        self._features.append(self._embed(self._mels[-MEL_WINDOW:]))
        features = np.stack(list(self._features))[None, :, :].astype(np.float32)
        score = float(np.squeeze(self._classifier.run(None, {self._classifier_input: features})[0]))
        self._predictions += 1
        return 0.0 if self._predictions <= WARMUP_PREDICTIONS else score


def microphone_chunks(stop: threading.Event) -> Iterator:
    import sounddevice as sd

    try:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK)
    except Exception as error:
        raise WakeWordUnavailable("No pude abrir el micrófono.") from error
    with stream:
        while not stop.is_set():
            data, _ = stream.read(CHUNK)
            yield data[:, 0].copy()


class WakeWordListener:
    """Escucha en segundo plano y llama a ``on_wake`` al oír «Hey Jarvis».

    ``pause()`` y ``resume()`` permiten soltar el micrófono mientras JARVIS graba una orden o
    habla, para que no se active con su propia voz.
    """

    def __init__(self, folder: Path, on_wake: Callable[[], None], *,
                 model_factory: Callable[[Path], WakeWordModel] = WakeWordModel,
                 chunks: Callable[[threading.Event], Iterator] = microphone_chunks,
                 threshold: float = THRESHOLD):
        self._folder = folder
        self._on_wake = on_wake
        self._model_factory = model_factory
        self._chunks = chunks
        self.threshold = threshold
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-activacion", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def _run(self) -> None:
        try:
            model = self._model_factory(ensure_models(self._folder))
        except Exception as error:
            self.error = str(error) or "No pude cargar la palabra de activación."
            return
        last_wake = -REFRACTORY_SECONDS
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.1)
                continue
            try:
                for chunk in self._chunks(self._stop):
                    if self._paused.is_set():
                        break  # suelta el micrófono mientras JARVIS lo usa
                    if model.score(chunk) >= self.threshold and monotonic() - last_wake >= REFRACTORY_SECONDS:
                        last_wake = monotonic()
                        model.reset()
                        self._on_wake()
            except WakeWordUnavailable as error:
                self.error = str(error)
                self._stop.wait(2)
            except Exception:
                self._stop.wait(2)
            model.reset()
