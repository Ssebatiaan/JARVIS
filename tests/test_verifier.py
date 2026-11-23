import os as _os
import tempfile as _tempfile

# El registro de diagnóstico de las pruebas va a una carpeta temporal, no al proyecto.
_os.environ["JARVIS_DIAGNOSTICO_ACTIVACION"] = _os.path.join(_tempfile.gettempdir(), "jarvis_pruebas_diagnostico.log")
import importlib.util
import os
import tempfile
import threading
import unittest
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import Mock, patch

from jarvis import wake

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


def release_tk(test):
    import gc

    for name in ("window", "root"):
        if hasattr(test, name):
            setattr(test, name, None)
    gc.collect()


class FakeModel:
    """Modelo de prueba: la «ventana» es la media del último bloque, repetida."""

    def __init__(self):
        import numpy as np

        self._np = np
        self.reset()

    def reset(self):
        self._last = 0.0
        self._predictions = 0

    def score(self, chunk):
        self._last = float(self._np.mean(chunk)) / 1000
        self._predictions += 1
        return 0.0

    def window(self):
        return self._np.full((2, 3), self._last, dtype=self._np.float32)


@unittest.skipUnless(HAS_NUMPY, "Requiere numpy (entorno .venv)")
class VerifierTests(unittest.TestCase):
    def setUp(self):
        import numpy as np

        self.np = np
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_learns_to_separate_and_survives_save_and_load(self):
        np = self.np
        rng = np.random.default_rng(0)
        positives = rng.normal(2.0, 0.3, (40, 2, 3))
        negatives = rng.normal(-1.0, 0.3, (200, 2, 3))
        verifier = wake.PersonalVerifier.fit(positives, negatives, threshold=0.8)
        self.assertGreater(verifier.probability(np.full((2, 3), 2.0)), 0.9)
        self.assertLess(verifier.probability(np.full((2, 3), -1.0)), 0.1)
        path = self.folder / wake.VERIFIER_FILE
        verifier.save(path)
        loaded = wake.PersonalVerifier.load(path)
        self.assertEqual(loaded.threshold, 0.8)
        self.assertAlmostEqual(loaded.probability(np.full((2, 3), 2.0)), verifier.probability(np.full((2, 3), 2.0)))

    def speech(self, level, seconds=1.0):
        np = self.np
        quiet = np.full(int(0.5 * 16000), 1, dtype=np.int16)
        loud = (np.random.default_rng(1).standard_normal(int(seconds * 16000)) * 0 + level).astype(np.int16)
        return np.concatenate([quiet, loud, quiet])

    def test_trains_with_reference_voices_cached_and_reports_detections(self):
        positives = [self.speech(3000) for _ in range(3)]
        negatives = [self.speech(-800, seconds=3)]
        synthesize = Mock(side_effect=lambda text, voice: self.speech(-500))
        report = wake.train_personal_verifier(self.folder, positives, negatives, model=FakeModel(),
                                              synthesize=synthesize)
        self.assertEqual(report, wake.EnrollmentReport(3, 3, True))
        self.assertTrue((self.folder / wake.VERIFIER_FILE).exists())
        calls = synthesize.call_count
        self.assertEqual(calls, len(wake.GENERIC_VOICES) * len(wake.GENERIC_TEXTS))
        wake.train_personal_verifier(self.folder, positives, negatives, model=FakeModel(), synthesize=synthesize)
        self.assertEqual(synthesize.call_count, calls)  # las voces de referencia quedan guardadas
        self.assertTrue(wake.forget_personal_verifier(self.folder))
        self.assertFalse(wake.forget_personal_verifier(self.folder))

    def test_without_internet_it_trains_in_strict_mode(self):
        report = wake.train_personal_verifier(
            self.folder, [self.speech(3000)], [self.speech(-800, 3)], model=FakeModel(),
            synthesize=Mock(side_effect=OSError("sin red")))
        self.assertFalse(report.with_references)
        verifier = wake.PersonalVerifier.load(self.folder / wake.VERIFIER_FILE)
        self.assertEqual(verifier.threshold, wake.OFFLINE_VERIFIER_THRESHOLD)

    def test_listener_uses_the_personal_verifier_when_present(self):
        np = self.np
        positives = np.full((30, 2, 3), 3.0)
        negatives = np.full((30, 2, 3), -1.0)
        wake.PersonalVerifier.fit(positives, negatives).save(self.folder / wake.VERIFIER_FILE)
        heard = threading.Event()

        def chunks(stop):
            for value in [-1000] * 8 + [3000] * 3 + [-1000] * 20:
                yield np.full(1280, value, dtype=np.int16)
            stop.wait(1)

        listener = wake.WakeWordListener(self.folder, heard.set, model_factory=lambda folder: FakeModel(),
                                         chunks=chunks)
        with patch.object(wake, "ensure_models", side_effect=lambda folder: folder):
            listener.start()
            self.assertTrue(heard.wait(3))
        listener.stop()
        self.assertTrue(listener.personal)


try:
    import tkinter as tk
    from jarvis.desktop import DesktopWindow
except ImportError:
    tk = None


@unittest.skipUnless(os.name == "nt" and tk is not None, "Requiere Windows con Tkinter")
class EnrollmentFlowTests(unittest.TestCase):
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
        self.window = DesktopWindow(self.root, Mock(spec=LocalTools), Audit(Path(temporary.name) / "a.sqlite3"),
                                    voice=self.voice)
        self.window._wake_folder = Path(temporary.name)
        self.window._wake = Mock(personal=False, error=None)
        self.addCleanup(self.close)
        for target in ("jarvis.desktop._enroll_beep", "time.sleep"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def close(self):
        try:
            self.window._wake = None
            self.window.close()
        except (AttributeError, tk.TclError):
            pass

    def wait_until(self, condition):
        deadline = monotonic() + 5
        while not condition():
            self.root.update()
            if monotonic() >= deadline:
                self.fail("La ventana no alcanzó el estado esperado.")
            sleep(0.01)

    def test_guides_the_recording_trains_and_reloads_the_detector(self):
        recordings = []
        self.window.enroll_recorder = lambda seconds: recordings.append(seconds) or b"audio"
        trainer = Mock(return_value=wake.EnrollmentReport(6, 6, True))
        self.window.enroll_trainer = trainer
        self.window.command.set("Aprende mi voz")
        self.window.submit()
        self.wait_until(lambda: self.window.status.get() == "Completado")
        self.assertEqual(recordings, [wake.ENROLL_SECONDS] * 6 + [wake.READING_SECONDS])
        positives, negatives = trainer.call_args.args[1:3]
        self.assertEqual((len(positives), len(negatives)), (6, 1))
        self.assertIn("reconocí 6 de 6", self.window.output.get("1.0", "end"))
        self.window._wake.pause.assert_called_once_with()
        self.window._wake.resume.assert_called_once_with()
        self.window._wake.reload_verifier.assert_called_once_with()

    def test_escape_cancels_without_training(self):
        started = threading.Event()

        def slow_recorder(seconds):
            started.set()
            self.window._enrollment.cancelled.wait(2)
            return b"audio"

        self.window.enroll_recorder = slow_recorder
        self.window.enroll_trainer = Mock()
        self.window.command.set("aprende mi voz")
        self.window.submit()
        self.wait_until(started.is_set)
        self.window._escape()
        self.wait_until(lambda: self.window.status.get() == "Cancelado")
        self.window.enroll_trainer.assert_not_called()
        self.window._wake.resume.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
