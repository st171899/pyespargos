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
    def test_peak_lists_reject_noise_and_keep_pronounced_returns(self):
        spec = importlib.util.spec_from_file_location("espargi_peaks", DEMO_DIR / "pulse-radar-analyzer-espargi.py")
        mixed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mixed)
        rng = np.random.default_rng(152)
        for analyzer in (Analyzer, mixed.EspargosPulseRadarAnalyzer):
            for length in (1, 4, 16, 64):
                reference = np.exp(1j * rng.uniform(-np.pi, np.pi, length)).astype(np.complex64)
                cfg = dict(chirp_frames=length, pings_per_256=2)
                backend = SimpleNamespace(_tx_waveform=reference, _current_ping_config=lambda: cfg, _reference_ping_config=lambda: cfg)
                for trial in range(20):
                    with self.subTest(analyzer=analyzer.__module__, length=length, trial=trial):
                        noise = (rng.normal(size=256) + 1j * rng.normal(size=256)).astype(np.complex64)
                        _, _, peaks = analyzer._matched_filter(backend, noise)
                        self.assertEqual(peaks, [])
                        received = noise.copy()
                        received[40 : 40 + length] += 12 * reference
                        received[180 : 180 + length] += 10 * np.exp(0.7j) * reference
                        _, _, peaks = analyzer._matched_filter(backend, received)
                        self.assertIn(40, peaks)
                        self.assertIn(180, peaks)
                        # A short random-phase reference can have strong
                        # deterministic autocorrelation sidelobes. Those are
                        # not noise detections; no peak may occur outside the
                        # two returns' autocorrelation support.
                        self.assertTrue(all(min(abs(k - 40), abs(k - 180)) < length for k in peaks))

    def test_overlapping_chirps_retain_separately_resolved_peaks(self):
        spec = importlib.util.spec_from_file_location("espargi_overlap", DEMO_DIR / "pulse-radar-analyzer-espargi.py")
        mixed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mixed)
        analyzer = mixed.EspargosPulseRadarAnalyzer
        cfg = dict(chirp_frames=64, bandwidth_hz=40e6, pings_per_256=2)
        waveform = analyzer._build_ping_waveform(None, cfg)
        backend = SimpleNamespace(_tx_waveform=waveform, _reference_ping_config=lambda: cfg)
        received = np.zeros(256, np.complex64)
        received[40:104] += 12 * waveform[:64]
        received[50:114] += 10 * np.exp(0.7j) * waveform[:64]
        # The second echo starts well within the first 64-sample chirp and
        # also within the previous chirp_length/4 peak suppression radius.
        correlation, _, peaks = analyzer._matched_filter(backend, received)
        self.assertEqual(peaks, [40, 50])
        chirp_only = np.correlate(received, waveform[:64], mode="full")[63:319]
        np.testing.assert_allclose(correlation, chirp_only, atol=1e-4)

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
                    appconfig=SimpleNamespace(get=lambda key: {"delay_min": 0, "delay_max": 256}.get(key)),
                    _radar_angle_size=256,
                    _radar_delay_size=256,
                    radarConfigChanged=Mock(),
                    radarStatusChanged=Mock(),
                    radarMapChanged=Mock(),
                )
                Analyzer._render_radar_heatmap(backend, {antenna: samples[antenna]})
                rgba = np.asarray(backend.radarMapChanged.emit.call_args.args[0]).reshape(256, 256, 4)
                self.assertGreater(rgba[40, :, 1].min(), rgba[0, :, 1].max())

    def test_selected_board_geometry_ignores_unmeasured_yaml_layout(self):
        # A deliberately scrambled multi-board layout must not become physical
        # antenna coordinates, or contaminate the selected board's aperture.
        class Revision:
            def antenna_id_to_row_col(self, antenna):
                return divmod(7 - antenna, 4)

        backend = SimpleNamespace(pool=SimpleNamespace(boards=[SimpleNamespace(revision=Revision()), SimpleNamespace(revision=Revision())]), active_board=1, n_rows=1, n_cols=16, indexing_matrix=np.arange(16)[None, ::-1])
        geometry = Analyzer._angle_geometry(backend)
        np.testing.assert_array_equal(geometry, np.arange(8)[::-1].reshape(2, 4))
        spec = importlib.util.spec_from_file_location("espargi_geometry", DEMO_DIR / "pulse-radar-analyzer-espargi.py")
        mixed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mixed)
        backend._rx_sensor_ids = lambda: (1, 3, 5, 7)
        geometry = mixed.EspargosPulseRadarAnalyzer._angle_geometry(backend)
        np.testing.assert_array_equal(geometry, [[7, -1, 5, -1], [3, -1, 1, -1]])

    def test_pluto_filter_keeps_transmitted_settings_and_resamples(self):
        from radar_signal import chirp_reference

        cfg = dict(chirp_frames=32, bandwidth_hz=8e6, pings_per_256=2)
        waveform = Analyzer._build_ping_waveform(None, cfg)
        for rate in (80e6, 40e6, 20e6, 10e6, 8e6, 4e6):
            reference, _ = chirp_reference(waveform, cfg, rate, 40e6)
            # Compare interpolation against the independent Fourier sum of
            # the actual integer DAC samples, including their quantization.
            positions = np.arange(reference.size) * 40e6 / rate
            bins = np.fft.fftfreq(waveform.size)
            expected = np.sum(np.fft.fft(waveform)[None, :] * np.exp(2j * np.pi * positions[:, None] * bins), axis=1) / waveform.size
            np.testing.assert_allclose(reference, expected, atol=0.002)
            backend = SimpleNamespace(_tx_waveform=waveform, _active_ping_config=cfg, _current_ping_config=lambda: dict(chirp_frames=1, pings_per_256=1), _spectrum_sample_rate_hz=rate)
            received = np.zeros(256, np.complex64)
            gain = 0.6 * np.exp(0.8j)
            received[40 : 40 + reference.size] = reference * gain
            values, _, peaks = Analyzer._matched_filter(backend, received)
            self.assertEqual(np.argmax(abs(values)), 40)
            self.assertIn(40, peaks)
            np.testing.assert_allclose(values[40], gain * np.vdot(reference, reference), rtol=1e-6)
            np.testing.assert_allclose(values, Analyzer._angle_matched_trace(backend, received))


if __name__ == "__main__":
    unittest.main()
