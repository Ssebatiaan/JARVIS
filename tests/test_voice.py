import importlib.util
import os
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import Mock

from jarvis.audit import Audit
from jarvis.tools import LocalTools, SystemUsage
from jarvis.voice import SpeechRecognizer, SpeechSynthesizer, Voice, record_until_silence, speakable

HAS_NUMPY = importlib.util.find_spec("numpy") is not None
try:
    import tkinter as tk
    from jarvis.desktop import DesktopWindow
except ImportError:
    tk = None

def release_tk(test):
    """Suelta las referencias a Tk y recolecta en el hilo principal.

    unittest libera cada prueba al terminar; si el recolector de basura destruyera después la
    ventana desde un hilo de trabajo, Tcl abortaría el proceso («Tcl_AsyncDelete»).
    """
    import gc

    for name in ("window", "root"):
        if hasattr(test, name):
            setattr(test, name, None)
    gc.collect()



class SpeakableTests(unittest.TestCase):
    def test_reads_numbers_units_and_symbols_naturally(self):
        self.assertEqual(
            speakable("CPU 5.9 % · RAM 9.34 de 15.34 GiB."),
            "CPU 5,9 por ciento. RAM 9,34 de 15,34 gigas.",
        )
        self.assertEqual(speakable("Lo recordaré: «Se llama Ana» 🙂"), "Lo recordaré: Se llama Ana")

    def test_tables_are_summarised_instead_of_read(self):
        table = "Procesos en la captura de Windows\n       PID  Ejecutable\n         4  System\nTotal de procesos: 3."
        self.assertEqual(speakable(table), "Procesos en la captura de Windows. Te dejo el detalle en pantalla.")

    def test_long_answers_are_cut_at_a_sentence(self):
        text = ("Esta es una frase de prueba bastante larga para el asistente. " * 12).strip()
        spoken = speakable(text)
        self.assertLessEqual(len(spoken), 420)
        self.assertTrue(spoken.endswith("."))
        self.assertEqual(speakable("   "), "")


@unittest.skipUnless(HAS_NUMPY, "Requiere numpy (entorno .venv)")
class RecordTests(unittest.TestCase):
    def blocks(self, pattern):
        import numpy as np

        rng = np.random.default_rng(1)
        for kind in pattern:
            amplitude = 0.002 if kind == "-" else 0.2
            yield (rng.standard_normal(1600) * amplitude).astype(np.float32)

    def test_stops_after_a_pause_following_speech(self):
        levels = []
        audio = record_until_silence(threading.Event(), blocks=self.blocks("---" + "#" * 8 + "-" * 30),
                                     on_level=levels.append)
        self.assertIsNotNone(audio)
        self.assertEqual(len(audio), 1600 * (3 + 8 + 10))
        self.assertGreater(max(levels), 0.1)

    def test_returns_none_when_nobody_speaks(self):
        self.assertIsNone(record_until_silence(threading.Event(), blocks=self.blocks("-" * 80)))

    def test_stop_ends_early_and_keeps_what_was_said(self):
        stop = threading.Event()

        def source():
            for index, block in enumerate(self.blocks("---" + "#" * 50)):
                if index == 6:
                    stop.set()
                yield block

        audio = record_until_silence(stop, blocks=source())
        self.assertEqual(len(audio), 1600 * 7)


class RecognizerTests(unittest.TestCase):
    def fake_model(self, *texts):
        model = Mock()
        model.transcribe.return_value = ([Mock(text=text) for text in texts], None)
        return model

    def test_loads_on_demand_uses_vocabulary_and_unloads_when_idle(self):
        model = self.fake_model(" Hola Jarvis,", " abre el Bloc de notas. ")
        factory = Mock(return_value=model)
        recognizer = SpeechRecognizer(factory=factory, idle_seconds=0.2)
        self.assertFalse(recognizer.loaded)
        self.assertEqual(recognizer.transcribe(b"audio"), "Hola Jarvis, abre el Bloc de notas.")
        self.assertEqual(model.transcribe.call_args.kwargs["language"], "es")
        self.assertIn("Jarvis", model.transcribe.call_args.kwargs["initial_prompt"])
        deadline = monotonic() + 3
        while recognizer.loaded and monotonic() < deadline:
            sleep(0.02)
        self.assertFalse(recognizer.loaded)
        factory.assert_called_once()

    def test_known_whisper_hallucinations_are_discarded(self):
        recognizer = SpeechRecognizer(factory=lambda: self.fake_model("Subtítulos realizados por Amara.org"))
        self.assertEqual(recognizer.transcribe(b"ruido"), "")
        recognizer.unload()


