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
from dataclasses import dataclass
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

    def window(self):
        """Las últimas 16 incrustaciones (16 × 96): lo que mira el clasificador."""
        return self._np.stack(list(self._features)).astype(self._np.float32)

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
        self._reload = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.personal = False  # True cuando usa el verificador entrenado con la voz del usuario

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

    def reload_verifier(self) -> None:
        """Vuelve a leer el verificador personal (tras entrenarlo u olvidarlo)."""
        self._reload.set()

    def _load_verifier(self):
        path = self._folder / VERIFIER_FILE
        try:
            verifier = PersonalVerifier.load(path) if path.exists() else None
        except (OSError, ValueError, KeyError):
            verifier = None
        self.personal = verifier is not None
        return verifier

    def _detected(self, model, chunk, verifier) -> bool:
        base = model.score(chunk)
        if verifier is None:
            return base >= self.threshold
        if model._predictions <= WARMUP_PREDICTIONS:
            return False
        return verifier.probability(model.window()) >= verifier.threshold

    def _run(self) -> None:
        try:
            model = self._model_factory(ensure_models(self._folder))
        except Exception as error:
            self.error = str(error) or "No pude cargar la palabra de activación."
            return
        verifier = self._load_verifier()
        last_wake = -REFRACTORY_SECONDS
        while not self._stop.is_set():
            if self._paused.is_set():
                self._stop.wait(0.1)
                continue
            try:
                for chunk in self._chunks(self._stop):
                    if self._paused.is_set():
                        break  # suelta el micrófono mientras JARVIS lo usa
                    if self._reload.is_set():
                        self._reload.clear()
                        verifier = self._load_verifier()
                    if self._detected(model, chunk, verifier) and monotonic() - last_wake >= REFRACTORY_SECONDS:
                        last_wake = monotonic()
                        model.reset()
                        self._on_wake()
            except WakeWordUnavailable as error:
                self.error = str(error)
                self._stop.wait(2)
            except Exception:
                self._stop.wait(2)
            model.reset()


# --- verificador personal --------------------------------------------------------------------
#
# Calibrado con la voz real del usuario: el modelo genérico solo detectó 2 de sus 7 «Hey Jarvis»
# en uso natural. Un verificador entrenado con 6 grabaciones suyas y voces de referencia detectó
# 7 de 7, sin falsas alarmas en 3,5 min de otras voces que no había visto. Solo se guardan los
# pesos del verificador; las grabaciones se descartan al terminar.

VERIFIER_FILE = "verificador.npz"
GENERIC_NEGATIVES_FILE = "voces_referencia.npy"
VERIFIER_THRESHOLD = 0.75
OFFLINE_VERIFIER_THRESHOLD = 0.97  # sin voces de referencia hay más riesgo de falsas alarmas
ENROLL_SAMPLES = 6
ENROLL_SECONDS = 2.6
READING_SECONDS = 16
ENROLL_PHRASE = ("Ayer fui al jardín con Harvey y jugamos un rato. Oye, ¿viste el partido? "
                 "Hay que revisar el servidor, abrir el correo y preparar la cena antes de las nueve.")
GENERIC_VOICES = ("es-CO-SalomeNeural", "es-AR-TomasNeural", "en-US-GuyNeural")
GENERIC_TEXTS = (
    "Bueno, la verdad es que ayer estuvimos hablando un buen rato sobre el proyecto y llegamos a la "
    "conclusión de que hay que cambiar varias cosas antes del viernes.",
    "Oye, ¿te acordaste de comprar el pan? Porque si no, mañana no tenemos nada para el desayuno.",
    "En el minuto noventa el delantero recibió el balón, se sacó a dos defensas y la clavó en el ángulo.",
    "Hola a todos, bienvenidos a un nuevo video. Hoy vamos a ver cómo configurar el servidor.",
    "Javier, Harvey y Gabriela llegaron tarde porque había mucho tráfico en la avenida principal.",
    "Hey guys, what is up? Today we are going to play the new update and check if the servers work.",
    "¿Me pasas la sal? Gracias. Oye, y del trabajo qué, ¿al final te dieron el aumento?",
    "La receta lleva harina, huevos, azúcar y un poco de vainilla. Se hornea durante treinta minutos.",
)


class PersonalVerifier:
    """Regresión logística sobre las 16 × 96 incrustaciones que ve el clasificador."""

    def __init__(self, mean, std, weights, bias: float, threshold: float = VERIFIER_THRESHOLD):
        import numpy as np

        self._np = np
        self.mean, self.std, self.weights = mean, std, weights
        self.bias, self.threshold = float(bias), float(threshold)

    @classmethod
    def fit(cls, positives, negatives, *, threshold: float = VERIFIER_THRESHOLD,
            iterations: int = 600, rate: float = 0.05, l2: float = 0.05) -> "PersonalVerifier":
        import numpy as np

        X = np.concatenate([np.asarray(positives), np.asarray(negatives)]).reshape(
            len(positives) + len(negatives), -1).astype(np.float32)
        y = np.concatenate([np.ones(len(positives)), np.zeros(len(negatives))])
        mean, std = X.mean(0), X.std(0) + 1e-6
        Z = (X - mean) / std
        weights, bias = np.zeros(Z.shape[1], dtype=np.float64), 0.0
        # Las clases se equilibran: hay muchas más ventanas negativas que positivas.
        balance = np.where(y == 1, len(y) / (2 * len(positives)), len(y) / (2 * len(negatives)))
        for _ in range(iterations):
            p = 1 / (1 + np.exp(-(Z @ weights + bias)))
            error = (p - y) * balance
            weights -= rate * (Z.T @ error / len(y) + l2 * weights)
            bias -= rate * float(error.mean())
        return cls(mean, std, weights, bias, threshold)

    def probability(self, window) -> float:
        np = self._np
        z = (np.asarray(window, dtype=np.float32).reshape(-1) - self.mean) / self.std
        return float(1 / (1 + np.exp(-(z @ self.weights + self.bias))))

    def save(self, path: Path) -> None:
        self._np.savez(path, mean=self.mean, std=self.std, weights=self.weights,
                       bias=self.bias, threshold=self.threshold)

    @classmethod
    def load(cls, path: Path) -> "PersonalVerifier":
        import numpy as np

        with np.load(path) as data:
            return cls(data["mean"], data["std"], data["weights"], float(data["bias"]), float(data["threshold"]))


