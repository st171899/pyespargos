"""Synthetic coherent-motion checks; no radio or Qt application required."""
import sys
from pathlib import Path
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from doppler import DopplerHistory, strongest_horizontal_power, C
from beamforming import horizontal_power


class DopplerTest(unittest.TestCase):
    def history(self, n=64, stride=2048, tone_bin=5, static=0):
        history = DopplerHistory(n)
        for k in range(n):
            trace = np.zeros((2, 256), np.complex64)
            trace[:, 31] = static + np.exp(2j*np.pi*tone_bin*k/n)
            history.append(k*stride, trace, stride)
        return history

    def test_strongest_beam_equals_direct_steering(self):
        rng = np.random.default_rng(17)
        for columns in (1, 4):
            samples = rng.normal(size=(19, 2, columns, 23)) + 1j*rng.normal(size=(19, 2, columns, 23))
            expected = np.asarray([horizontal_power(frame, 2.4e9, 0.06).max(axis=0) for frame in samples])
            np.testing.assert_allclose(strongest_horizontal_power(samples, 2.4e9, 0.06), expected, rtol=1e-6)

    def test_velocity_sign_resolution_and_range(self):
        for stride in (1024, 8192, 32768):
            history = self.history(stride=stride)
            velocity, spectrum = history.spectrum(80e6, 2.4e9)
            peak = np.unravel_index(np.argmax(abs(spectrum)), spectrum.shape)
            self.assertEqual(peak[-1], 31)
            dt = stride*256/80e6
            expected = -5*C/(2*2.4e9*64*dt)
            self.assertAlmostEqual(velocity[peak[0]], expected)
            self.assertAlmostEqual(np.diff(velocity)[0], C/(2*2.4e9*128*dt))
            self.assertAlmostEqual(abs(spectrum[peak]), 1, places=6)

    def test_partial_history_starts_at_eight_with_honest_resolution(self):
        history = DopplerHistory(64)
        for k in range(8):
            history.append(k*2048, np.ones((2, 256), np.complex64), 2048)
            if k < 7:
                self.assertIsNone(history.spectrum(80e6, 2.4e9, allow_partial=True))
        self.assertFalse(history.ready)
        self.assertIsNone(history.spectrum(80e6, 2.4e9))
        v, spectrum = history.spectrum(80e6, 2.4e9, allow_partial=True)
        self.assertEqual(spectrum.shape, (16, 2, 256))
        dt = 2048*256/80e6
        self.assertAlmostEqual(np.diff(v)[0]*len(v), C/(2*2.4e9*dt))
        self.assertAlmostEqual(abs(spectrum[np.argmin(abs(v)), 0, 0]), 1)

    def test_zero_suppression_keeps_moving_echo(self):
        history = self.history(static=100)
        velocity, spectrum = history.spectrum(80e6, 2.4e9, suppress=True)
        dc = np.argmin(abs(velocity))
        self.assertTrue(np.all(spectrum[dc] == 0))
        peak = np.unravel_index(np.argmax(abs(spectrum)), spectrum.shape)
        self.assertLess(velocity[peak[0]], 0)
        self.assertAlmostEqual(abs(spectrum[peak]), 1, places=5)
        static = self.history(tone_bin=0, static=100)
        self.assertLess(abs(static.spectrum(80e6, 2.4e9, True)[1]).max(), 1e-10)
        self.assertLess(abs(static.residual()).max(), 1e-10)

    def test_gap_cadence_shape_and_duplicate_handling(self):
        history = self.history()
        frame = history.frames[-1]
        self.assertFalse(history.append(history.indices[-1], frame, 2048))
        self.assertTrue(history.ready)
        history.append(history.indices[-1]+4096, frame, 2048)
        self.assertFalse(history.ready)
        self.assertEqual(len(history.frames), 1)
        history.append(history.indices[-1]+4096, frame, 4096)
        self.assertEqual(len(history.frames), 1)
        history.append(history.indices[-1]+4096, frame[:1], 4096)
        self.assertEqual(len(history.frames), 1)

    def test_gapped_clutter_mean_preserves_history_and_new_motion(self):
        history = DopplerHistory(64, uniform=False)
        static = np.full((8, 256), 100+30j, np.complex64)
        for i in (17, 18, 8209, 16403, 32785):
            history.append(i, static, 1)
        self.assertEqual(len(history.frames), 5)
        np.testing.assert_allclose(history.latest_residual(), 0)
        moving = static.copy()
        moving[:, 23] += 20j
        history.append(40977, moving, 1)
        np.testing.assert_allclose(history.latest_residual()[:, 23], 20j*5/6, rtol=1e-6)
        with self.assertRaisesRegex(ValueError, 'not a uniform'):
            history.spectrum(80e6, 2.4e9)

    def test_partial_clutter_frames_do_not_learn_missing_antennas_as_zero(self):
        history = DopplerHistory(64, uniform=False)
        first = np.full((2,256), np.nan+1j*np.nan, np.complex64)
        first[0] = 100+30j
        history.append(17, first, 1)
        self.assertTrue(history.append(17, np.full((2,256),100+30j,np.complex64), 1))
        for i in (8209,16403):
            partial = first.copy()
            partial[1] = 200+60j
            partial[0] = np.nan+1j*np.nan
            history.append(i,partial,1)
        residual = history.latest_residual()
        self.assertTrue(np.isnan(residual[0]).all())
        # Antenna 1 has three real observations, no zero-filled missing samples.
        np.testing.assert_allclose(residual[1], (200+60j)-(500+150j)/3,rtol=1e-6)

    def test_accumulation_boxcar_null_and_nyquist_loss(self):
        # One lane averages M repetitions spaced V chunks apart. A full
        # phase revolution across that window disappears before host FFT.
        fs, frequency, interval, vector = 80e6, 2.4e9, 4096, 4
        m = interval//vector
        dt_repeat = vector*256/fs
        window = interval*256/fs
        for cycles, expected in ((0, 1), (0.5, 2/np.pi), (1, 0)):
            fd = cycles/window
            measured = abs(np.exp(2j*np.pi*fd*np.arange(m)*dt_repeat).mean())
            self.assertAlmostEqual(measured, expected, places=5)
        self.assertAlmostEqual(C/(4*frequency*window), 2.3825363953908285)


if __name__ == '__main__':
    unittest.main()