class FakeSource:
    def __init__(self, chunks=(), error=None):
        self.chunks_to_send = list(chunks)
        self.error = error
        self.asked = []
        self.warmed = 0

    def chunks(self, text, stop):
        self.asked.append(text)
        yield from self.chunks_to_send
        if self.error:
            raise self.error

    def warm(self):
        self.warmed += 1


def collect(chunks, stop):
    collect.played.extend(chunks)


class SynthesizerTests(unittest.TestCase):
    def setUp(self):
        collect.played = []

    def synthesizer(self, *sources, fallback=None):
        self.fallback = fallback or Mock()
        return SpeechSynthesizer(sources=list(sources), play=collect, fallback=self.fallback)

    def test_streams_the_first_source_and_warms_its_connection(self):
        first, second = FakeSource(["a", "b"]), FakeSource(["z"])
        synthesizer = self.synthesizer(first, second)
        synthesizer.warm()
        synthesizer.speak("Volumen 40 %.", threading.Event())
        self.assertEqual(collect.played, ["a", "b"])
        self.assertEqual(first.asked, ["Volumen 40 por ciento."])
        self.assertEqual((first.warmed, second.asked), (1, []))
        self.fallback.assert_not_called()

    def test_falls_back_to_next_source_and_then_to_windows_voice(self):
        broken = FakeSource(error=OSError("sin red"))
        backup = FakeSource(["x"])
        self.synthesizer(broken, backup).speak("Hola", threading.Event())
        self.assertEqual(collect.played, ["x"])
        self.fallback.assert_not_called()
        self.synthesizer(FakeSource(error=OSError()), FakeSource(error=OSError())).speak("Hola", threading.Event())
        self.fallback.assert_called_once()
        self.assertEqual(self.fallback.call_args.args[0], "Hola")

    def test_failure_after_audio_started_does_not_repeat_the_answer(self):
        failing = FakeSource(["a"], error=ConnectionResetError())
        backup = FakeSource(["otra vez"])
        self.synthesizer(failing, backup).speak("Hola", threading.Event())
        self.assertEqual(collect.played, ["a"])
        self.assertEqual(backup.asked, [])
        self.fallback.assert_not_called()

    def test_stopped_speech_is_not_played(self):
        stop = threading.Event()
        stop.set()
        source = FakeSource(["a"])
        self.synthesizer(source).speak("Hola", stop)
        self.assertEqual((source.asked, collect.played), ([], []))

    def test_voice_speaks_in_background_and_can_be_interrupted(self):
        started, finished = threading.Event(), threading.Event()

        def slow_speak(text, stop):
            started.set()
            stop.wait(3)
            finished.set()

        voice = Voice(recognizer=Mock(), synthesizer=Mock(speak=slow_speak))
        voice.say("Hola")
        self.assertTrue(started.wait(2))
        self.assertTrue(voice.speaking)
        voice.stop_speaking()
        self.assertTrue(finished.wait(2))
        self.assertFalse(voice.speaking)

    def test_listen_returns_none_without_speech_and_transcribes_otherwise(self):
        recognizer = Mock(transcribe=Mock(return_value="hola"))
        voice = Voice(recognizer=recognizer, synthesizer=Mock(), record=lambda stop, on_level=None: None)
        self.assertIsNone(voice.listen(threading.Event()))
        recognizer.transcribe.assert_not_called()
        transcribing = Mock()
        voice = Voice(recognizer=recognizer, synthesizer=Mock(), record=lambda stop, on_level=None: b"a")
        self.assertEqual(voice.listen(threading.Event(), on_transcribing=transcribing), "hola")
        transcribing.assert_called_once_with()


