"""Hardware-free tests for clutter calibration and subtraction."""

import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

DEMO_DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pulse_radar_analyzer_clutter", DEMO_DIR / "pulse-radar-analyzer.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Analyzer = module.EspargosPulseRadarAnalyzer


def make_backend(target_blocks=2):
    return SimpleNamespace(
        _tx_waveform=np.ones(16, dtype=np.complex64),
        _current_ping_config=lambda: {"pings_per_256": 1, "chirp_frames": 16},
        _clutter_lock=threading.Lock(),
        _clutter_calibrating=True,
        _clutter_calibrated=False,
        _clutter_collected_blocks=0,
        _clutter_target_blocks=target_blocks,
        _clutter_buffer={},  # Empty initial mapping must not KeyError.
        _clutter_reference={},
        _clutter_status_text="Calibrating clutter...",
        _remove_los=True,
        clutterStatusChanged=Mock(),
    )


class ClutterRemovalTest(unittest.TestCase):
    def test_calibration_initializes_empty_buffers_and_averages_environment(self):
        backend = make_backend()
        static_environment = np.arange(module.CHUNK_SAMPLES, dtype=np.float32).astype(np.complex64)
        traces = {antid: static_environment.copy() for antid in range(module.SENSOR_COUNT)}

        Analyzer._collect_clutter_snapshot(backend, traces)
        self.assertTrue(backend._clutter_calibrating)
        Analyzer._collect_clutter_snapshot(backend, traces)

        self.assertFalse(backend._clutter_calibrating)
        self.assertTrue(backend._clutter_calibrated)
        self.assertEqual(len(backend._clutter_reference), module.SENSOR_COUNT)
        for reference in backend._clutter_reference.values():
            np.testing.assert_allclose(reference, static_environment)

    def test_subtraction_tiles_environment_and_preserves_new_target(self):
        backend = make_backend(target_blocks=1)
        background = np.exp(1j * np.arange(module.CHUNK_SAMPLES) * 0.03).astype(np.complex64)
        backend._clutter_calibrating = False
        backend._clutter_calibrated = True
        backend._clutter_reference = {antid: background.copy() for antid in range(module.SENSOR_COUNT)}

        traces = {}
        for antid in range(module.SENSOR_COUNT):
            current = np.tile(background, 2)
            current[module.CHUNK_SAMPLES + 70] += 5.0 + 2.0j
            traces[antid] = current

        cleaned = Analyzer._cancel_line_of_sight(backend, traces)
        for trace in cleaned.values():
            expected = np.zeros_like(trace)
            expected[module.CHUNK_SAMPLES + 70] = 5.0 + 2.0j
            np.testing.assert_allclose(trace, expected, atol=1e-6)

    def test_no_subtraction_during_calibration_or_when_disabled(self):
        backend = make_backend()
        frame = {antid: np.ones(module.CHUNK_SAMPLES, dtype=np.complex64) for antid in range(module.SENSOR_COUNT)}
        self.assertIs(Analyzer._cancel_line_of_sight(backend, frame), frame)

        backend._clutter_calibrating = False
        backend._clutter_calibrated = True
        backend._remove_los = False
        backend._clutter_reference = {antid: np.ones(module.CHUNK_SAMPLES, dtype=np.complex64) for antid in range(module.SENSOR_COUNT)}
        self.assertIs(Analyzer._cancel_line_of_sight(backend, frame), frame)


if __name__ == "__main__":
    unittest.main()
