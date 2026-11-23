import hashlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import Mock, patch

from jarvis import wake
from jarvis.wake import WakeWordListener, WakeWordUnavailable, ensure_models


def release_tk(test):
    import gc

    for name in ("window", "root"):
        if hasattr(test, name):
            setattr(test, name, None)
    gc.collect()


class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "modelos"
        self.content = {"a.onnx": b"modelo a", "b.onnx": b"modelo b"}
        digests = {name: hashlib.sha256(data).hexdigest() for name, data in self.content.items()}
        patcher = patch.dict(wake.MODELS, digests, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_downloads_missing_models_once_and_verifies_them(self):
        fetched = []

        def download(url, target):
            fetched.append(url)
            target.write_bytes(self.content[url.rsplit("/", 1)[1]])

        ensure_models(self.folder, download=download)
        ensure_models(self.folder, download=download)
        self.assertEqual(len(fetched), 2)
        self.assertTrue(all(url.startswith("https://github.com/dscripka/openWakeWord/") for url in fetched))

    def test_rejects_altered_downloads(self):
        def tampered(url, target):
            target.write_bytes(b"otra cosa")

        with self.assertRaises(WakeWordUnavailable):
            ensure_models(self.folder, download=tampered)
        self.assertEqual(list(self.folder.glob("*.onnx")), [])


class FakeModel:
    def __init__(self, scores):
        self.scores = list(scores)
        self.resets = 0

    def score(self, chunk):
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self):
        self.resets += 1


class ListenerTests(unittest.TestCase):
    def listener(self, scores, *, chunk_count=30, delay=0.0):
        self.model = FakeModel(scores)
        self.heard = threading.Event()
        self.wakes = 0

        def on_wake():
            self.wakes += 1
            self.heard.set()

        def chunks(stop):
            for _ in range(chunk_count):
                if stop.is_set():
                    return
                if delay:
                    sleep(delay)
                yield b""
            stop.wait(0.5)

        listener = WakeWordListener(Path("."), on_wake, model_factory=lambda folder: self.model,
                                    chunks=chunks)
        patcher = patch.object(wake, "ensure_models", side_effect=lambda folder: folder)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(listener.stop)
        return listener

    def test_wakes_once_per_phrase(self):
        listener = self.listener([0.1, 0.9, 0.95, 0.97, 0.2])
        listener.start()
        self.assertTrue(self.heard.wait(2))
        sleep(0.2)
        self.assertEqual(self.wakes, 1)  # los puntajes altos seguidos son la misma frase

    def test_paused_listener_ignores_the_microphone(self):
        listener = self.listener([0.0] * 5 + [0.99] * 40, delay=0.02)
        listener.pause()
        listener.start()
        sleep(0.4)
        self.assertEqual(self.wakes, 0)
        listener.resume()
        self.assertTrue(self.heard.wait(3))

    def test_reports_load_errors(self):
        listener = WakeWordListener(Path("."), Mock(), model_factory=Mock(side_effect=OSError("sin modelo")))
        with patch.object(wake, "ensure_models", side_effect=WakeWordUnavailable("sin red")):
            listener.start()
            listener._thread.join(2)
        self.assertEqual(listener.error, "sin red")
        self.assertFalse(listener.running)


try:
    import tkinter as tk
    from jarvis.desktop import DesktopWindow
except ImportError:
    tk = None


class FakeListener:
    instances = []

    def __init__(self, folder, on_wake):
        self.on_wake = on_wake
        self.calls = []
        self.error = None
        FakeListener.instances.append(self)

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")

    def pause(self):
        self.calls.append("pause")

    def resume(self):
        self.calls.append("resume")


class Preferences(dict):
    def get_setting(self, key, default=None):
        return self.get(key, default)

    def set_setting(self, key, value):
        self[key] = value


@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class DesktopWakeTests(unittest.TestCase):
    def setUp(self):
        from jarvis.audit import Audit
        from jarvis.tools import LocalTools

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"Sesión gráfica no disponible: {error}")
        self.root.withdraw()
        self.addCleanup(release_tk, self)
        self.voice = Mock(speaking=False)
        self.voice.listen.return_value = None
        self.preferences = Preferences()
        self.window = DesktopWindow(self.root, Mock(spec=LocalTools), Audit(Path(temporary.name) / "a.sqlite3"),
                                    voice=self.voice, preferences=self.preferences)
        self.addCleanup(self.close)
        FakeListener.instances.clear()
        patcher = patch("jarvis.wake.WakeWordListener", FakeListener)
        patcher.start()
        self.addCleanup(patcher.stop)
        chime = patch("jarvis.desktop._wake_chime")
        self.chime = chime.start()
        self.addCleanup(chime.stop)

    def close(self):
        try:
            self.window.close()
        except (AttributeError, tk.TclError):
            pass

    def wait_until(self, condition):
        deadline = monotonic() + 4
        while not condition():
            self.root.update()
            if monotonic() >= deadline:
                self.fail("La ventana no alcanzó el estado esperado.")
            sleep(0.01)

    def test_hearing_hey_jarvis_starts_listening_and_frees_the_microphone(self):
        self.window.enable_wake_word(Path("."))
        listener = FakeListener.instances[0]
        self.assertIn("Hey Jarvis", self.window.hint["text"])
        listener.on_wake()
        self.wait_until(lambda: self.voice.listen.called and self.window._listen is None)
        self.chime.assert_called_once_with()
        self.assertEqual(listener.calls, ["start", "pause", "resume"])

    def test_spoken_toggle_turns_it_off_and_is_remembered(self):
        self.window.enable_wake_word(Path("."))
        listener = FakeListener.instances[0]
        self.window.command.set("Desactiva Hey Jarvis")
        self.window.submit()
        self.assertEqual(self.preferences["voz.activacion"], "0")
        self.assertIn("stop", listener.calls)
        self.assertNotIn("Hey Jarvis", self.window.hint["text"])
        self.window.command.set("activa hey jarvis")
        self.window.submit()
        self.assertEqual(self.preferences["voz.activacion"], "1")
        self.assertEqual(len(FakeListener.instances), 2)

    def test_sensitivity_can_be_changed_by_voice_and_is_remembered(self):
        self.window.enable_wake_word(Path("."))
        listener = FakeListener.instances[0]
        self.assertEqual(listener.threshold, 0.22)
        self.window.command.set("Sensibilidad alta")
        self.window.submit()
        self.assertEqual((listener.threshold, self.preferences["voz.sensibilidad"]), (0.15, "alta"))
        self.window.disable_wake_word()
        self.window.enable_wake_word(Path("."))
        self.assertEqual(FakeListener.instances[-1].threshold, 0.15)

    def test_stays_off_when_disabled_or_without_voice(self):
        self.preferences["voz.activacion"] = "0"
        self.window.enable_wake_word(Path("."))
        self.assertEqual(FakeListener.instances, [])
        self.window.voice = None
        self.preferences["voz.activacion"] = "1"
        self.window.enable_wake_word(Path("."))
        self.assertEqual(FakeListener.instances, [])


if __name__ == "__main__":
    unittest.main()
