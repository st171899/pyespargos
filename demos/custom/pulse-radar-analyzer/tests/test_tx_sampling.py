"""Sampling-rate conversion: spectral purity, phase and pulse-arrival timing."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock

import numpy as np

DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
from radar_signal import resample_replay
from radar_waveform import beam_source, pulse_waveform, replay_samples, prepare_source
from radar_tx import RadarTransmitter
from demos.custom.common.iq_tx_waveform import default_beam_settings

spec = importlib.util.spec_from_file_location("sampling_radar", DEMO / "pulse-radar-analyzer-espargi.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SamplingTest(unittest.TestCase):
    def test_upsampling_preserves_complex_tone_without_images(self):
        phase = 0.7
        source = np.exp(1j * (2*np.pi*32*np.arange(256)/256 + phase))
        output = resample_replay(source, 40e6, 80e6)
        expected = np.exp(1j * (2*np.pi*5e6*np.arange(512)/80e6 + phase))
        np.testing.assert_allclose(output, expected, atol=3e-7)
        spectrum = np.fft.fft(output)/output.size
        spectrum[32] = 0
        self.assertLess(float(np.max(abs(spectrum))), 1e-7)

    def test_downsampling_removes_out_of_band_tone_instead_of_aliasing(self):
        time = np.arange(256)/80e6
        source = np.exp(2j*np.pi*5e6*time) + 0.5*np.exp(2j*np.pi*25e6*time)
        output = resample_replay(source, 80e6, 20e6)
        expected = np.exp(2j*np.pi*5e6*np.arange(64)/20e6)
        np.testing.assert_allclose(output, expected, atol=3e-7)

    def test_nonintegral_period_keeps_exact_rate_and_phase(self):
        source = np.exp(2j*np.pi*2.5e6*np.arange(256)/80e6)
        output = resample_replay(source, 80e6, 8e6)
        # Five input periods provide an integral output count: no rounded rate.
        self.assertEqual(output.size, 128)
        np.testing.assert_allclose(output, np.exp(2j*np.pi*2.5e6*np.arange(128)/8e6), atol=3e-7)

    def test_equal_rates_are_unchanged_and_invalid_rates_rejected(self):
        source = np.array([1+0j, -0.5j], np.complex64)
        np.testing.assert_array_equal(resample_replay(source, 40e6, 40e6), source)
        for rate in (0, -1, np.nan, np.inf):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                resample_replay(source, rate, 80e6)

    def test_tx_rate_controls_duration_and_preserves_sweep_frequencies(self):
        config = dict(fs_hz=40e6, chirp_frames=32, bandwidth_hz=10e6, pings_per_256=2)
        waveform = pulse_waveform(config)
        reference, period = module.receive_chirp_reference(waveform, config, 80e6)
        self.assertEqual((reference.size, period), (64, 256))
        backend = SimpleNamespace(_tx_waveform=waveform, _reference_ping_config=lambda: config, _spectrum_sample_rate_hz=80e6)
        received = np.zeros(256, np.complex64)
        phase = np.exp(0.7j)
        received[41:41+reference.size] = phase*reference
        spatial = module.EspargosPulseRadarAnalyzer._angle_matched_trace(backend, received)
        chart, _, peaks = module.EspargosPulseRadarAnalyzer._matched_filter(backend, received)
        self.assertEqual(int(np.argmax(abs(spatial))), 41)
        self.assertIn(41, peaks)
        np.testing.assert_allclose(chart, spatial)
        np.testing.assert_allclose(spatial[41], phase*np.vdot(reference, reference), rtol=1e-6)

    def test_editor_tone_uses_selected_source_rate(self):
        beam = default_beam_settings()
        beam['offset_mhz'] = 5
        source = beam_source(beam, dict(fs_hz=40e6, waveform_editor=True), 256).synthesize(256)
        self.assertEqual(int(np.argmax(abs(np.fft.fft(source)))), 32)

    def test_20_mhz_transmission_and_20_msps_reception_match(self):
        config = dict(fs_hz=20e6, bandwidth_hz=20e6, chirp_frames=32, pings_per_256=2)
        beam = default_beam_settings()
        beam.update(type='lfm', delay_samples=0)
        hardware, native = prepare_source(beam_source(beam, config, 256), config, 256)
        # An ideal RX ADC at 20 MSa/s observes every fourth 80 MSa/s DAC sample.
        received_period = hardware.synthesize(1024)[::4]
        np.testing.assert_allclose(received_period, native, atol=1.5/511)
        template, _ = module.receive_chirp_reference(native, config, 20e6)
        received = np.zeros(256, np.complex64)
        received[21:53] = received_period[:32] * np.exp(0.6j)
        backend = SimpleNamespace(_tx_waveform=native, _reference_ping_config=lambda: config,
                                  _spectrum_sample_rate_hz=20e6)
        correlation, _, peaks = module.EspargosPulseRadarAnalyzer._matched_filter(backend, received)
        self.assertEqual(int(np.argmax(abs(correlation))), 21)
        self.assertIn(21, peaks)
        self.assertAlmostEqual(float(np.angle(correlation[21])), 0.6, places=3)
        self.assertEqual(template.size, 32)

    def test_replay_limits_and_chirp_nyquist_validation(self):
        self.assertEqual(replay_samples(dict(fs_hz=40e6), 256), 512)
        self.assertEqual(replay_samples(dict(fs_hz=2.5e6), 256), 8192)
        for config, samples in ((dict(fs_hz=40e6), 8192), (dict(fs_hz=8e6), 256)):
            with self.assertRaises(ValueError):
                replay_samples(config, samples)
        with self.assertRaises(ValueError):
            pulse_waveform(dict(fs_hz=20e6, bandwidth_hz=40e6, chirp_frames=16, pings_per_256=2))

    def test_hardware_upload_preserves_duration_and_source_reference(self):
        beam = default_beam_settings()
        beam.update(type='lfm', amplitude=0.1, delay_samples=5)
        args = SimpleNamespace(fs_hz=40e6, chirp_frames=24, bandwidth_hz=10e6, pings_per_256=2,
                               samples=256, waveform_editor=False, beams=[beam])
        tx = RadarTransmitter.__new__(RadarTransmitter)
        tx.args, tx.session, tx.antennas = args, MagicMock(), ['antenna']
        reference = tx.prepare(np.zeros(256, complex), -20)
        tx.session.update.assert_called_once_with(samples=512)
        uploaded = tx.session.signals.add.call_args.kwargs['waveform'].synthesize(512)
        expected = resample_replay(reference, 40e6, 80e6)
        # Hardware quantizes each component to 10 bits; no interpolation clipping.
        np.testing.assert_allclose(uploaded, expected, atol=1.5/511)
        self.assertLessEqual(float(max(abs(uploaded.real).max(), abs(uploaded.imag).max())), 1)
        self.assertEqual(reference.size, 256)


if __name__ == '__main__':
    unittest.main()
