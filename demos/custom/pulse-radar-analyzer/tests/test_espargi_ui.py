"""Load the Transmission tab and exercise settings and asynchronous apply.

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
    def test_tab_and_apply(self):
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
            tabs = window.findChild(QObject, "workspaceTabs")
            self.assertIsNotNone(tabs)
            board_tabs = window.findChild(QObject, "receiveBoardTabs")
            self.assertIsNotNone(board_tabs)
            for mode in ("angle", "radar"):
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
            tabs.setProperty("currentIndex", 1)
            pump()
            app.setTxSensor("espargostx", -1, True)
            app.setTxSensor("espargostx", 3, False)
            self.assertEqual(len(app.txSettings["tx_sensors"]), 7)
            pump()
            self.assertTrue(visual_item(window.contentItem(), "txSensor_espargostx_0").property("checked"))
            self.assertFalse(visual_item(window.contentItem(), "txSensor_espargostx_3").property("checked"))
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
            app.iqcontrol = SimpleNamespace(referenceBusy=False, syncNeeded=True, _set_sync_needed=Mock(), _correction_cache={}, _begin_reference=Mock(return_value=True), _end_reference=Mock())
            release = threading.Event()
            reference = module.np.ones(256, dtype=complex)
            candidate = Mock(antennas=["tx"])
            candidate.start.side_effect = lambda *args: (release.wait(2), reference)[1]
            with patch.object(module, "RadarTransmitter", return_value=candidate) as factory:
                app.startTx()
                self.assertTrue(app.txBusy)
                app.setTxSetting("frequency_mhz", 2500)
                self.assertEqual(app.txSettings["frequency_mhz"], 2420.5)
                release.set()
                app._tx_worker.join(3)
                pump()
                self.assertFalse(app.txBusy)
                self.assertTrue(app.txActive)
                self.assertEqual(factory.call_args.args[1].frequency_mhz, 2420.5)
                self.assertEqual(app._active_ping_config["chirp_frames"], 32)
                self.assertIn("Use Sync + Calibration", app.txStatus)
                app.iqcontrol._end_reference.assert_called_with(False)
                app.stopTx()
                app._tx_worker.join(3)
                pump()
                self.assertFalse(app.txActive)
                self.assertEqual(app.txStatus, "TX stopped.")
            tabs.setProperty("currentIndex", 0)
            pump()
        finally:
            if app._tx_worker:
                app._tx_worker.join(3)
            sip.delete(window)
            sip.delete(app.engine)


if __name__ == "__main__":
    unittest.main()
