"""Voz de JARVIS: escuchar con faster-whisper (local) y hablar con la voz neuronal Jorge (edge-tts).

Los paquetes de voz son opcionales (requirements.txt). Sin ellos JARVIS funciona solo por texto.
Las bibliotecas pesadas se importan al usarlas por primera vez, y el modelo de reconocimiento se
libera tras unos minutos sin uso para no ocupar RAM.
"""

import asyncio
import gc
import io
import re
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator
from time import sleep

SAMPLE_RATE = 16_000
BLOCK_SECONDS = 0.1
WHISPER_MODEL = "base"
VOCABULARY = (
    "Jarvis, abre el Bloc de notas, consulta la CPU y la RAM, sube o baja el volumen, "
    "lista procesos, abre la carpeta del proyecto, recuerda que, qué recuerdas, olvida."
)
VOICE = "es-MX-JorgeNeural"
VOICE_RATE = "-6%"
VOICE_PITCH = "-6Hz"
IDLE_UNLOAD_SECONDS = 300
MAX_RECORD_SECONDS = 15.0
SILENCE_TO_STOP = 1.0
NO_SPEECH_TIMEOUT = 6.0
MIN_THRESHOLD = 0.005
SPOKEN_LIMIT = 420
PACKAGES = {"faster_whisper": "faster-whisper", "sounddevice": "sounddevice", "edge_tts": "edge-tts", "av": "av"}
# Frases que Whisper inventa sobre silencio o ruido (habituales en español).
HALLUCINATIONS = re.compile(r"amara\.org|subt[ií]tulos|suscr[ií]bete|gracias por ver", re.IGNORECASE)


class VoiceUnavailable(Exception):
    """Falta un paquete o un dispositivo de audio para la voz."""


def missing_packages() -> list[str]:
    import importlib.util

    return [name for module, name in PACKAGES.items() if importlib.util.find_spec(module) is None]


# --- texto para hablar ---------------------------------------------------------------------

def speakable(text: str) -> str:
    """Adapta una respuesta para leerla en voz alta, sin tablas ni símbolos."""
    text = text.strip()
    if not text:
        return ""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 6 or "PID" in text:
        first = lines[0].rstrip(":.")
        return f"{first}. Te dejo el detalle en pantalla."
    spoken = " ".join(lines)
    replacements = (
        (r"(\d)\.(\d)", r"\1,\2"),
        (r"\s?%", " por ciento"),
        (r"\bGiB\b", "gigas"),
        (r"\bMiB\b", "megas"),
        (r"\s*·\s*", ". "),
        (r"[«»“”\"*_#`]", ""),
        (r"[\U0001F000-\U0001FAFF☀-➿]", ""),
        (r"\s{2,}", " "),
    )
    for pattern, replacement in replacements:
        spoken = re.sub(pattern, replacement, spoken)
    if len(spoken) > SPOKEN_LIMIT:
        cut = spoken[:SPOKEN_LIMIT]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        spoken = cut[: end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "…"
    return spoken.strip()


# --- escuchar ------------------------------------------------------------------------------

def _rms(block) -> float:
    import numpy as np

    return float(np.sqrt(np.mean(np.square(block, dtype=np.float64)))) if len(block) else 0.0


def microphone_blocks(stop: threading.Event) -> Iterator:
    """Bloques de 100 ms del micrófono predeterminado, en float32 mono a 16 kHz."""
    import sounddevice as sd

    block = int(SAMPLE_RATE * BLOCK_SECONDS)
    try:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=block)
    except Exception as error:
        raise VoiceUnavailable("No pude abrir el micrófono.") from error
    with stream:
        while not stop.is_set():
            data, _ = stream.read(block)
            yield data[:, 0].copy()


def record_until_silence(
    stop: threading.Event,
    *,
    blocks: Iterable | None = None,
    on_level: Callable[[float], None] | None = None,
):
    """Graba hasta una pausa tras hablar. Devuelve el audio o None si no se detectó voz.

    ``stop`` termina la grabación antes de tiempo (se conserva lo grabado).
    """
    import numpy as np

    source = blocks if blocks is not None else microphone_blocks(stop)
    # Se cuentan bloques enteros: sumar 0,1 s repetidamente acumula error de redondeo.
    silence_blocks = round(SILENCE_TO_STOP / BLOCK_SECONDS)
    no_speech_blocks = round(NO_SPEECH_TIMEOUT / BLOCK_SECONDS)
    max_blocks = round(MAX_RECORD_SECONDS / BLOCK_SECONDS)
    collected, levels = [], []
    threshold = None
    speech = False
    silent = 0
    for block in source:
        level = _rms(block)
        if on_level is not None:
            on_level(level)
        collected.append(block)
        levels.append(level)
        if threshold is None:
            if len(levels) < 3:
                continue
            threshold = max(MIN_THRESHOLD, min(levels) * 3.5)
        if level > threshold:
            speech, silent = True, 0
        elif speech:
            silent += 1
            if silent >= silence_blocks:
                break
        if not speech and len(collected) >= no_speech_blocks:
            return None
        if len(collected) >= max_blocks or stop.is_set():
            break
    if not speech:
        return None
    return np.concatenate(collected).astype(np.float32)