def _analyse(model: WakeWordModel, audio) -> list:
    """Para cada bloque de 80 ms: (puntaje base, ventana de incrustaciones, energía)."""
    import numpy as np

    model.reset()
    rows = []
    for start in range(0, len(audio) - CHUNK + 1, CHUNK):
        chunk = audio[start:start + CHUNK]
        score = model.score(chunk)
        level = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2))) / 32768
        rows.append((score, model.window(), level))
    return rows


def _speech_end(levels: list) -> int:
    import numpy as np

    threshold = max(0.01, float(np.percentile(levels, 20)) * 4)
    voiced = [index for index, level in enumerate(levels) if level > threshold]
    return voiced[-1] if voiced else len(levels) - 1


def _variants(audio, rng):
    """El audio original, más bajo, más alto y con ruido: el verificador generaliza mejor."""
    import numpy as np

    yield audio
    for gain in (0.5, 1.6):
        yield np.clip(audio.astype(np.float64) * gain, -32768, 32767).astype(np.int16)
    yield np.clip(audio + rng.standard_normal(len(audio)) * 300, -32768, 32767).astype(np.int16)


def enrollment_windows(model: WakeWordModel, positives: list, negatives: list) -> tuple:
    """Ventanas positivas al final de cada «Hey Jarvis» y negativas del resto."""
    import numpy as np

    rng = np.random.default_rng(0)
    positive_windows, negative_windows = [], []
    for clip in positives:
        for audio in _variants(clip, rng):
            rows = _analyse(model, audio)
            if not rows:
                continue
            end = _speech_end([level for _, _, level in rows])
            positive_windows += [rows[i][1] for i in range(end, min(len(rows), end + 4))]
            negative_windows += [rows[i][1] for i in range(0, max(0, end - 18))]  # silencio previo
    for clip in negatives:
        for audio in _variants(clip, rng):
            negative_windows += [window for _, window, _ in _analyse(model, audio)]
    return positive_windows, negative_windows


def _edge_speech(text: str, voice: str):
    """Habla sintética de referencia (16 kHz int16) con las voces de Microsoft."""
    import asyncio
    import io

    import av
    import edge_tts
    import numpy as np

    async def collect() -> bytes:
        data = bytearray()
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk["type"] == "audio":
                data.extend(chunk["data"])
        return bytes(data)

    container = av.open(io.BytesIO(asyncio.run(collect())), format="mp3")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    audio = np.concatenate([out.to_ndarray().reshape(-1) for frame in container.decode(audio=0)
                            for out in resampler.resample(frame)])
    container.close()
    return audio


def reference_windows(model: WakeWordModel, folder: Path, synthesize: Callable | None = None):
    """Huellas de voces de referencia: se generan una vez y se guardan (no se guarda audio)."""
    import numpy as np

    cache = folder / GENERIC_NEGATIVES_FILE
    if cache.exists():
        return np.load(cache).astype(np.float32)
    speak = synthesize or _edge_speech
    windows = []
    for voice in GENERIC_VOICES:
        for text in GENERIC_TEXTS:
            windows += [window for _, window, _ in _analyse(model, speak(text, voice))]
    stacked = np.stack(windows).astype(np.float16)
    np.save(cache, stacked)
    return stacked.astype(np.float32)


@dataclass(frozen=True)
class EnrollmentReport:
    detected: int
    total: int
    with_references: bool


def train_personal_verifier(folder: Path, positives: list, negatives: list, *,
                            model: WakeWordModel | None = None,
                            synthesize: Callable | None = None) -> EnrollmentReport:
    """Entrena y guarda el verificador con las grabaciones del usuario."""
    model = model or WakeWordModel(ensure_models(folder))
    positive_windows, negative_windows = enrollment_windows(model, positives, negatives)
    if not positive_windows:
        raise WakeWordUnavailable("No oí ningún «Hey Jarvis» en las grabaciones.")
    try:
        negative_windows += list(reference_windows(model, folder, synthesize))
        threshold, with_references = VERIFIER_THRESHOLD, True
    except Exception:
        threshold, with_references = OFFLINE_VERIFIER_THRESHOLD, False
    verifier = PersonalVerifier.fit(positive_windows, negative_windows, threshold=threshold)
    detected = 0
    for clip in positives:
        rows = _analyse(model, clip)
        if any(verifier.probability(window) >= threshold for _, window, _ in rows[WARMUP_PREDICTIONS:]):
            detected += 1
    verifier.save(folder / VERIFIER_FILE)
    return EnrollmentReport(detected, len(positives), with_references)


def forget_personal_verifier(folder: Path) -> bool:
    path = folder / VERIFIER_FILE
    if path.exists():
        path.unlink()
        return True
    return False


def record(seconds: float):
    """Graba del micrófono predeterminado (16 kHz int16)."""
    import sounddevice as sd

    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="int16")
    sd.wait()
    return audio[:, 0].copy()
