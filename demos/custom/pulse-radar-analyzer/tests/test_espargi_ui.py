"""Load the transmitter sidebar and exercise settings and asynchronous apply.

Run separately with QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software.
No radios or OpenGL shader backend are needed.
"""

import importlib.util
from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt6 import sip
from PyQt6.QtCore import QObject, QEventLoop, QTimer, QMetaObject

DEMO = Path(__file__).resolve().parents[1]


def pump(milliseconds=50):
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()


def visual_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_item(child, name)
        if found is not None:
            return found
    return None


class TransmissionUITest(unittest.TestCase):
    def test_sidebar_and_apply(self):
        spec = importlib.util.spec_from_file_location("espargi_ui", DEMO / "pulse-radar-analyzer-espargi.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = ["radar", "-c", str(DEMO.parents[2] / "config/rx-and-tx-espargos.yml")]
        with patch.object(sys, "argv", argv):
            app = module.EspargosPulseRadarAnalyzer(argv)
        app.pool = SimpleNamespace(names=["espargosrx", "espargostx"], hosts=["rx", "tx"])
        app.receiver = SimpleNamespace(boards=[SimpleNamespace(host=h) for h in app.pool.hosts])
        app.pool.boards = app.receiver.boards
        app.ready = True
        app.initialize_qml(DEMO / "pulse-radar-analyzer-ui-espargi.qml", {"iqcontrol": None})
        self.assertTrue(app.engine.rootObjects())
        window = app.engine.rootObjects()[0]
        try:
            self.assertIsNone(window.findChild(QObject, "workspaceTabs"))
            pump()
            drawer = window.property("appDrawer")
            self.assertIsNotNone(drawer)
            QMetaObject.invokeMethod(drawer, "open")
            pump()
            modes = window.findChild(QObject, "displayModeSelector")
            self.assertEqual(modes.property("count"), 8)
            app._set_display_mode("time")
            app._set_display_mode("spectrum")  # Old saved modes fall back to power.
            self.assertEqual(app.display_mode, "power")
            board_tabs = window.findChild(QObject, "receiveBoardTabs")
            self.assertIsNotNone(board_tabs)
            for mode in ("angle", "radar", "doppler"):
                app._set_display_mode(mode)
                pump()
                self.assertTrue(board_tabs.property("visible"))
                board_tabs.setProperty("currentIndex", 1)
                pump()
                self.assertEqual(app.activeBoard, 1)
                self.assertTrue(module.np.isnan(app.angleEstimate))
                board_tabs.setProperty("currentIndex", 0)
                pump()
                self.assertEqual(app.activeBoard, 0)
            observation = window.findChild(QObject, "dopplerObservationSelector")
            self.assertIsNotNone(observation)
            observation.setProperty("currentIndex", 8)
            pump()
            self.assertEqual(app.appconfig.get("doppler_observation_ms"), 500)
            self.assertIsNone(window.findChild(QObject, "dopplerHistorySelector"))
            app.setRadarVelocity(-2.5, False)
            app.setRadarVelocity(3.5, True)
            self.assertEqual(app.velocityMinimum, -2.5)
            self.assertEqual(app.velocityMaximum, 3.5)
            fine_min = window.findChild(QObject, "dopplerMinVelocityField")
            fine_max = window.findChild(QObject, "dopplerMaxVelocityField")
            fine_min.setProperty("text", "-0.000123456")
            self.assertTrue(fine_min.property("acceptableInput"))
            QMetaObject.invokeMethod(fine_min, "editingFinished")
            fine_max.setProperty("text", "-0.000123455")
            self.assertTrue(fine_max.property("acceptableInput"))
            QMetaObject.invokeMethod(fine_max, "editingFinished")
            self.assertAlmostEqual(app.velocityMinimum, -0.000123456, places=12)
            self.assertAlmostEqual(app.velocityMaximum, -0.000123455, places=12)
            self.assertAlmostEqual(app.velocityMaximum-app.velocityMinimum, 1e-9, places=12)
            app.setRadarVelocity(3.5, True)
            app.setRadarVelocity(-2.5, False)
            clutter = window.findChild(QObject, "clutterModeSelector")
            clutter.setProperty("currentIndex", 2)
            pump()
            self.assertEqual(app.clutterMode, "zero_doppler")
            # No reference must produce a clear empty map, never fake range/Doppler.
            app._tx_waveform = None
            app._angle_geometry = lambda: module.np.arange(8).reshape(2, 4)
            frames = []
            app.dopplerMapChanged.connect(frames.append)
            app._doppler_last_render = 0
            app._render_doppler()
            self.assertEqual(len(frames), 1)
            self.assertIn("Start ping transmission", app.dopplerProcessingInfo)
            app.dopplerMapChanged.disconnect(frames.append)
            app._set_display_mode("radar")
            app.remove_los = True
            static = {a: module.np.full(256, 100+30j, module.np.complex64) for a in range(8)}
            # Actual display ingestion: starts are off the configured phase,
            # and alternate gaps/missing starts must not reset the DC mean.
            app._display_config = dict(trigger=dict(mode="interval", interval_chunks=8192, offset_chunks=0, burst_chunks=4))
            sets = [(k*8192+17+k%2, {a: t for a, t in static.items() if a != k%8}) for k in range(12)]
            app._render_block_rows(sets, "radar")
            self.assertEqual(len(app._doppler.frames), 12)
            radar_frames = []
            app.radarMapChanged.connect(radar_frames.append)
            app._radar_last_render_time = 0
            app._publish_sample_windows(static)
            rgba = module.np.asarray(radar_frames[-1]).reshape(-1, 4)
            module.np.testing.assert_array_equal(rgba, module.np.tile([68, 1, 84, 255], (len(rgba), 1)))
            app.radarMapChanged.disconnect(radar_frames.append)
            app.remove_los = False

            scale = window.findChild(QObject, "radarScaleSelector")
            limits = window.findChild(QObject, "radarPowerRangeSlider")
            self.assertIsNotNone(scale)
            self.assertIsNotNone(limits)
            scale.setProperty("currentIndex", 1)
            limits.setProperty("minimumValue", -30)
            limits.setProperty("maximumValue", -5)
            pump()
            self.assertTrue(app.radarLinearScale)
            self.assertEqual(app.radarMinDb, -30)
            self.assertEqual(app.radarMaxDb, -5)
            scale.setProperty("currentIndex", 0)
            pump()
            self.assertFalse(app.radarLinearScale)
            start = window.findChild(QObject, "radarMinRangeField")
            end = window.findChild(QObject, "radarMaxRangeField")
            self.assertFalse(start.property("enabled"))
            self.assertFalse(end.property("enabled"))
            correction = SimpleNamespace(state=SimpleNamespace(antenna=0), delay_s=0)
            app._radar_timing = SimpleNamespace(delays_s={0: 10 / 80e6}, calibration=SimpleNamespace(select=lambda **kw: SimpleNamespace(groups=[SimpleNamespace(corrections=[correction])])))
            app._tx_calibration_valid = True
            app.radarConfigChanged.emit()
            pump()
            self.assertTrue(start.property("enabled"))
            self.assertTrue(end.property("enabled"))
            start.setProperty("text", str((20.4 - app.radarZeroSample) * app.radarMetersPerSample))
            QMetaObject.invokeMethod(start, "editingFinished")
            end.setProperty("text", str((90.3 - app.radarZeroSample) * app.radarMetersPerSample))
            QMetaObject.invokeMethod(end, "editingFinished")
            pump()
            self.assertEqual((app.delayMin, app.delayMax), (20, 90))
            self.assertAlmostEqual(float(start.property("text")), (20 - app.radarZeroSample) * app.radarMetersPerSample, places=3)
            self.assertAlmostEqual(float(end.property("text")), (90 - app.radarZeroSample) * app.radarMetersPerSample, places=3)
            app._radar_timing = None
            app._tx_calibration_valid = False
            app.radarConfigChanged.emit()
            selector = window.findChild(QObject, "clutterModeSelector")
            button = window.findChild(QObject, "calibrateClutterButton")
            self.assertIsNotNone(selector)
            self.assertIsNotNone(button)
            selector.setProperty("currentIndex", 1)
            pump()
            self.assertEqual(app.clutterMode, "automatic")
            switch = window.findChild(QObject, "clutterRemovalSwitch")
            self.assertIsNotNone(switch)
            switch.setProperty("checked", True)
            pump()
            self.assertFalse(app._clutter_calibrating)
            self.assertFalse(button.property("enabled"))
            selector.setProperty("currentIndex", 0)
            pump()
            self.assertEqual(app.clutterMode, "manual")
            self.assertTrue(button.property("enabled"))
            self.assertFalse(app._clutter_calibrating)  # Wait for the button.
            QMetaObject.invokeMethod(button, "clicked")
            self.assertTrue(app._clutter_calibrating)
            self.assertEqual(app._clutter_target_blocks, 200)
            static_scene = {a: module.np.ones(256, module.np.complex64) for a in range(8)}
            for _ in range(199):
                app._collect_clutter_snapshot(static_scene)
            self.assertTrue(app._clutter_calibrating)
            app._collect_clutter_snapshot(static_scene)
            self.assertFalse(app._clutter_calibrating)
            self.assertTrue(app._clutter_calibrated)
            switch.setProperty("checked", False)
            pump()
            pump()
            window.findChild(QObject, "radarSettingsTabs").setProperty("currentIndex", 1)
            pump()
            app.setTxSensor("espargostx", -1, True)
            app.setTxSensor("espargostx", 3, False)
            self.assertEqual(len(app.txSettings["tx_sensors"]), 7)
            pump()
            self.assertTrue(visual_item(window.contentItem(), "txSensor_espargostx_0").property("checked"))
            self.assertFalse(visual_item(window.contentItem(), "txSensor_espargostx_3").property("checked"))
            tx_rate = visual_item(window.contentItem(), "radarTxSampleRate")
            self.assertIsNotNone(tx_rate)
            app.setTxSetting("fs_hz", 20e6)
            pump()
            self.assertEqual(tx_rate.property("currentIndex"), 2)
            self.assertEqual(app.txSettings["bandwidth_hz"], 20e6)
            self.assertEqual(app.txcontrol.data["sample_counts"], [256, 512, 1024, 2048])
            self.assertAlmostEqual(app.txcontrol.data["beams"][0]["duration_us"], 16/20)
            app.txcontrol.setSetting("samples", 8192)
            self.assertEqual(app.txcontrol.data["samples"], 256)
            app.setTxSetting("fs_hz", 80e6)
            app.txcontrol.setSetting("samples", 8192)
            app.setTxSetting("fs_hz", 2.5e6)
            self.assertEqual(app.txcontrol.data["samples"], 256)
            self.assertEqual(app.txSettings["samples"], 256)
            app.setTxSetting("fs_hz", 80e6)
            app.setTxSetting("bandwidth_hz", 40e6)
            app.setTxSetting("frequency_mhz", 2420.5)
            app.setTxSetting("chirp_frames", 32.0)
            self.assertIsInstance(app.txSettings["chirp_frames"], int)
            app.setTxSetting("tx_calibration", "monitor")
            self.assertEqual(app.txSettings["calibration_path"], "reference")
            app.steerTx(0.5, 0.0)
            self.assertAlmostEqual(app.txSettings["tx_azimuth"], 30)
            self.assertAlmostEqual(app.txSettings["tx_elevation"], 0)
            self.assertIsNotNone(visual_item(window.contentItem(), "txBeamspace"))
            self.assertFalse(app.txActive)
            from radar_waveform import beam_source
            from demos.custom.common.iq_tx_plot import IQTxPlot
            self.assertEqual(app.txcontrol.data["beams"][0]["amplitude"], 10**(app.DEFAULT_CONFIG["tx_gain_db"]/20))
            app.txcontrol.setBeam(0, "amplitude", 0.25)
            self.assertAlmostEqual(app.txSettings["tx_gain_db"], 20*module.np.log10(0.25))
            app.setTxSetting("tx_gain_db", -20)
            self.assertAlmostEqual(app.txcontrol.data["beams"][0]["amplitude"], 0.1)
            plot = window.findChild(IQTxPlot, "txWaveformPreview")
            self.assertIsNotNone(plot)
            self.assertFalse(plot.monitor)
            before = plot.z.copy()
            app.setTxSetting("pings_per_256", 4)
            app.setTxSetting("bandwidth_hz", 20e6)
            app.txcontrol.setBeam(0, "duration_samples", 20)
            app.txcontrol.setBeam(0, "delay_samples", 5)
            pump()
            beam = app.txcontrol.data["beams"][0]
            self.assertEqual((beam["start_mhz"], beam["stop_mhz"]), (-10, 10))
            self.assertEqual((beam["duration_samples"], beam["delay_samples"]), (20, 5))
            expected = beam_source(beam, app.txSettings, 256).synthesize(256)*0.1
            module.np.testing.assert_allclose(plot.z, expected)
            self.assertFalse(module.np.array_equal(plot.z, before))
            # Restore the setup used by the live-update checks below.
            app.txcontrol.setBeam(0, "delay_samples", 0)
            app.setTxSetting("pings_per_256", 2)
            app.setTxSetting("chirp_frames", 32)
            pump()
            app.iqcontrol = SimpleNamespace(referenceBusy=False, syncNeeded=True, _set_sync_needed=Mock(), _correction_cache={}, _begin_reference=Mock(return_value=True), _end_reference=Mock())
            release = threading.Event()
            reference = module.np.ones(256, dtype=complex)
            candidate = Mock(antennas=["tx"])
            candidate.start.side_effect = lambda *args: (release.wait(2), reference)[1]
            with patch.object(module, "RadarTransmitter", return_value=candidate) as factory:
                app.startTx()
                self.assertTrue(app.txBusy)
                release.set()
                app._tx_worker.join(3)
                pump()
                self.assertFalse(app.txBusy)
                self.assertTrue(app.txActive)
                self.assertEqual(factory.call_args.args[1].frequency_mhz, 2420.5)
                self.assertEqual(app._active_ping_config["chirp_frames"], 32)
                self.assertIn("Use Sync + Calibration", app.txStatus)
                app.iqcontrol._end_reference.assert_called_with(False)
                # Stage a different aperture, then drag repeatedly while TX is live.
                candidate.selected = {(1, a) for a in range(8) if a != 3}
                candidate.args = SimpleNamespace(**app.txSettings)
                app.setTxSensor("espargosrx", 0, True)
                app.txcontrol.setSetting("count", 2)
                app.txcontrol.steer(1, -0.4, 0.2)
                app.txcontrol.steer(1, 0.6, 0.1)
                pump(250)
                if app._tx_worker:
                    app._tx_worker.join(3)
                pump()
                applied = factory.call_args.args[1]
                self.assertEqual(len(applied.beams), 2)
                self.assertAlmostEqual(applied.beams[1]["u"], 0.6)
                self.assertNotIn("espargosrx.0", applied.tx_sensors)
                self.assertIn("espargosrx.0", app.txSettings["tx_sensors"])
                self.assertTrue(board_tabs.property("visible"))
                self.assertTrue(app.txActive)
                app.stopTx()
                app._tx_worker.join(3)
                pump()
                self.assertFalse(app.txActive)
                self.assertEqual(app.txStatus, "TX stopped.")
            pump()
        finally:
            if app._tx_worker:
                app._tx_worker.join(3)
            sip.delete(window)
            sip.delete(app.engine)


if __name__ == "__main__":
    unittest.main()
