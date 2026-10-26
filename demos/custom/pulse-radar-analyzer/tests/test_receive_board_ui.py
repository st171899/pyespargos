"""Pluto receive-board controls stay above the angle/radar overlays."""

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QObject, QEventLoop, QTimer, QMetaObject
from PyQt6 import sip

DEMO = Path(__file__).resolve().parents[1]


def pump():
    loop = QEventLoop()
    QTimer.singleShot(50, loop.quit)
    loop.exec()


class ReceiveBoardUITest(unittest.TestCase):
    def test_selection_in_spatial_views(self):
        spec = importlib.util.spec_from_file_location("pluto_board_ui", DEMO / "pulse-radar-analyzer.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        argv = ["radar", "-s", "unused"]
        with patch.object(sys, "argv", argv):
            app = module.EspargosPulseRadarAnalyzer(argv)
        app.pool = SimpleNamespace(boards=[SimpleNamespace(host="first"), SimpleNamespace(host="second")])
        app.ready = True
        app.initialize_qml(DEMO / "pulse-radar-analyzer-ui.qml", {"iqcontrol": None})
        self.assertTrue(app.engine.rootObjects())
        window = app.engine.rootObjects()[0]
        try:
            tabs = window.findChild(QObject, "receiveBoardTabs")
            self.assertIsNotNone(tabs)
            for mode in ("angle", "radar"):
                app._set_display_mode(mode)
                loop = QEventLoop()
                QTimer.singleShot(50, loop.quit)
                loop.exec()
                self.assertTrue(tabs.property("visible"))
                self.assertGreater(tabs.property("height"), 0)
                tabs.setProperty("currentIndex", 1)
                self.assertEqual(app.activeBoard, 1)
                self.assertTrue(module.np.isnan(app.angleEstimate))
                tabs.setProperty("currentIndex", 0)
                self.assertEqual(app.activeBoard, 0)
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
            loader = window.findChild(QObject, "radarModeLoader")
            self.assertGreaterEqual(loader.y(), tabs.y() + tabs.height())
        finally:
            sip.delete(window)
            sip.delete(app.engine)


if __name__ == "__main__":
    unittest.main()
