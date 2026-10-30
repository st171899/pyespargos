"""Exercise all-sample processing through the actual acquisition/render/UI path.

Run separately with QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software.
"""
import importlib.util
from pathlib import Path
import sys
import unittest
import threading
from unittest.mock import patch
import numpy as np
from PyQt6.QtCore import QObject, QEventLoop, QTimer, QMetaObject, Qt
from PyQt6.QtTest import QTest

DEMO = Path(__file__).resolve().parents[1]


def pump():
    loop = QEventLoop()
    QTimer.singleShot(60, loop.quit)
    loop.exec()


class SampleRadarUITest(unittest.TestCase):
    def test_all_burst_chunks_and_ui_tradeoffs(self):
        spec = importlib.util.spec_from_file_location('sample_radar_ui', DEMO/'pulse-radar-analyzer-espargi.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = ['radar', '-s', 'unused', '-o', 'app.display_mode=doppler']
        with patch.object(sys, 'argv', argv):
            app = module.EspargosPulseRadarAnalyzer(argv)
        app.ready = True
        app.initialize_qml(DEMO/'pulse-radar-analyzer-ui-espargi.qml', {'iqcontrol': None})
        self.assertTrue(app.engine.rootObjects())
        window = app.engine.rootObjects()[0]
        pump()
        drawer = window.property('appDrawer')
        QMetaObject.invokeMethod(drawer, 'open')
        pump()
        try:
            for name in ('dopplerMethodSelector', 'dopplerWindowSelector', 'dopplerBinsSelector',
                         'dopplerAgeSelector', 'dopplerCompensateSwitch', 'dopplerResponseSelector',
                         'dopplerPartialSelector', 'dopplerHistorySelector', 'dopplerMigrationSwitch',
                         'dopplerVelocityPreset'):
                self.assertIsNone(window.findChild(QObject, name))
            observation = window.findChild(QObject, 'dopplerObservationSelector')
            self.assertEqual(observation.property('count'), 12)
            # Keep an in-progress edit intact while live plot/status signals fire.
            for name, keys, expected in [
                    ('dopplerMaxVelocityField', '350.125', 350.125),
                    ('dopplerMinVelocityField', '-300.25', -300.25)]:
                field = window.findChild(QObject, name)
                QMetaObject.invokeMethod(field, 'forceActiveFocus')
                pump()
                self.assertTrue(field.property('activeFocus'))
                QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
                for char in keys:
                    QTest.keyClick(window, getattr(Qt.Key, 'Key_' + ({'.': 'Period', '-': 'Minus'}.get(char, char))))
                app.radarConfigChanged.emit()
                pump()
                self.assertEqual(field.property('text'), keys)
                QTest.keyClick(window, Qt.Key.Key_Return)
                pump()
                self.assertEqual(app.velocityMaximum if 'Max' in name else app.velocityMinimum, expected)
                field.setProperty('focus', False)
                pump()
                app.radarConfigChanged.emit()
                pump()
                self.assertEqual(float(field.property('text')), expected)
            observation.setProperty('currentIndex', 8)
            pump()
            self.assertEqual(app.appconfig.get('doppler_observation_ms'), 500)
            app.setRadarVelocity(400, True)
            app.setRadarVelocity(200, False)
            app._spectrum_sample_rate_hz, app._spectrum_center_hz = 80e6, 2.4e9
            app._angle_geometry = lambda: np.arange(8).reshape(2, 4)
            cfg = dict(chirp_frames=16, pings_per_256=2, fs_hz=80e6, bandwidth_hz=40e6)
            app._active_ping_config = cfg
            app._tx_waveform = app._build_ping_waveform(cfg)
            velocity = 300.
            indices = [0, 1, 2, 3, 2048, 2049, 2050, 2051]
            sets = []
            for index in indices:
                time = index*256 + np.arange(256)
                trace = 200*app._tx_waveform[(time-23) % 256]*np.exp(-2j*np.pi*2*2.4e9*velocity/module.espargos.constants.SPEED_OF_LIGHT*time/80e6)
                sets.append((index, {a: trace.astype(np.complex64) for a in range(8)}))
                app._radar_capture_meta[index] = dict(count=1, step=0, capture=None, partial=False)
            app._display_config = dict(trigger=dict(mode='interval', interval_chunks=2048, offset_chunks=0, burst_chunks=4))
            frames = []
            # The live QML texture has no per-pixel list signal connection.
            self.assertEqual(app.receivers(app.dopplerMapChanged), 0)
            self.assertEqual(app.receivers(app.radarMapChanged), 0)
            app.dopplerMapChanged.connect(frames.append)
            app._render_block_rows(sets, 'doppler')
            pump()
            self.assertEqual(list(app._sample_radar.records), indices)
            self.assertEqual(len(frames), 1)
            self.assertEqual((app.dopplerDataMinimum, app.dopplerDataMaximum), (200, 400))
            self.assertIn('Coherent Doppler', app.radarStatus)
            self.assertIn('Capture-gap aliases ≈ 9.53 m/s apart', app.dopplerProcessingInfo)
            self.assertIn('no guaranteed unambiguous band', app.dopplerProcessingInfo)
            rgba = np.asarray(frames[-1]).reshape(256, app.angleSize, 4)
            peak = np.unravel_index(np.argmax(rgba[:, :, 1]), rgba.shape[:2])
            self.assertGreaterEqual(int(rgba[23, app.angleSize//2, 1]), int(rgba[:, :, 1].max())-1)
            self.assertIn(peak[0], [23, 151])
            self.assertTrue(129 <= app.angleSize <= 513)
            observation.setProperty('currentIndex', 5)
            pump()
            self.assertEqual(list(app._sample_radar.records), indices)
            self.assertTrue(app._doppler_dirty)
            app._doppler_last_render = 0
            app._render_doppler()
            self.assertEqual(app.appconfig.get('doppler_observation_ms'), 100)
            self.assertEqual(len(frames), 2)  # Reprocess buffered samples without new input.
            # Both ends of the expanded selector reach the processing window.
            for index, seconds in [(0, .005), (11, 5.)]:
                observation.setProperty('currentIndex', index)
                pump()
                app._doppler_last_render = 0
                with patch.object(app._sample_radar, 'maps', wraps=app._sample_radar.maps) as maps:
                    app._render_doppler()
                self.assertEqual(maps.call_args.kwargs['max_age'], seconds)
            # An incomplete firmware average cannot support an exact model.
            app._radar_capture_meta[4096] = dict(count=8, step=256, capture=4096, partial=True)
            app._render_block_rows([(4096, sets[0][1])], 'doppler')
            self.assertNotIn(4096, app._sample_radar.records)
            # A live search must let the Qt event loop run and let changed
            # limits cancel it before publishing an obsolete map.
            app._sample_radar.clear()
            for k in range(512):
                index = (k//4)*8192+k%4
                app._sample_radar.append(index, sets[0][1])
            app.setRadarVelocity(-350, False)
            app.setRadarVelocity(350, True)
            app._doppler_last_render = 0
            before = len(frames)
            errors, ticks = [], []
            loop = QEventLoop()
            def work():
                try:
                    app._render_doppler()
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=work)
            timer = QTimer()
            def heartbeat():
                ticks.append(1)
                if not worker.is_alive():
                    loop.quit()
            timer.timeout.connect(heartbeat)
            timer.start(10)
            worker.start()
            QTimer.singleShot(100, lambda: app.setRadarVelocity(349, True))
            QTimer.singleShot(3000, loop.quit)
            loop.exec()
            timer.stop()
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertGreaterEqual(len(ticks), 3)
            self.assertEqual(len(frames), before)
            app.dopplerMapChanged.disconnect(frames.append)
        finally:
            app.render_running = False
            app._shutting_down = True
            app.txcontrol.executor.shutdown(wait=True)
            app.txcontrol.timer.stop()
            app.txcontrol.poll.stop()
            window.close()


if __name__ == '__main__':
    unittest.main()
