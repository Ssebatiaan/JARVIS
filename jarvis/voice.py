"""Voz de JARVIS: escuchar con faster-whisper (local) y hablar con la voz neuronal Jorge (edge-tts).

Los paquetes de voz son opcionales (requirements.txt). Sin ellos JARVIS funciona solo por texto.
Las bibliotecas pesadas se importan al usarlas por primera vez, y el modelo de reconocimiento se
libera tras unos minutos sin uso para no ocupar RAM.
"""

import asyncio
import gc
import queue
import re
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator
from time import monotonic, sleep

SAMPLE_RATE = 16_000
BLOCK_SECONDS = 0.1
WHISPER_MODEL = "base"
# Órdenes habituales: Whisper reconoce mejor lo que aparece aquí (no cuesta RAM).
VOCABULARY = (
    "Jarvis, abre SAVIO. ¿Puedes abrir Discord? Abre YouTube y reproduce una canción. Pausa la canción. "
    "Minimiza todas las ventanas. Cambia a Opera GX. Cierra Spotify. Recuérdame mañana. Pon un temporizador. "
    "Consulta la CPU, la RAM y la GPU. Sube o baja el volumen. Qué recuerdas, olvida. Eso es todo."
)
VOICE = "es-MX-JorgeNeural"
# Velocidad de la voz (el usuario la encontraba muy pausada a -6 %). Se cambia con la voz.
SPEEDS = {"lenta": "+5%", "normal": "+25%", "rápida": "+35%", "muy rápida": "+45%"}
SPEED_ORDER = ("lenta", "normal", "rápida", "muy rápida")
DEFAULT_SPEED = "normal"
_current_rate = [SPEEDS[DEFAULT_SPEED]]


def set_speed(name: str) -> str:
    """Cambia la velocidad de la voz para las próximas frases y devuelve el nivel aplicado."""
    name = name if name in SPEEDS else DEFAULT_SPEED
    _current_rate[0] = SPEEDS[name]
    return name


def voice_rate() -> str:
    return _current_rate[0]
VOICE_PITCH = "-6Hz"
IDLE_UNLOAD_SECONDS = 300
MAX_RECORD_SECONDS = 15.0
SILENCE_TO_STOP = 1.0
NO_SPEECH_TIMEOUT = 6.0
MIN_THRESHOLD = 0.005
MAX_THRESHOLD = 0.03
CHIME_SECONDS = 0.4  # desde la activación hasta que termina el pitido
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


def speech_threshold(noise: float) -> float:
    """Nivel a partir del cual hay voz, con un tope para no ignorar a quien habla bajo."""
    return max(MIN_THRESHOLD, min(noise * 3.5, max(MAX_THRESHOLD, noise * 1.8)))


def record_until_silence(
    stop: threading.Event,
    *,
    blocks: Iterable | None = None,
    on_level: Callable[[float], None] | None = None,
    noise: float | None = None,
    block_seconds: float = BLOCK_SECONDS,
    deaf_seconds: float = 0.0,
):
    """Graba hasta una pausa tras hablar. Devuelve el audio o None si no se detectó voz.

    ``stop`` termina la grabación antes de tiempo (se conserva lo grabado). ``noise`` es el ruido de
    fondo medido antes de que el usuario hable; ``deaf_seconds``, el comienzo que se graba pero no
    cuenta como voz porque puede contener el pitido de JARVIS.
    """
    import numpy as np

    source = blocks if blocks is not None else microphone_blocks(stop)
    # Se cuentan bloques enteros: sumar 0,1 s repetidamente acumula error de redondeo.
    silence_blocks = round(SILENCE_TO_STOP / block_seconds)
    no_speech_blocks = round(NO_SPEECH_TIMEOUT / block_seconds)
    max_blocks = round(MAX_RECORD_SECONDS / block_seconds)
    deaf_blocks = round(deaf_seconds / block_seconds)
    collected, levels = [], []
    speech = False
    for block in source:
        level = _rms(block)
        if on_level is not None:
            on_level(level)
        collected.append(block)
        levels.append(level)
        if len(levels) < 3:
            continue
        # Se reevalúa todo lo grabado: si ya hablaba al empezar, la primera pausa revela el ruido real.
        threshold = speech_threshold(noise if noise is not None else min(levels))
        voiced = [index for index, value in enumerate(levels) if value > threshold and index >= deaf_blocks]
        speech = bool(voiced)
        if speech and len(levels) - 1 - voiced[-1] >= silence_blocks:
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

OUTPUT_RATE = 24_000
# El servicio cierra las conexiones inactivas entre 30 y 90 s (medido): se renuevan antes.
CONNECTION_REUSE_SECONDS = 45
CHUNK_TIMEOUT = 8.0