class SpeechRecognizer:
    """faster-whisper bajo demanda; el modelo se libera tras un tiempo sin uso."""

    def __init__(self, model: str = WHISPER_MODEL, *, idle_seconds: float = IDLE_UNLOAD_SECONDS,
                 factory: Callable | None = None):
        self._model_name = model
        self._idle_seconds = idle_seconds
        self._factory = factory
        self._model = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        if self._model is None:
            if self._factory is not None:
                self._model = self._factory()
            else:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
        return self._model

    def preload(self) -> None:
        with self._lock:
            self._load()
            self._schedule_unload()

    def _schedule_unload(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._idle_seconds, self.unload)
        self._timer.daemon = True
        self._timer.start()

    def unload(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._model is not None:
                self._model = None
                gc.collect()

    def transcribe(self, audio) -> str:
        with self._lock:
            model = self._load()
            segments, _ = model.transcribe(
                audio, language="es", beam_size=1, vad_filter=True, initial_prompt=VOCABULARY,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            self._schedule_unload()
        if HALLUCINATIONS.search(text):
            return ""
        return text


# --- hablar --------------------------------------------------------------------------------

def _neural_speech(text: str) -> tuple:
    """Audio de la voz Jorge en float32 mono a 24 kHz."""
    import av
    import edge_tts
    import numpy as np

    async def collect() -> bytes:
        data = bytearray()
        communicate = edge_tts.Communicate(text, VOICE, rate=VOICE_RATE, pitch=VOICE_PITCH)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                data.extend(chunk["data"])
        return bytes(data)

    mp3 = asyncio.run(collect())
    if not mp3:
        raise VoiceUnavailable("El servicio de voz no devolvió audio.")
    container = av.open(io.BytesIO(mp3), format="mp3")
    resampler = av.AudioResampler(format="flt", layout="mono", rate=24_000)
    chunks = []
    for frame in container.decode(audio=0):
        for out in resampler.resample(frame):
            chunks.append(out.to_ndarray().reshape(-1))
    container.close()
    return np.concatenate(chunks), 24_000


def _play(audio, rate: int, stop: threading.Event) -> None:
    import sounddevice as sd

    sd.play(audio, rate)
    try:
        while sd.get_stream().active:
            if stop.is_set():
                sd.stop()
                return
            sleep(0.05)
    finally:
        if stop.is_set():
            sd.stop()


_WINDOWS_SPEECH = (
    "Add-Type -AssemblyName System.Speech; $v = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$v.Speak([Console]::In.ReadToEnd())"
)


def _windows_speech(text: str, stop: threading.Event) -> None:
    """Respaldo sin internet: la voz instalada en Windows."""
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", _WINDOWS_SPEECH], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        process.stdin.write(text)
        process.stdin.close()
    except OSError:
        pass
    while process.poll() is None:
        if stop.is_set():
            process.kill()
            break
        sleep(0.05)
    process.wait()


class SpeechSynthesizer:
    def __init__(self, *, synthesize: Callable | None = None, play: Callable | None = None,
                 fallback: Callable | None = None):
        self._synthesize = synthesize or _neural_speech
        self._play = play or _play
        self._fallback = fallback or _windows_speech

    def speak(self, text: str, stop: threading.Event) -> None:
        text = speakable(text)
        if not text or stop.is_set():
            return
        try:
            audio, rate = self._synthesize(text)
        except Exception:
            if not stop.is_set():
                self._fallback(text, stop)
            return
        if not stop.is_set():
            self._play(audio, rate, stop)


class Voice:
    """Fachada para la ventana: escuchar, transcribir y hablar sin bloquear la interfaz."""

    def __init__(self, recognizer: SpeechRecognizer | None = None,
                 synthesizer: SpeechSynthesizer | None = None,
                 record: Callable = record_until_silence):
        self.recognizer = recognizer or SpeechRecognizer()
        self.synthesizer = synthesizer or SpeechSynthesizer()
        self._record = record
        self._speech_stop = threading.Event()
        self._speech_thread: threading.Thread | None = None

    @property
    def speaking(self) -> bool:
        thread = self._speech_thread
        return thread is not None and thread.is_alive()

    def preload(self) -> None:
        threading.Thread(target=self._safe_preload, daemon=True).start()

    def _safe_preload(self) -> None:
        try:
            self.recognizer.preload()
        except Exception:
            pass

    def listen(self, stop: threading.Event, *, on_level: Callable[[float], None] | None = None,
               on_transcribing: Callable[[], None] | None = None) -> str | None:
        """Graba hasta una pausa y devuelve el texto, o None si no se oyó a nadie."""
        self.stop_speaking()
        if not getattr(self.recognizer, "loaded", True):
            self.preload()  # el modelo se carga mientras el usuario habla
        audio = self._record(stop, on_level=on_level)
        if audio is None:
            return None
        if on_transcribing is not None:
            on_transcribing()
        return self.recognizer.transcribe(audio) or None

    def say(self, text: str) -> None:
        self.stop_speaking()
        stop = threading.Event()
        self._speech_stop = stop
        self._speech_thread = threading.Thread(
            target=self._speak_safely, args=(text, stop), name="jarvis-voz", daemon=True,
        )
        self._speech_thread.start()

    def _speak_safely(self, text: str, stop: threading.Event) -> None:
        try:
            self.synthesizer.speak(text, stop)
        except Exception:
            pass

    def stop_speaking(self) -> None:
        self._speech_stop.set()
        thread = self._speech_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1)

    def close(self) -> None:
        self.stop_speaking()
        self.recognizer.unload()


def create_voice() -> Voice | None:
    """Devuelve la voz si los paquetes están instalados; si no, None."""
    if missing_packages():
        return None
    return Voice()
