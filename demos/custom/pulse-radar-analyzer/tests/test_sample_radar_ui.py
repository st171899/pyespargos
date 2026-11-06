"""Exercise all-sample processing through the actual acquisition/render/UI path.

Run separately with QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software.
"""
import importlib.util
from pathlib import Path
import sys
import unittest
import threading
import time
from types import SimpleNamespace
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
                sample_time = index*256 + np.arange(256)
                trace = 200*app._tx_waveform[(sample_time-23) % 256]*np.exp(-2j*np.pi*2*2.4e9*velocity/module.espargos.constants.SPEED_OF_LIGHT*sample_time/80e6)
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
            self.assertIn('Pulse Doppler', app.radarStatus)
            self.assertIn('16 matched pulses', app.radarStatus)
            self.assertIn('Capture-gap aliases ≈ 9.53 m/s apart', app.dopplerProcessingInfo)
            self.assertIn('no guaranteed unambiguous band', app.dopplerProcessingInfo)
            self.assertEqual(app.delaySize, 128)  # Two pings: one unambiguous period.
            rgba = np.asarray(frames[-1]).reshape(app.delaySize, app.angleSize, 4)
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
            # Exercise the actual accumulate -> timestamp history -> rendered
            # map path using the sparse coverage found in real packet headers.
            from range_doppler import accumulation_response
            app._sample_radar.clear()
            app.setRadarVelocity(-2,False)
            app.setRadarVelocity(2,True)
            app.receiver = SimpleNamespace(boards=[SimpleNamespace(revision=SimpleNamespace(
                antenna_id_to_row_col=lambda a:divmod(a,4)))])
            cluster = object.__new__(module.IQAccumCluster)
            cluster.chunk_index = cluster.source_chunk_start = 2342912
            cluster.source_chunk_end = 2351104
            cluster.nominal_chunks,cluster.vector_chunks = 1024,1
            cluster.sample_rate_hz,cluster.center_freq_hz = 80_000_000,2_400_000_000
            cluster.config_generation = np.full((1,2,4),4,np.uint32)
            cluster._completion = np.ones((1,2,4),bool)
            cluster._section_completion = np.ones((1,2,4,1),bool)
            cluster._counts = np.full((1,2,4,1),1024,np.int32)
            cluster._flags = np.zeros((1,2,4,1),np.uint32)
            cluster._coverage_hashes = np.full((1,2,4,1),4125752773,np.uint32)
            source = cluster.chunk_index*256+np.arange(256)
            fd = -2*2.4e9/module.espargos.constants.SPEED_OF_LIGHT
            trace = 200*app._tx_waveform[(source-23)%256]*np.exp(2j*np.pi*fd*source/80e6)*accumulation_response(fd,1024,2048/80e6)
            cluster._sections = np.broadcast_to(trace,(1,2,4,1,256)).astype(np.complex64).copy()
            app._display_config = dict(trigger=dict(mode='accumulate',vector_chunks=1,interval_chunks=8192,offset_chunks=0))
            app._pending_clusters = [(time.monotonic()-1,cluster)]
            app._doppler_last_render = 0
            before = len(frames)
            app._render_block_rows(app._drain_sets(),'doppler')
            self.assertEqual(len(app._sample_radar.records),1)
            record = app._sample_radar.records[cluster.chunk_index]
            self.assertEqual(record[2:4],(1024,2048))
            self.assertEqual(len(frames),before+1)
            self.assertIn('Pulse Doppler',app.radarStatus)
            self.assertNotIn('Waiting for sample-timestamp',app.dopplerProcessingInfo)
            rgba = np.asarray(frames[-1]).reshape(app.delaySize,app.angleSize,4)
            self.assertGreater(int(rgba[23,:,1].max()),1)
            # Invalid fingerprints must explain the rejection, not wait forever.
            app._sample_radar.clear()
            cluster._coverage_hashes[:] ^= 1
            app._pending_clusters = [(time.monotonic()-1,cluster)]
            app._doppler_last_render = 0
            app._render_block_rows(app._drain_sets(),'doppler')
            self.assertFalse(app._sample_radar.records)
            self.assertIn('source coverage cannot be identified',app.dopplerProcessingInfo)
            # Radar publishes an entire validated batch atomically and never
            # mixes observations across the streaming dead time.
            import copy
            app._display_config = dict(trigger=dict(mode='radar', vector_chunks=1,
                interval_chunks=24, offset_chunks=0, pulses=3, cycle_windows=10))
            def radar_cluster(w, bad=False):
                c = copy.deepcopy(cluster)
                c.window_index = w
                c.chunk_index = c.source_chunk_start = w*24
                c.source_chunk_end = c.source_chunk_start+24
                c.nominal_chunks = 8
                c.config_generation[:] = 5
                c._counts[:] = 8
                c._flags[:] = 259 if bad else 256
                h = 2166136261
                for x in range(w*24,w*24+8): h = ((h^x)*16777619)&0xffffffff
                c._coverage_hashes[:] = h
                return c
            app._pending_clusters = [(time.monotonic()-1,radar_cluster(w)) for w in (2,0)]
            self.assertEqual(app._drain_sets(), [])
            app._pending_clusters = [(time.monotonic()-1,radar_cluster(1))]
            ready = app._drain_sets()
            self.assertEqual([i for i,_ in ready],[0,24,48])
            app._render_block_rows(ready,'doppler')
            self.assertEqual(len(app._sample_radar.records),3)
            app._pending_clusters = [(time.monotonic()-1,radar_cluster(w,w==11)) for w in (10,11,12)]
            self.assertEqual(app._drain_sets(), [])
            self.assertEqual(len(app._sample_radar.records),3)
            app._pending_clusters = [(time.monotonic()-1,radar_cluster(w)) for w in (20,21,22)]
            app._render_block_rows(app._drain_sets(),'doppler')
            self.assertEqual(list(app._sample_radar.records),[480,504,528])
            app._display_config = {}
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
