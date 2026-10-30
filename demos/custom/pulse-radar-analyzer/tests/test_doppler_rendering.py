"""Exercise the rectangular GPU shader, axis crop, and resize using real QML.

Run with QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1 and a display.
"""
import importlib.util
from pathlib import Path
import sys
import unittest
import time
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

DEMO = Path(__file__).resolve().parents[1]


class DopplerRenderingTest(unittest.TestCase):
    def test_rectangular_texture_crop_requested_range_and_resize(self):
        spec = importlib.util.spec_from_file_location('espargi_render', DEMO / 'pulse-radar-analyzer-espargi.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = ['radar', '-s', 'unused', '-o', 'app.display_mode=doppler']
        class DelayedProvider(module.WaterfallImageProvider):
            delay = 0
            loading = False
            def requestImage(self, image_id, requested_size):
                self.loading = True
                try:
                    time.sleep(self.delay)
                    return super().requestImage(image_id, requested_size)
                finally:
                    self.loading = False

        with patch.object(sys, 'argv', argv), patch.object(module, 'WaterfallImageProvider', DelayedProvider):
            app = module.EspargosPulseRadarAnalyzer(argv)
        app.ready = True
        app.initialize_qml(DEMO / 'pulse-radar-analyzer-ui-espargi.qml', {'iqcontrol': None})
        self.assertTrue(app.engine.rootObjects())
        window = app.engine.rootObjects()[0]
        errors, captures = [], []

        def publish(stage=0):
            app._radar_angle_size, app._radar_delay_size = 64, 32
            app._doppler_velocity = np.linspace(-5, 5, 64, endpoint=False)
            if stage == 1:
                app.setRadarVelocity(0, False)
                app.setRadarVelocity(4.8, True)
                window.resize(1400, 950)
            elif stage == 2:
                app.setRadarVelocity(-15, False)
                app.setRadarVelocity(15, True)
            if stage == 3:
                app._doppler_velocity = np.linspace(-15, 15, 64)
            app.radarConfigChanged.emit()
            data = np.zeros((32, 64, 4), np.uint8)
            data[:, :32, 0] = 255
            data[:, 32:, 2] = 255
            data[:, :, 3] = 255
            if stage >= 4:
                app._set_display_mode("radar")
                app._radar_angle_size, app._radar_delay_size = 64, 32
                if stage == 5:
                    window.resize(1280, 850)
                    data[:, :, [0, 2]] = data[:, :, [2, 0]]
                app.radarConfigChanged.emit()
                module.publish_map(app, data)
            else:
                module.publish_map(app, data, True)

        def capture(stage=0):
            try:
                image = window.grabWindow().convertToFormat(QImage.Format.Format_RGBA8888)
                self.assertFalse(image.isNull())
                bits = image.bits()
                bits.setsize(image.sizeInBytes())
                pixels = np.frombuffer(bits, np.uint8).reshape(image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(-1, 4)
                red = np.count_nonzero((pixels[:, 0] > 220) & (pixels[:, 1] < 30) & (pixels[:, 2] < 30))
                blue = np.count_nonzero((pixels[:, 2] > 220) & (pixels[:, 1] < 30) & (pixels[:, 0] < 30))
                self.assertGreater(blue, 10000)
                if stage >= 4:
                    self.assertGreater(red, 10000)
                    captures.append(True)
                    return
                if stage == 2:
                    self.assertGreater(red+blue, image.width()*image.height()*0.12)
                    self.assertLess(red+blue, image.width()*image.height()*0.4)
                else:
                    self.assertGreater(red+blue, image.width()*image.height()*0.45)
                if stage == 1:
                    self.assertLess(red, 1000)
                else:
                    self.assertGreater(red, 10000)
                    self.assertLess(abs(red-blue)/max(red, blue), 0.05)
                captures.append(True)
            except Exception as error:
                errors.append(error)
                app.quit()

        # Capture throughout an asynchronous load, rather than only after it
        # finishes. Both shaders must retain a complete old frame at every tick.
        probe = dict(doppler=False, samples=0, loading=False)
        monitor = QTimer()
        monitor.setInterval(25)
        def check_retained_frame():
            try:
                provider = app._doppler_image if probe['doppler'] else app._radar_image
                probe['loading'] |= provider.loading
                frame = window.grabWindow().convertToFormat(QImage.Format.Format_RGBA8888)
                bits = frame.bits()
                bits.setsize(frame.sizeInBytes())
                pixels = np.frombuffer(bits,np.uint8).reshape(frame.height(),frame.bytesPerLine())[:, :frame.width()*4].reshape(-1,4)
                colored = ((pixels[:,0] > 220) & (pixels[:,1] < 30) & (pixels[:,2] < 30)) | ((pixels[:,2] > 220) & (pixels[:,1] < 30) & (pixels[:,0] < 30))
                self.assertGreater(np.count_nonzero(colored),20000, 'Shader went blank during texture replacement')
                probe['samples'] += 1
            except Exception as error:
                errors.append(error)
                monitor.stop()
                app.quit()
        monitor.timeout.connect(check_retained_frame)
        def start_probe(doppler):
            probe.update(doppler=doppler, samples=0, loading=False)
            provider = app._doppler_image if doppler else app._radar_image
            provider.delay = .3
            pixels = np.zeros((32,64,4),np.uint8)
            pixels[:, :32,0] = 255
            pixels[:,32:,2] = 255
            pixels[:,:,3] = 255
            module.publish_map(app,pixels,doppler)
            monitor.start()
        def finish_probe():
            monitor.stop()
            try:
                self.assertTrue(probe['loading'], 'Test never observed the pending image load')
                self.assertGreaterEqual(probe['samples'],5)
            except Exception as error:
                errors.append(error)
            (app._doppler_image if probe['doppler'] else app._radar_image).delay = 0
        def return_to_doppler():
            app._set_display_mode('doppler')
            publish(3)

        QTimer.singleShot(500, publish)
        QTimer.singleShot(1300, capture)
        QTimer.singleShot(1600, lambda: publish(1))
        QTimer.singleShot(2400, lambda: capture(1))
        QTimer.singleShot(2700, lambda: publish(2))
        QTimer.singleShot(3500, lambda: capture(2))
        QTimer.singleShot(3800, lambda: publish(3))
        QTimer.singleShot(4600, lambda: capture(3))
        QTimer.singleShot(4900, lambda: publish(4))
        QTimer.singleShot(5700, lambda: capture(4))
        QTimer.singleShot(6000, lambda: publish(5))
        QTimer.singleShot(6800, lambda: capture(5))
        QTimer.singleShot(7100, lambda: start_probe(False))
        QTimer.singleShot(7700, finish_probe)
        QTimer.singleShot(8000, return_to_doppler)
        QTimer.singleShot(8600, lambda: start_probe(True))
        QTimer.singleShot(9200, finish_probe)
        QTimer.singleShot(9500, app.quit)
        QApplication.exec()
        self.assertEqual(errors, [])
        self.assertEqual(len(captures), 6)


if __name__ == '__main__':
    unittest.main()
