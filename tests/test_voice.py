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

    def test_hears_someone_already_talking_when_it_starts(self):
        # Sin silencio inicial el umbral antiguo (3,5 veces lo más bajo) ignoraba toda la frase.
        audio = record_until_silence(threading.Event(), blocks=self.blocks("#" * 12 + "-" * 30))
        self.assertEqual(len(audio), 1600 * (12 + 10))

    def test_the_chime_is_not_taken_for_speech(self):
        audio = record_until_silence(threading.Event(), blocks=self.blocks("##" + "-" * 15 + "#" * 6 + "-" * 30),
                                     noise=0.002, deaf_seconds=0.4)
        self.assertEqual(len(audio), 1600 * (2 + 15 + 6 + 10))
        self.assertIsNone(record_until_silence(threading.Event(), blocks=self.blocks("##" + "-" * 80),
                                               noise=0.002, deaf_seconds=0.4))

    def test_stop_ends_early_and_keeps_what_was_said(self):
        stop = threading.Event()

        def source():
            for index, block in enumerate(self.blocks("---" + "#" * 50)):
                if index == 6:
                    stop.set()
                yield block

        audio = record_until_silence(stop, blocks=source())
        self.assertEqual(len(audio), 1600 * 7)


    def test_reports_the_first_pause_and_when_speech_resumes(self):
        events = []
        audio = record_until_silence(
            threading.Event(), blocks=self.blocks("---" + "#" * 5 + "-" * 6 + "#" * 4 + "-" * 30),
            on_pause=lambda heard: events.append(("pausa", len(heard) // 1600)),
            on_resume=lambda: events.append("sigue"),
        )
        self.assertEqual(events, [("pausa", 12), "sigue", ("pausa", 22)])
        self.assertEqual(len(audio), 1600 * 28)


class EarlyTranscriptionTests(unittest.TestCase):
    def listen(self, script, transcribe):
        recognizer = Mock(loaded=True)
        recognizer.transcribe.side_effect = transcribe

        def record(stop, *, on_level, on_pause, on_resume):
            return script(on_pause, on_resume)

        text = Voice(recognizer=recognizer, synthesizer=Mock(), record=record).listen(threading.Event())
        return text, [call.args[0] for call in recognizer.transcribe.call_args_list]

    def test_reuses_the_text_started_during_the_pause(self):
        text, calls = self.listen(lambda pause, resume: pause("abre") or "abre y silencio", lambda audio: f"[{audio}]")
        self.assertEqual((text, calls), ("[abre]", ["abre"]))  # una sola transcripción

    def test_discards_it_if_the_user_keeps_talking(self):
        def script(pause, resume):
            pause("abre")
            resume()
            return "abre discord"

        text, calls = self.listen(script, lambda audio: f"[{audio}]")
        self.assertEqual(text, "[abre discord]")
        self.assertEqual(calls[-1], "abre discord")

    def test_discarded_pauses_do_not_pile_up(self):
        release = threading.Event()

        def transcribe(audio):
            if audio == "uno":
                release.wait(3)
            return f"[{audio}]"

        def script(pause, resume):
            pause("uno")
            sleep(0.1)  # «uno» ya está en marcha y no se puede interrumpir
            for audio in ("dos", "tres"):
                resume()
                pause(audio)
            release.set()
            return "final"

        text, calls = self.listen(script, transcribe)
        self.assertEqual((text, calls), ("[tres]", ["uno", "tres"]))  # «dos» nunca empezó

    def test_falls_back_when_the_early_attempt_fails(self):
        attempts = []

        def transcribe(audio):
            attempts.append(audio)
            if len(attempts) == 1:
                raise RuntimeError("fallo")
            return "bien"

        self.assertEqual(self.listen(lambda pause, resume: pause("a") or "a", transcribe)[0], "bien")

    def test_an_empty_result_is_silence(self):
        text, calls = self.listen(lambda pause, resume: pause("ruido") or "ruido", lambda audio: "")
        self.assertIsNone(text)
        self.assertEqual(len(calls), 1)


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
        voice = Voice(recognizer=recognizer, synthesizer=Mock(), record=lambda stop, **options: None)
        self.assertIsNone(voice.listen(threading.Event()))
        recognizer.transcribe.assert_not_called()
        transcribing = Mock()
        voice = Voice(recognizer=recognizer, synthesizer=Mock(), record=lambda stop, **options: b"a")
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

        self.assertEqual(voice_rate(), "+25%")
        self.assertIn("velocidad rápida", self.say("Jarvis, habla más rápido"))
        self.assertEqual((voice_rate(), self.preferences.values["voz.velocidad"]), ("+35%", "rápida"))
        self.say("velocidad muy rápida")
        self.assertIn("tan rápido como puedo", self.say("habla más rápido"))
        self.say("habla más despacio")
        self.assertEqual(voice_rate(), "+35%")
        self.say("velocidad lenta")
        self.assertEqual(voice_rate(), "+5%")
        self.assertEqual(self.voice.said[-1], "Entendido, señor. Hablaré a velocidad lenta.")


class ConfirmationWordsTests(unittest.TestCase):
    def test_yes_no_and_unclear(self):
        from jarvis.text import confirmation

        for text in ("Sí", "sí, adelante", "claro que sí, Jarvis", "dale", "hazlo por favor", "de acuerdo",
                     "por supuesto"):
            self.assertIs(confirmation(text), True, text)
        for text in ("No", "no, mejor no", "Cancela", "mejor no", "eh... no sé"):
            self.assertIs(confirmation(text), False, text)
        for text in ("qué hora es", "", None):
            self.assertIsNone(confirmation(text), text)


class ScriptedVoice(FakeVoice):
    """Voz falsa que habla durante unos ciclos y luego «oye» las respuestas indicadas."""

    def __init__(self, answers):
        super().__init__(None)
        self.answers = list(answers)
        self.listens = 0
        self._speaking_polls = 0

    @property
    def speaking(self):
        if self._speaking_polls > 0:
            self._speaking_polls -= 1
            return True
        return False

    @speaking.setter
    def speaking(self, value):
        pass

    def say(self, text):
        self.said.append(text)
        self._speaking_polls = 3

    def listen(self, stop, *, on_level=None, on_transcribing=None):
        self.listens += 1
        return self.answers.pop(0) if self.answers else None


@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class FollowUpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(release_tk, self)
        self.tools = Mock(spec=LocalTools)
        self.audit = Audit(Path(temporary.name) / "a.sqlite3")
        chime = unittest.mock.patch("jarvis.desktop._wake_chime")
        chime.start()
        self.addCleanup(chime.stop)

    def make(self, answers, brain=None):
        self.voice = ScriptedVoice(answers)
        self.window = DesktopWindow(self.root, self.tools, self.audit, brain, self.voice, FakePreferences())
        self.addCleanup(self.close)
        return self.window

    def close(self):
        try:
            self.window.close()
        except (AttributeError, tk.TclError):
            pass

    def wait_until(self, condition, seconds=4):
        deadline = monotonic() + seconds
        while not condition():
            self.root.update()
            if monotonic() >= deadline:
                self.fail("La ventana no alcanzó el estado esperado.")
            sleep(0.01)

    def test_after_a_question_it_listens_for_the_answer_without_wake_word(self):
        from jarvis.brain import Decision

        brain = Mock()
        brain.decide.side_effect = [Decision(None, {}, "¿Desea que abra Netflix, señor?"),
                                    Decision("web.open", {"site": "netflix"}, "Enseguida, señor.")]
        window = self.make(["sí, por favor"], brain)
        window.command.set("estoy aburrido")
        window.submit()
        self.wait_until(lambda: self.tools.open_url.called and window._task is None)
        self.assertEqual(brain.decide.call_args_list[1].args[0], "sí, por favor")
        self.assertEqual(self.voice.listens, 1)

    def test_due_reminder_is_spoken_and_shown_once_the_window_is_free(self):
        from datetime import datetime, timedelta

        from jarvis.memory import MemoryStore

        store = MemoryStore(self.audit.path.parent / "m.sqlite3")
        store.add_reminder("sacar la ropa", datetime.now() - timedelta(seconds=1), "recordatorio")
        window = self.make([])
        window.floating_orb = Mock()
        self.voice.say = Mock()
        with unittest.mock.patch("jarvis.desktop._reminder_chime") as chime:
            window.enable_reminders(store)
            window._listen = Mock()  # ocupado: el aviso espera
            window.root.after_cancel(window._reminders_after)
            window._poll_reminders()
            self.assertEqual(len(store.pending_reminders()), 1)
            window._listen = None
            window.root.after_cancel(window._reminders_after)
            window._poll_reminders()
            self.wait_until(lambda: self.voice.say.called)
        self.voice.say.assert_called_once_with("Señor, le recuerdo: sacar la ropa.")
        self.assertIn("sacar la ropa", window.output.get("1.0", "end"))
        window.floating_orb.show.assert_called()
        chime.assert_called_once_with(False)
        self.assertEqual(store.pending_reminders(), [])
        window.floating_orb = None

    def test_silence_after_a_question_closes_the_microphone_quietly(self):
        from jarvis.brain import Decision

        brain = Mock()
        brain.decide.return_value = Decision(None, {}, "¿Algo más, señor?")
        window = self.make([], brain)
        window.command.set("hola")
        window.submit()
        self.wait_until(lambda: self.voice.listens == 1 and window._listen is None)
        self.assertEqual(window.status.get(), "Listo")
        self.assertIn("¿Algo más, señor?", window.output.get("1.0", "end"))

    def test_approvals_can_be_answered_by_voice(self):
        from jarvis.tools import VolumeStatus

        self.tools.set_volume.return_value = VolumeStatus(50, False)
        window = self.make(["sí, adelante"])
        window.command.set("pon el volumen al 50 %")
        window.submit()
        self.wait_until(lambda: self.tools.set_volume.called)
        self.assertIn("¿Lo confirma, señor?", self.voice.said[0])
        self.wait_until(lambda: window._task is None)
        self.assertEqual(window.status.get(), "Completado")

    def test_a_spoken_no_rejects_and_unclear_answers_are_asked_again(self):
        window = self.make(["qué hora es", "no, mejor no"])
        window.command.set("pon el volumen al 50 %")
        window.submit()
        self.wait_until(lambda: window._task is None, seconds=6)
        self.tools.set_volume.assert_not_called()
        self.assertIn("Disculpe, señor, ¿sí o no?", self.voice.said)
        self.assertEqual(window.status.get(), "Detenido")
