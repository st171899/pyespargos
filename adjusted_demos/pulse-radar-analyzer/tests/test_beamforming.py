"""Physical-direction and Fourier equivalence checks for conventional beams."""

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from beamforming import AZIMUTH_DEG, ELEVATION_DEG, SPEED_OF_LIGHT, planar_power


class BeamformingTest(unittest.TestCase):
    def test_plane_waves_and_analytic_azimuth_cuts(self):
        row, column = np.indices((2, 4))
        for frequency in (2.31e9, 2.437e9, 2.68e9):
            for azimuth, elevation in ((0, 0), (-25, 30), (40, -20), (15, 55)):
                with self.subTest(frequency=frequency, azimuth=azimuth, elevation=elevation):
                    kd = 2 * np.pi * frequency * 0.06 / SPEED_OF_LIGHT
                    az, el = np.deg2rad([azimuth, elevation])
                    phase = kd * (column * np.cos(el) * np.sin(az) + row * np.sin(el))
                    samples = np.exp(1j * phase)[..., None]
                    power = planar_power(samples, frequency, 0.06)
                    iy, ix = np.unravel_index(np.argmax(power), power.shape)
                    self.assertEqual(AZIMUTH_DEG[ix], azimuth)
                    self.assertEqual(ELEVATION_DEG[iy], elevation)
                    self.assertAlmostEqual(power[iy, ix], 1.0)
                    # Exact uniformly weighted ULA cut: no masked FFT bins,
                    # smoothing or interpolation should alter this response.
                    delta = kd * np.cos(el) * (np.sin(az) - np.sin(np.deg2rad(AZIMUTH_DEG)))
                    expected = abs(np.mean(np.exp(1j * delta[:, None] * np.arange(4)), axis=1)) ** 2
                    np.testing.assert_allclose(power[iy], expected, atol=1e-12)

    def test_matches_direct_coherent_sum_and_spatial_fft(self):
        rng = np.random.default_rng(7)
        samples = rng.normal(size=(2, 4, 24)) + 1j * rng.normal(size=(2, 4, 24))
        frequency = SPEED_OF_LIGHT / (2 * 0.06)
        power = planar_power(samples, frequency, 0.06)
        row, column = np.indices((2, 4))
        for azimuth, elevation in ((-30, 0), (30, 0), (20, 40)):
            az, el = np.deg2rad([azimuth, elevation])
            weights = np.exp(-1j * np.pi * (column * np.cos(el) * np.sin(az) + row * np.sin(el)))
            beam = np.sum(samples * weights[..., None], axis=(0, 1)) / 8
            iy = np.flatnonzero(ELEVATION_DEG == elevation)[0]
            ix = np.flatnonzero(AZIMUTH_DEG == azimuth)[0]
            self.assertAlmostEqual(power[iy, ix], np.mean(abs(beam) ** 2))
        fft_power = np.mean(abs(np.fft.fft2(samples, axes=(0, 1))) ** 2, axis=2) / 64
        # At half-wave spacing, azimuth +/-30 degrees maps to FFT +/-1/4.
        self.assertAlmostEqual(power[90, 240], fft_power[0, 1])
        self.assertAlmostEqual(power[90, 120], fft_power[0, 3])


if __name__ == "__main__":
    unittest.main()
