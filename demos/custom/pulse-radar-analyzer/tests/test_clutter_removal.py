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
from clutter import RollingClutter


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
    def test_automatic_subtracts_static_scene_but_preserves_new_complex_return(self):
        rolling = RollingClutter()
        background = np.exp(0.03j * np.arange(256)).astype(np.complex64)
        rolling.process(0, {0: background, 1: 2 * background}, 0)
        residual = rolling.process(1, {0: background, 1: 2 * background}, 0.01)
        np.testing.assert_allclose(residual[0], 0, atol=1e-6)
        target = background.copy()
        target[70] += 5 + 2j
        residual = rolling.process(2, {0: target}, 0.02)
        expected = np.zeros(256, np.complex64)
        expected[70] = 5 + 2j
        np.testing.assert_allclose(residual[0], expected, atol=1e-6)
        self.assertEqual(len(rolling.history[1]), 2)  # Missing antenna is not learned as zero.

    def test_automatic_departed_target_expires_by_count_and_by_time(self):
        for step, count in ((0.001, 17), (0.05, 10)):
            rolling = RollingClutter()
            old_target = np.zeros(256, np.complex64)
            old_target[40] = 12 + 3j
            for index in range(16):
                rolling.process(index, {0: old_target}, index * step)
            new_target = np.zeros(256, np.complex64)
            new_target[70] = 12 + 3j
            first = rolling.process(16, {0: new_target}, 16 * step)[0]
            self.assertGreater(abs(first[40]), 1)  # Initially leaves a negative ghost.
            for index in range(17, 16 + count):
                residual = rolling.process(index, {0: new_target}, index * step)[0]
            self.assertAlmostEqual(abs(residual[40]), 0, places=6)
            self.assertAlmostEqual(abs(residual[70]), 0, places=6)  # Stopped target becomes background.

    def test_automatic_expires_history_across_reception_gap_and_ignores_duplicates(self):
        rolling = RollingClutter()
        rolling.process(1, {0: np.ones(256)}, 0)
        rolling.process(1, {0: np.ones(256)}, 0.01)
        self.assertEqual(len(rolling.history[0]), 1)
        residual = rolling.process(2, {0: 3 * np.ones(256)}, 0.5)
        np.testing.assert_array_equal(residual[0], 0)
        self.assertEqual(len(rolling.history[0]), 1)

    def test_automatic_learns_all_acquisitions_before_display_drops_frames(self):
        backend = SimpleNamespace(
            fft_size=256,
            _display_config={},
            _display_schedule=None,
            _clutter_calibrating=False,
            _clutter_mode="automatic",
            _remove_los=True,
            _clutter_lock=threading.Lock(),
            _automatic_clutter=RollingClutter(),
            block_acc={},
            block_time={},
            last_display_block=-1,
            display_mode="radar",
            clutterStatusChanged=Mock(),
            _render_sample_blocks=Mock(),
        )
        sets = [(index, {0: np.ones(256, np.complex64)}) for index in range(20)]
        Analyzer._render_block_rows(backend, sets, "radar")
        self.assertEqual(backend._automatic_clutter.last_index[0], 19)
        self.assertEqual(len(backend._automatic_clutter.history[0]), 16)
        for block in backend.block_acc.values():
            np.testing.assert_array_equal(block[0][0], 0)
        # Publishing already-cancelled traces must not subtract a second time.
        frame = {0: np.ones(256, np.complex64)}
        self.assertIs(Analyzer._cancel_line_of_sight(backend, frame), frame)

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
