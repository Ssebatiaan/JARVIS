import importlib.util
import unittest

HAS_NUMPY = importlib.util.find_spec("numpy") is not None


@unittest.skipUnless(HAS_NUMPY, "Requiere numpy")
class ClapDetectorTests(unittest.TestCase):
    def setUp(self):
        import numpy as np

        self.np = np
        self.rng = np.random.default_rng(3)

    def silence(self, seconds, level=0.002):
        return self.rng.standard_normal(int(seconds * 16000)) * level

    def clap(self, amplitude=0.5):
        np = self.np
        t = np.arange(int(0.06 * 16000)) / 16000
        return self.rng.standard_normal(len(t)) * amplitude * np.exp(-t / 0.012)

    def hum(self, seconds, amplitude=0.2):
        """Sonido sostenido y grave, parecido a una vocal."""
        np = self.np
        t = np.arange(int(seconds * 16000)) / 16000
        return amplitude * np.sin(2 * np.pi * 180 * t) * (1 + 0.3 * np.sin(2 * np.pi * 4 * t))

    def wakes(self, *parts):
        from jarvis.clap import ClapDetector

        audio = self.np.clip(self.np.concatenate(parts) * 32768, -32768, 32767).astype(self.np.int16)
        detector = ClapDetector()
        return sum(detector.feed(audio[i:i + 1280]) for i in range(0, len(audio) - 1280, 1280))

    def test_two_claps_wake_once(self):
        s, c = self.silence, self.clap
        self.assertEqual(self.wakes(s(0.6), c(), s(0.3), c(0.45), s(0.6)), 1)
        self.assertEqual(self.wakes(s(0.6), c(0.15), s(0.5), c(0.12), s(0.6)), 1)

    def test_single_claps_far_claps_noise_and_sustained_sounds_do_not(self):
        s, c = self.silence, self.clap
        self.assertEqual(self.wakes(s(0.6), c(), s(1.2)), 0)
        self.assertEqual(self.wakes(s(0.6), c(), s(1.2), c(), s(0.6)), 0)
        self.assertEqual(self.wakes(s(2.0, 0.03)), 0)
        self.assertEqual(self.wakes(s(0.6), self.hum(0.5), s(0.3), self.hum(0.5), s(0.6)), 0)

    def test_claps_in_the_middle_of_ongoing_sound_do_not_wake(self):
        s, c = self.silence, self.clap
        self.assertEqual(self.wakes(s(0.6), self.hum(0.4, 0.3), c(), s(0.3), c(), self.hum(0.6, 0.3), s(0.4)), 0)


if __name__ == "__main__":
    unittest.main()