class _Mp3Decoder:
    """Decodifica MP3 por partes, a medida que llega, en float32 mono a 24 kHz."""

    def __init__(self):
        import av

        self._codec = av.CodecContext.create("mp3", "r")
        self._resampler = av.AudioResampler(format="flt", layout="mono", rate=OUTPUT_RATE)

    def _frames(self, frames) -> list:
        return [chunk.to_ndarray().reshape(-1).copy() for frame in frames
                for chunk in self._resampler.resample(frame)]

    def feed(self, data: bytes) -> list:
        return [chunk for packet in self._codec.parse(data) for chunk in self._frames(self._codec.decode(packet))]

    def flush(self) -> list:
        chunks = [chunk for packet in self._codec.parse(b"") for chunk in self._frames(self._codec.decode(packet))]
        return chunks + self._frames(self._codec.decode(None))


class NeuralStream:
    """Voz Jorge por una conexión persistente al servicio de voz de Edge.

    Abrir la conexión cuesta ~0,8 s; con ella abierta el audio empieza en ~0,2-0,5 s (medido).
    warm() la abre por adelantado mientras JARVIS escucha o trabaja. Usa piezas internas de
    edge-tts 7.3.1, la versión fijada en requirements.txt; si cambian, se usa el método simple.
    """

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._session = None
        self._socket = None
        self._last_used = 0.0
        self._connect_lock: asyncio.Lock | None = None
        self._speech_lock: asyncio.Lock | None = None

    def _submit(self, coroutine):
        with self._start_lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="jarvis-voz-red", daemon=True).start()
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def warm(self) -> None:
        self._submit(self._warm())

    async def _warm(self) -> None:
        try:
            await self._connection()
        except Exception:
            pass  # se reintentará al hablar

    def _fresh(self) -> bool:
        socket = self._socket
        return (socket is not None and not socket.closed
                and monotonic() - self._last_used < CONNECTION_REUSE_SECONDS)

    async def _connection(self):
        import aiohttp
        from edge_tts import communicate as protocol
        from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
        from edge_tts.drm import DRM

        if self._connect_lock is None:
            self._connect_lock = asyncio.Lock()
        async with self._connect_lock:
            if self._fresh():
                return self._socket
            await self._disconnect()
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(trust_env=True)
            url = (f"{WSS_URL}&ConnectionId={protocol.connect_id()}"
                   f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
            try:
                socket = await self._session.ws_connect(
                    url, compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=protocol._SSL_CTX,
                )
            except aiohttp.ClientResponseError as error:
                if error.status == 403:
                    DRM.handle_client_response_error(error)  # corrige el desfase del reloj
                raise
            await socket.send_str(
                f"X-Timestamp:{protocol.date_to_string()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\nPath:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
                '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
            )
            self._socket, self._last_used = socket, monotonic()
            return socket

    async def _disconnect(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None and not socket.closed:
            try:
                await socket.close()
            except Exception:
                pass

    async def _produce(self, text: str, out: queue.Queue, stop: threading.Event) -> None:
        import aiohttp
        from edge_tts import communicate as protocol
        from edge_tts.data_classes import TTSConfig

        if self._speech_lock is None:
            self._speech_lock = asyncio.Lock()
        produced = False
        try:
            async with self._speech_lock:
                for attempt in (1, 2):
                    try:
                        socket = await self._connection()
                        self._last_used = monotonic()
                        config = TTSConfig(VOICE, voice_rate(), "+0%", VOICE_PITCH, "SentenceBoundary")
                        await socket.send_str(protocol.ssml_headers_plus_data(
                            protocol.connect_id(), protocol.date_to_string(),
                            protocol.mkssml(config, protocol.escape(text)),
                        ))
                        decoder = _Mp3Decoder()
                        finished = False
                        async for message in socket:
                            if stop.is_set():
                                await self._disconnect()  # corta el audio pendiente
                                return
                            if message.type == aiohttp.WSMsgType.BINARY:
                                header = int.from_bytes(message.data[:2], "big")
                                for chunk in decoder.feed(message.data[2 + header:]):
                                    produced = True
                                    out.put(chunk)
                            elif message.type == aiohttp.WSMsgType.TEXT:
                                if "Path:turn.end" in message.data:
                                    finished = True
                                    break
                            else:
                                break
                        if not finished:
                            raise ConnectionResetError("El servicio de voz cerró la conexión.")
                        for chunk in decoder.flush():
                            out.put(chunk)
                        self._last_used = monotonic()
                        return
                    except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError) as error:
                        await self._disconnect()
                        if produced or attempt == 2:
                            raise VoiceUnavailable("El servicio de voz no respondió.") from error
        except Exception as error:
            out.put(error)
        finally:
            out.put(None)

    def chunks(self, text: str, stop: threading.Event) -> Iterator:
        out: queue.Queue = queue.Queue()
        self._submit(self._produce(text, out, stop))
        while True:
            try:
                item = out.get(timeout=CHUNK_TIMEOUT)
            except queue.Empty:
                raise VoiceUnavailable("El servicio de voz tardó demasiado.") from None
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    def close(self) -> None:
        if self._loop is None:
            return

        async def shutdown() -> None:
            await self._disconnect()
            if self._session is not None and not self._session.closed:
                await self._session.close()

        try:
            self._submit(shutdown()).result(timeout=3)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


class OneShotNeural:
    """Método simple de edge-tts (una conexión por frase). Respaldo si falla el persistente."""

    def chunks(self, text: str, stop: threading.Event) -> Iterator:
        import edge_tts

        async def collect() -> bytes:
            data = bytearray()
            async for chunk in edge_tts.Communicate(text, VOICE, rate=voice_rate(), pitch=VOICE_PITCH).stream():
                if chunk["type"] == "audio":
                    data.extend(chunk["data"])
            return bytes(data)

        mp3 = asyncio.run(collect())
        if not mp3:
            raise VoiceUnavailable("El servicio de voz no devolvió audio.")
        decoder = _Mp3Decoder()
        yield from decoder.feed(mp3)
        yield from decoder.flush()


# Volumen (RMS) de lo que JARVIS está diciendo, para animar el orbe. Lo escribe el hilo de voz.
OUTPUT_LEVEL = [0.0]


def _play_chunks(chunks: Iterable, stop: threading.Event) -> None:
    """Reproduce el audio a medida que llega; abre la salida con el primer bloque."""
    import numpy as np
    import sounddevice as sd

    stream = None
    try:
        for chunk in chunks:
            if stop.is_set():
                break
            if stream is None:
                stream = sd.OutputStream(samplerate=OUTPUT_RATE, channels=1, dtype="float32", latency="low")
                stream.start()
            OUTPUT_LEVEL[0] = float(np.sqrt(np.mean(np.square(chunk)))) if len(chunk) else 0.0
            stream.write(chunk.reshape(-1, 1))
    finally:
        OUTPUT_LEVEL[0] = 0.0
        if stream is not None:
            if stop.is_set():
                stream.abort()
            else:
                stream.stop()  # espera a que termine de sonar lo pendiente
            stream.close()


_WINDOWS_SPEECH = (
    "Add-Type -AssemblyName System.Speech; $v = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "$v.Speak([Console]::In.ReadToEnd())"
)


def _windows_speech(text: str, stop: threading.Event) -> None:
    """Último respaldo, sin internet: la voz instalada en Windows."""
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
    """Prueba las fuentes de voz en orden y pasa a la siguiente solo si no sonó nada."""

    def __init__(self, *, sources: list | None = None, play: Callable | None = None,
                 fallback: Callable | None = None):
        self._sources = sources if sources is not None else [NeuralStream(), OneShotNeural()]
        self._play = play or _play_chunks
        self._fallback = fallback or _windows_speech

    def warm(self) -> None:
        if self._sources and hasattr(self._sources[0], "warm"):
            self._sources[0].warm()

    def close(self) -> None:
        for source in self._sources:
            if hasattr(source, "close"):
                source.close()

    def speak(self, text: str, stop: threading.Event) -> None:
        text = speakable(text)
        if not text or stop.is_set():
            return
        for source in self._sources:
            started = False

            def tracked(source=source):
                nonlocal started
                for chunk in source.chunks(text, stop):
                    started = True
                    yield chunk

            try:
                self._play(tracked(), stop)
                return
            except Exception:
                if started or stop.is_set():
                    return
        if not stop.is_set():
            self._fallback(text, stop)


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

    @property
    def output_level(self) -> float:
        """Volumen actual de la voz de JARVIS (0 si no habla)."""
        return OUTPUT_LEVEL[0] if self.speaking else 0.0

    def preload(self) -> None:
        threading.Thread(target=self._safe_preload, daemon=True).start()

    def warm(self) -> None:
        """Abre por adelantado la conexión de la voz para que la respuesta suene antes."""
        try:
            self.synthesizer.warm()
        except Exception:
            pass

    def _safe_preload(self) -> None:
        try:
            self.recognizer.preload()
        except Exception:
            pass

    def listen(self, stop: threading.Event, *, on_level: Callable[[float], None] | None = None,
               on_transcribing: Callable[[], None] | None = None, microphone=None,
               chimed: bool = False) -> str | None:
        """Graba hasta una pausa y devuelve el texto, o None si no se oyó a nadie.

        ``microphone`` es el micrófono ya abierto por el detector de «Hey Jarvis» (sin huecos).
        """
        self.stop_speaking()
        if not getattr(self.recognizer, "loaded", True):
            self.preload()  # el modelo se carga mientras el usuario habla
        if microphone is not None:
            from .wake import CHUNK

            audio = self._record(stop, on_level=on_level, blocks=microphone.blocks(stop),
                                 noise=microphone.noise, block_seconds=CHUNK / SAMPLE_RATE,
                                 deaf_seconds=CHIME_SECONDS if chimed else 0.0)
        else:
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
        if hasattr(self.synthesizer, "close"):
            self.synthesizer.close()


def create_voice() -> Voice | None:
    """Devuelve la voz si los paquetes están instalados; si no, None."""
    if missing_packages():
        return None
    return Voice()
