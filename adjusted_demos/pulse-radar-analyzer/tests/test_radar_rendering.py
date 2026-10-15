"""Render the actual ping-analyzer QML without ESPARGOS or Pluto hardware.

Run with an OpenGL-capable display (on headless Linux, use xvfb-run):
    QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a \
        python demos/pulse-radar-analyzer/tests/test_radar_rendering.py
"""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

DEMO_DIR = Path(__file__).resolve().parents[1]


class RadarRenderingTest(unittest.TestCase):
    def test_heatmap_renders_and_updates_after_resize(self):
        spec = importlib.util.spec_from_file_location("pulse_radar_analyzer", DEMO_DIR / "pulse-radar-analyzer.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = [str(DEMO_DIR / "pulse-radar-analyzer.py"), "-s", "unused", "-o", "app.display_mode=radar"]
        with patch.object(sys, "argv", argv):
            app = module.EspargosPulseRadarAnalyzer(argv)
        # Exercise the real processing path with no radio connection.
        frames = []
        app.radarMapChanged.connect(frames.append)
        traces = {antid: np.exp(0.1j * (np.arange(256) + antid)).astype(np.complex64) for antid in range(8)}
        with patch.object(app, "_angle_geometry", return_value=np.arange(8).reshape(2, 4)):
            for calibrated in (False, True):
                app.iqcontrol = SimpleNamespace(cal_correction=lambda board, antenna, n: np.ones(n)) if calibrated else None
                for transmitting in (False, True):
                    app._tx_waveform = np.ones(16, dtype=np.complex64) if transmitting else None
                    before = len(frames)
                    app._publish_sample_windows(traces)
                    self.assertEqual(len(frames), before + 1)
                    rgba = np.asarray(frames[-1]).reshape(app.delaySize, app.angleSize, 4)
                    self.assertTrue(np.all(rgba[:, :, 3] == 255))
                    self.assertGreater(np.ptp(rgba[:, :, :3], axis=(0, 1)).max(), 0)
            app.iqcontrol = None
            app._tx_waveform = None
            # A constant-power raw trace fills every measured delay bin,
            # independently of the ping count and fft_size padding.
            tone = {antid: np.ones(256, dtype=np.complex64) for antid in range(8)}
            for ping_count in (1, 2, 4):
                with patch.object(app, "_current_ping_config", return_value={"pings_per_256": ping_count}):
                    app._render_radar_heatmap(tone)
                    raw = np.asarray(frames[-1]).reshape(app.delaySize, app.angleSize, 4)
                    self.assertEqual(app.delaySize, 256)
                    np.testing.assert_array_equal(raw[0], raw[-1])
                    # A return appearing only in the second repetition must
                    # survive folding, and occupy the correct delay bin.
                    app._tx_waveform = np.ones(16, dtype=np.complex64)
                    period = 256 // ping_count
                    impulses = {antid: np.zeros(256, dtype=np.complex64) for antid in range(8)}
                    for trace in impulses.values():
                        trace[(period if ping_count > 1 else 0) + period // 2] = 10
                    app._render_radar_heatmap(impulses)
                    folded = np.asarray(frames[-1]).reshape(app.delaySize, app.angleSize, 4)
                    self.assertEqual(app.delaySize, period)
                    self.assertEqual(app.delayMax, period)
                    # Viridis green is monotonic with intensity here.
                    self.assertEqual(int(np.argmax(folded[:, :, 1].max(axis=1))), period // 2)
                    app._tx_waveform = None
            # One available antenna must still reach the renderer, even when
            # the complete-only option is selected for the other display modes.
            app.complete_only = True
            app.block_acc = {0: {1: {0: traces[1]}}}
            app.block_time = {0: 0}
            before = len(frames)
            app._render_sample_blocks([0], 4, "radar")
            self.assertEqual(len(frames), before + 1)
            self.assertIn("1/8 antennas", app.radarStatus)
            app._publish_sample_windows({})
            self.assertEqual(app.radarStatus, "Waiting for IQ samples.")
        app.radarMapChanged.disconnect(frames.append)
        # Load the real UI, bypassing the hardware initialization in app.exec().
        app.ready = True
        app.initialize_qml(DEMO_DIR / "pulse-radar-analyzer-ui.qml", {"iqcontrol": None})
        self.assertTrue(app.engine.rootObjects(), "QML did not load")
        window = app.engine.rootObjects()[0]
        window.resize(1000, 750)
        results = []
        errors = []

        def publish(width, height, reverse=False):
            app._radar_angle_size = width
            app._radar_delay_size = height
            app.radarConfigChanged.emit()
            data = np.zeros((height, width, 4), dtype=np.uint8)
            data[: height // 2, :, 2 if reverse else 0] = 255
            data[height // 2 :, :, 0 if reverse else 2] = 255
            data[:, :, 3] = 255
            app.radarMapChanged.emit(data.flatten().tolist())

        def capture(initial=False):
            try:
                image = window.grabWindow().convertToFormat(QImage.Format.Format_RGBA8888)
                self.assertFalse(image.isNull(), "No rendered frame; use an OpenGL-capable display")
                bits = image.bits()
                bits.setsize(image.sizeInBytes())
                pixels = np.frombuffer(bits, np.uint8).reshape(image.height(), image.bytesPerLine())
                pixels = pixels[:, : image.width() * 4].reshape(-1, 4).astype(int)
                if initial:
                    floor = np.count_nonzero(np.all(np.abs(pixels[:, :3] - [68, 1, 84]) < 3, axis=1))
                    self.assertGreater(floor, 1000, "Heatmap is missing before the first sample")
                    return
                red = np.count_nonzero((pixels[:, 0] > 200) & (pixels[:, 1] < 40) & (pixels[:, 2] < 40))
                blue = np.count_nonzero((pixels[:, 2] > 200) & (pixels[:, 1] < 40) & (pixels[:, 0] < 40))
                results.append((red, blue))
            except Exception as error:
                errors.append(error)
                app.quit()

        def resize_and_update():
            window.resize(800, 900)
            publish(128, 64, reverse=True)

        QTimer.singleShot(500, lambda: capture(initial=True))
        QTimer.singleShot(800, lambda: publish(256, 256))
        QTimer.singleShot(1500, capture)
        QTimer.singleShot(1800, resize_and_update)
        QTimer.singleShot(2800, capture)
        QTimer.singleShot(3100, app.quit)
        QApplication.exec()
        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        for red, blue in results:
            self.assertGreater(red, 1000, "Missing red heatmap pixels (software Qt Quick cannot render shaders)")
            self.assertGreater(blue, 1000, "Missing blue heatmap pixels")
        # The outer half of the radius covers roughly three times the area.
        self.assertGreater(results[0][1], 2 * results[0][0])
        self.assertGreater(results[1][0], 2 * results[1][1])


if __name__ == "__main__":
    unittest.main()
