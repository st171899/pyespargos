"""Hardware-free checks for pulse-start indexing and full-aperture beamforming."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

DEMO_DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pulse_radar_analyzer", DEMO_DIR / "pulse-radar-analyzer.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Analyzer = module.EspargosPulseRadarAnalyzer


class RadarProcessingTest(unittest.TestCase):
    def test_matched_filters_report_pulse_start_and_preserve_phase(self):
        rng = np.random.default_rng(42)
        for length in (15, 16, 64):
            reference = np.exp(1j * rng.uniform(-np.pi, np.pi, length)).astype(np.complex64)
            backend = SimpleNamespace(
                _tx_waveform=reference,
                _current_ping_config=lambda: {"pings_per_256": 2, "chirp_frames": length},
            )
            for delay in (0, 40, 256 - length):
                with self.subTest(length=length, delay=delay):
                    received = np.zeros(256, dtype=np.complex64)
                    gain = 0.7 * np.exp(0.6j)
                    received[delay : delay + length] = gain * reference
                    spatial = Analyzer._angle_matched_trace(backend, received)
                    chart, _, peaks = Analyzer._matched_filter(backend, received)
                    for correlation in (spatial, chart):
                        self.assertEqual(len(correlation), len(received))
                        self.assertEqual(int(np.argmax(np.abs(correlation))), delay)
                        np.testing.assert_allclose(correlation[delay], gain * np.vdot(reference, reference), rtol=1e-6)
                    np.testing.assert_allclose(spatial, chart)
                    self.assertIn(delay, peaks)

    def test_angle_estimate_without_transmitter(self):
        # Under the existing half-wave convention, this phase progression
        # represents +30 degrees azimuth and zero elevation.
        samples = np.repeat(np.tile(np.exp(0.5j * np.pi * np.arange(4)), 2)[:, None], 256, axis=1)
        backend = SimpleNamespace(
            _tx_waveform=None,
            _angle_corrected_traces=lambda traces: (samples, True),
            _angle_geometry=lambda: np.arange(8).reshape(2, 4),
            fft_size=256,
            _spectrum_center_hz=module.espargos.constants.SPEED_OF_LIGHT / (2 * module.espargos.constants.ANTENNA_SEPARATION),
            angleChanged=Mock(),
        )
        Analyzer._update_angle_estimate(backend, dict(enumerate(samples)))
        self.assertAlmostEqual(backend._angle_estimate_deg, 30.0, places=5)
        self.assertAlmostEqual(backend._elevation_estimate_deg, 0.0, places=5)
        self.assertIn("TX inactive", backend._angle_status)
        backend.angleChanged.emit.assert_called_once()
        backend._angle_corrected_traces = lambda traces: (samples, False)
        Analyzer._update_angle_estimate(backend, dict(enumerate(samples)))
        self.assertTrue(np.isnan(backend._angle_estimate_deg))
        self.assertIn("calibration", backend._angle_status)

    def test_each_outer_column_contributes_to_radar_map(self):
        for antenna in (0, 3, 4, 7):
            with self.subTest(antenna=antenna):
                samples = np.zeros((8, 256), dtype=np.complex64)
                samples[antenna, 40] = 10
                backend = SimpleNamespace(
                    _tx_waveform=None,
                    _current_ping_config=lambda: {"pings_per_256": 2},
                    _angle_corrected_traces=lambda traces: (samples, False),
                    _angle_geometry=lambda: np.arange(8).reshape(2, 4),
                    _radar_angle_size=256,
                    _radar_delay_size=256,
                    radarConfigChanged=Mock(),
                    radarStatusChanged=Mock(),
                    radarMapChanged=Mock(),
                )
                Analyzer._render_radar_heatmap(backend, {antenna: samples[antenna]})
                rgba = np.asarray(backend.radarMapChanged.emit.call_args.args[0]).reshape(256, 256, 4)
                self.assertGreater(rgba[40, :, 1].min(), rgba[0, :, 1].max())


if __name__ == "__main__":
    unittest.main()