class FakeVoice:
    def __init__(self, heard):
        self.heard = heard
        self.said = []
        self.speaking = False
        self.stops = 0

    def listen(self, stop, *, on_level=None, on_transcribing=None):
        if on_level:
            on_level(0.05)
        if on_transcribing:
            on_transcribing()
        return self.heard

    def say(self, text):
        self.said.append(text)

    def stop_speaking(self):
        self.stops += 1


class FakePreferences:
    def __init__(self, value="1"):
        self.values = {"voz.respuestas": value}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value


@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class DesktopVoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.audit = Audit(Path(temporary.name) / "auditoria.sqlite3")
        self.tools = Mock(spec=LocalTools)
        self.tools.read_usage.return_value = SystemUsage(7, 8 * 1024**3, 4 * 1024**3)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(release_tk, self)
        self.addCleanup(self.close)

    def close(self):
        try:
            self.window.close()
        except (AttributeError, tk.TclError):
            pass

    def make(self, voice, preferences=None):
        self.window = DesktopWindow(self.root, self.tools, self.audit, voice=voice, preferences=preferences)
        return self.window

    def wait_until(self, condition):
        deadline = monotonic() + 4
        while not condition():
            self.root.update()
            if monotonic() >= deadline:
                self.fail("La ventana no alcanzó el estado esperado.")
            sleep(0.01)

    def test_spoken_order_runs_and_the_answer_is_read_aloud(self):
        voice = FakeVoice("consulta CPU y RAM")
        window = self.make(voice, FakePreferences("1"))
        window.mic_button.invoke()
        self.wait_until(lambda: window.status.get() == "Completado")
        self.tools.read_usage.assert_called_once_with()
        self.assertEqual(len(voice.said), 1)
        self.assertIn("4.00 de 8.00 GiB", voice.said[0])

    def test_speaker_toggle_is_remembered_and_silences_answers(self):
        voice = FakeVoice("consulta CPU y RAM")
        preferences = FakePreferences("1")
        window = self.make(voice, preferences)
        window.speaker_button.invoke()
        self.assertEqual(preferences.values["voz.respuestas"], "0")
        window.mic_button.invoke()
        self.wait_until(lambda: window.status.get() == "Completado")
        self.assertEqual(voice.said, [])

    def test_nothing_heard_shows_a_hint_without_running_tools(self):
        window = self.make(FakeVoice(None), FakePreferences())
        window.mic_button.invoke()
        self.wait_until(lambda: window._listen is None)
        self.assertIn("No te escuché", window.output.get("1.0", "end"))
        self.assertEqual(self.tools.mock_calls, [])

    def test_without_voice_packages_the_buttons_are_disabled(self):
        window = self.make(None)
        self.assertEqual(str(window.mic_button["state"]), "disabled")
        self.assertEqual(str(window.speaker_button["state"]), "disabled")
        self.assertFalse(window.speak_enabled)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class VoiceSpeedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(release_tk, self)
        self.preferences = FakePreferences()
        self.voice = FakeVoice(None)
        self.window = DesktopWindow(self.root, Mock(spec=LocalTools), Audit(Path(temporary.name) / "a.sqlite3"),
                                    voice=self.voice, preferences=self.preferences)
        self.addCleanup(self.close)
        from jarvis import voice as voice_module

        self.addCleanup(voice_module.set_speed, voice_module.DEFAULT_SPEED)

    def close(self):
        try:
            self.window.close()
        except (AttributeError, tk.TclError):
            pass

    def say(self, text):
        self.window.command.set(text)
        self.window.submit()
        return self.window.output.get("1.0", "end").strip()

    def test_faster_slower_and_named_levels_change_the_real_rate_and_are_remembered(self):
        from jarvis.voice import voice_rate

        self.assertEqual(voice_rate(), "+10%")
        self.assertIn("velocidad rápida", self.say("Jarvis, habla más rápido"))
        self.assertEqual((voice_rate(), self.preferences.values["voz.velocidad"]), ("+20%", "rápida"))
        self.say("velocidad muy rápida")
        self.assertIn("tan rápido como puedo", self.say("habla más rápido"))
        self.say("habla más despacio")
        self.assertEqual(voice_rate(), "+20%")
        self.say("velocidad lenta")
        self.assertEqual(voice_rate(), "-5%")
        self.assertEqual(self.voice.said[-1], "Entendido, señor. Hablaré a velocidad lenta.")
