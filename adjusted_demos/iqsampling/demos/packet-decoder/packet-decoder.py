#!/usr/bin/env python3
"""GUI signal-triggered ESPARGOS packet detector and protocol decoder."""

from __future__ import annotations

import argparse
from collections import deque
import pathlib
import queue
import signal
import sys
import threading
import time

import numpy as np

# pyespargos repository root (this demo lives in an addon checkout).
sys.path.append(str(pathlib.Path(__file__).absolute().parents[4]))

import PyQt6.QtCharts
import PyQt6.QtCore

import espargos  # noqa: F401 -- loads addon entry points
from espargos_iqsampling.iq_application import ESPARGOSIQApplication
from espargos_iqsampling.packet_decoding import default_pipeline

DECIMATION = {80: 1, 40: 2, 20: 4, 10: 6, 8: 8, 4: 10}
MAX_PLOT_POINTS = 4096
CAPTURE_RATE_WINDOW_SECONDS = 5.0


def _wifi_channel_center_hz(channel):
    channel = int(channel)
    if channel == 14:
        return 2_484_000_000
    if 1 <= channel <= 13:
        return 2_407_000_000 + 5_000_000 * channel
    raise ValueError(f"unsupported 2.4 GHz WiFi channel {channel}")


class ObservationModel(PyQt6.QtCore.QAbstractListModel):
    """Bounded, protocol-neutral packet list exposed to QML."""

    ROLES = (
        "clock",
        "protocol",
        "status",
        "confidence",
        "duration",
        "frequency",
        "bandwidth",
        "rate",
        "summary",
        "sensor",
    )

    def __init__(self, limit=500):
        super().__init__()
        self._rows = []
        self._limit = limit

    def rowCount(self, parent=PyQt6.QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=PyQt6.QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        offset = role - int(PyQt6.QtCore.Qt.ItemDataRole.UserRole) - 1
        if 0 <= offset < len(self.ROLES):
            return self._rows[index.row()].get(self.ROLES[offset], "")
        return None

    def roleNames(self):
        base = int(PyQt6.QtCore.Qt.ItemDataRole.UserRole) + 1
        return {base + index: name.encode() for index, name in enumerate(self.ROLES)}

    @PyQt6.QtCore.pyqtSlot(dict)
    def prepend(self, row):
        self.beginInsertRows(PyQt6.QtCore.QModelIndex(), 0, 0)
        self._rows.insert(0, dict(row))
        self.endInsertRows()
        if len(self._rows) > self._limit:
            first, last = self._limit, len(self._rows) - 1
            self.beginRemoveRows(PyQt6.QtCore.QModelIndex(), first, last)
            del self._rows[first:]
            self.endRemoveRows()

    @PyQt6.QtCore.pyqtSlot()
    def clear(self):
        if not self._rows:
            return
        self.beginResetModel()
        self._rows.clear()
        self.endResetModel()


class PacketDecoderApplication(ESPARGOSIQApplication):
    """Protocol-neutral receiver built on the common ESPARGOS lifecycle."""

    DEFAULT_CONFIG = {"decode_sensors": 1, "show_unknown": True}

    statusChanged = PyQt6.QtCore.pyqtSignal()
    statsChanged = PyQt6.QtCore.pyqtSignal()
    waveformChanged = PyQt6.QtCore.pyqtSignal()
    observationReady = PyQt6.QtCore.pyqtSignal(dict)

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument("--channel", type=int, default=11)
        parser.add_argument("--center-hz", type=int)
        parser.add_argument("--sample-rate", type=int, choices=DECIMATION, default=80)
        parser.add_argument("--gain", type=int, default=50)
        parser.add_argument("--threshold", type=int, default=24)
        parser.add_argument(
            "--silence-threshold",
            type=int,
            default=12,
            help="require a preceding quiet chunk below this amplitude; 0 disables onset detection",
        )
        parser.add_argument("--trigger-mask", type=lambda value: int(value, 0), default=0xFF)
        parser.add_argument("--holdoff-ms", type=int, default=1)
        parser.add_argument("--capture-chunks", type=int, choices=range(56, 113), default=56, metavar="56..112")
        parser.add_argument("--decode-sensors", type=int, choices=range(1, 9), default=None, metavar="1..8")
        parser.add_argument("--sync-timeout", type=float, default=25)
        parser.add_argument("--duration", type=float, default=0)
        parser.add_argument("--record-dir", type=pathlib.Path, help="save a bounded set of raw array captures as compressed NumPy files")
        parser.add_argument("--max-recordings", type=int, default=100)

    def _process_args(self):
        super()._process_args()
        if not 0 <= self.args.silence_threshold < self.args.threshold:
            raise ValueError("silence threshold must be 0 or lower than threshold")
        if self.args.decode_sensors is not None:
            self.initial_config["app"]["decode_sensors"] = self.args.decode_sensors

    def __init__(self, argv):
        parser = argparse.ArgumentParser(description=__doc__, add_help=False)
        super().__init__(argv, argparse_parent=parser)
        if len(self.get_initial_config("pool", "hosts")) != 1:
            raise ValueError("wired-OR Signal capture currently requires exactly one ESPARGOS board")

        self.setApplicationName("ESPARGOS Packet Decoder")
        self.observation_model = ObservationModel()
        self.observationReady.connect(self.observation_model.prepend)
        self.iqcontrol = None
        self.pipeline = default_pipeline()
        self.decode_sensor_count = int(self.get_initial_config("app", "decode_sensors"))
        self.show_unknown = bool(self.get_initial_config("app", "show_unknown"))
        self.appConfigChanged.connect(self._on_app_config_changed)
        self._status = "Initializing packet decoder"
        self._counts = {
            "captures": 0,
            "processed": 0,
            "recognized": 0,
            "observations": 0,
            "decoded": 0,
            "classified": 0,
            "unknown": 0,
            "queue_drops": 0,
        }
        self._protocol_counts = {}
        self._stats_lock = threading.Lock()
        self._capture_times = deque()
        self._capture_times_lock = threading.Lock()
        self._stats_timer = PyQt6.QtCore.QTimer(self)
        self._stats_timer.setInterval(500)
        self._stats_timer.timeout.connect(self.statsChanged.emit)
        self._stats_timer.start()
        self._wave_lock = threading.Lock()
        self._wave = None
        self._wave_generation = 0
        self._wave_consumed = -1
        self._wave_info = "Waiting for the first complete array capture"
        self._decode_queue = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._decode_threads = []
        self._signal_callback = None
        self._shutdown_started = False
        self._recorded = 0
        self._record_lock = threading.Lock()
        if self.args.record_dir:
            self.args.record_dir.mkdir(parents=True, exist_ok=True)

    def _on_app_config_changed(self, changed):
        if "decode_sensors" in changed:
            self.decode_sensor_count = max(1, min(8, int(changed["decode_sensors"])))
        if "show_unknown" in changed:
            self.show_unknown = bool(changed["show_unknown"])

    @PyQt6.QtCore.pyqtProperty(str, notify=statusChanged)
    def status(self):
        return self._status

    @PyQt6.QtCore.pyqtProperty(str, notify=statsChanged)
    def stats(self):
        with self._stats_lock:
            counts = dict(self._counts)
        recognized_percent = 100 * counts["recognized"] / max(1, counts["processed"])
        text = (
            f"{self.capture_rate:.2f} complete events/s  ·  "
            f"{counts['processed']}/{counts['captures']} processed  ·  "
            f"{counts['recognized']} protocol-recognized ({recognized_percent:.0f}%)  ·  "
            f"{counts['decoded']} decoded  ·  {counts['classified']} classified  ·  "
            f"{counts['unknown']} unknown"
        )
        if counts["queue_drops"]:
            text += f"  ·  {counts['queue_drops']} decoder-queue drops"
        return text

    @PyQt6.QtCore.pyqtProperty(str, notify=statsChanged)
    def protocolSummary(self):
        with self._stats_lock:
            protocol_counts = dict(self._protocol_counts)
        if not protocol_counts:
            return "No packet families observed yet"
        ordered = sorted(protocol_counts.items(), key=lambda item: (-item[1], item[0]))
        return "  ·  ".join(f"{name}: {count}" for name, count in ordered[:6])

    @property
    def capture_rate(self):
        now = time.monotonic()
        cutoff = now - CAPTURE_RATE_WINDOW_SECONDS
        with self._capture_times_lock:
            while self._capture_times and self._capture_times[0] < cutoff:
                self._capture_times.popleft()
            if len(self._capture_times) < 2:
                return 0.0
            elapsed = self._capture_times[-1] - self._capture_times[0]
            return (len(self._capture_times) - 1) / elapsed if elapsed > 0 else 0.0

    @PyQt6.QtCore.pyqtProperty(str, notify=waveformChanged)
    def waveformInfo(self):
        return self._wave_info

    def _set_status(self, text):
        self._status = text
        self.statusChanged.emit()

    def _calibrate_pool(self, calibrate, additional_calibrate_args):
        super()._calibrate_pool(calibrate=calibrate, additional_calibrate_args=additional_calibrate_args)
        self._signal_callback = self.pool.add_signal_capture_callback(self._on_capture)
        self._decode_threads = [
            threading.Thread(
                target=self._decode_loop,
                name=f"packet-decoder-{index}",
                daemon=True,
            )
            for index in range(2)
        ]
        for thread in self._decode_threads:
            thread.start()
        try:
            center_hz = self.args.center_hz or _wifi_channel_center_hz(self.args.channel)
            self._set_status("Establishing array timestamp grid")
            self.pool.apply_config(
                {
                    "mode": "wifi",
                    "adc_decimation": DECIMATION[self.args.sample_rate],
                    "rf_freq_hz": center_hz,
                    # The coarse timestamp anchor only needs a packet decoded
                    # by every hardware WiFi modem. Let their AGCs acquire it;
                    # the lower fixed IQ gain is applied afterwards and cannot
                    # change the resulting timestamp grid.
                    "gain_mode": "auto",
                    "trigger_mode": 4,
                    "trigger_config": [
                        self.args.threshold,
                        self.args.trigger_mask,
                        self.args.holdoff_ms,
                        self.args.capture_chunks,
                        self.args.silence_threshold,
                    ],
                }
            )
            self.pool.sync(timeout=self.args.sync_timeout)
            self.pool.apply_config(
                {
                    "gain_mode": "manual",
                    "rx_gain": self.args.gain,
                }
            )
            self.pool.enter_iq_mode(timeout=self.args.sync_timeout)
            onset = (
                f"silence {self.args.silence_threshold}"
                if self.args.silence_threshold
                else "level trigger"
            )
            self._set_status(
                f"Listening at {center_hz / 1e6:g} MHz · "
                f"{self.args.sample_rate} MSa/s · threshold {self.args.threshold} · "
                f"{onset} · {self.args.capture_chunks} chunks"
            )
        except Exception as error:
            self.logger.exception("packet-decoder initialization failed")
            self._set_status(f"Error: {error}")
        return True

    def _on_framework_ready(self):
        self.iqcontrol = self.create_iq_controller()
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)

    def _on_capture(self, capture):
        if self._shutdown_started:
            return
        with self._stats_lock:
            self._counts["captures"] += 1
            capture_number = self._counts["captures"]
        now = time.monotonic()
        with self._capture_times_lock:
            self._capture_times.append(now)
            cutoff = now - CAPTURE_RATE_WINDOW_SECONDS
            while self._capture_times[0] < cutoff:
                self._capture_times.popleft()

        flat = capture.iq.reshape(-1, capture.iq.shape[-1])
        ac = flat - np.mean(flat, axis=-1, keepdims=True)
        powers = np.mean(np.abs(ac) ** 2, axis=-1)
        strongest = int(np.argmax(powers))
        samples = ac[strongest]
        take = np.linspace(0, len(samples) - 1, min(len(samples), MAX_PLOT_POINTS), dtype=np.int64)
        plot = samples[take].astype(np.complex64, copy=True)
        time_us = take.astype(np.float64) * 1e6 / capture.sample_rate_hz
        with self._wave_lock:
            self._wave = (time_us, plot)
            self._wave_generation += 1
            self._wave_info = f"Capture {capture_number} · strongest sensor {strongest} · " f"{capture.duration_seconds * 1e6:.1f} µs · {capture.sample_rate_hz / 1e6:g} MSa/s"
        self.waveformChanged.emit()
        self.statsChanged.emit()
        try:
            self._decode_queue.put_nowait((capture, ac))
        except queue.Full:
            with self._stats_lock:
                self._counts["queue_drops"] += 1

    def _record_capture(self, capture, signals):
        if not self.args.record_dir:
            return
        with self._record_lock:
            if self._recorded >= max(0, self.args.max_recordings):
                return
            self._recorded += 1
        path = self.args.record_dir / f"capture-{capture.capture_id:010d}.npz"
        np.savez_compressed(
            path,
            iq=signals.astype(np.complex64),
            sample_rate_hz=np.int64(capture.sample_rate_hz),
            center_freq_hz=np.int64(capture.center_freq_hz),
            capture_id=np.uint32(capture.capture_id),
        )

    @staticmethod
    def _format_observation(observation, capture):
        frequency = "—" if observation.center_offset_hz is None else f"{(capture.center_freq_hz + observation.center_offset_hz) / 1e6:.3f} MHz"
        bandwidth = "—" if observation.bandwidth_hz is None else f"{observation.bandwidth_hz / 1e6:.2f} MHz"
        status = observation.status if observation.clean_onset else f"{observation.status} · truncated"
        return {
            "clock": time.strftime("%H:%M:%S"),
            "protocol": observation.protocol,
            "status": status,
            "confidence": f"{100 * observation.confidence:.0f}%",
            "duration": f"{observation.duration_us(capture.sample_rate_hz):.1f} µs",
            "frequency": frequency,
            "bandwidth": bandwidth,
            "rate": observation.bitrate or "—",
            "summary": observation.summary,
            "sensor": str(observation.sensor_index),
        }

    def _decode_loop(self):
        while not self._stop.is_set() or not self._decode_queue.empty():
            try:
                capture, signals = self._decode_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._record_capture(capture, signals)
                observations = self.pipeline.process(
                    signals,
                    capture.sample_rate_hz,
                    capture.center_freq_hz,
                    decode_sensor_count=self.decode_sensor_count,
                )
            except Exception as error:
                self.logger.exception("packet pipeline failed for capture %s", capture.capture_id)
                with self._stats_lock:
                    self._counts["processed"] += 1
                    self._counts["observations"] += 1
                    self._counts["unknown"] += 1
                    self._protocol_counts["Decoder error"] = self._protocol_counts.get("Decoder error", 0) + 1
                self.observationReady.emit(
                    {
                        "clock": time.strftime("%H:%M:%S"),
                        "protocol": "Decoder error",
                        "status": "unknown",
                        "confidence": "0%",
                        "duration": f"{capture.duration_seconds * 1e6:.1f} µs",
                        "frequency": f"{capture.center_freq_hz / 1e6:.3f} MHz",
                        "bandwidth": "—",
                        "rate": "—",
                        "summary": str(error),
                        "sensor": "—",
                    }
                )
                continue
            for observation in observations:
                with self._stats_lock:
                    self._counts["observations"] += 1
                    if observation.status == "decoded":
                        self._counts["decoded"] += 1
                    elif observation.status == "unknown":
                        self._counts["unknown"] += 1
                    else:
                        self._counts["classified"] += 1
                    self._protocol_counts[observation.protocol] = self._protocol_counts.get(observation.protocol, 0) + 1
                if self.show_unknown or observation.status != "unknown":
                    self.observationReady.emit(self._format_observation(observation, capture))
            with self._stats_lock:
                if any(observation.decoder != "feature classifier" for observation in observations):
                    self._counts["recognized"] += 1
                self._counts["processed"] += 1
            self.statsChanged.emit()

    @PyQt6.QtCore.pyqtSlot(
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QLineSeries,
        PyQt6.QtCharts.QValueAxis,
        PyQt6.QtCharts.QValueAxis,
    )
    def updateTimeChart(self, i_series, q_series, magnitude_series, time_axis, value_axis):
        with self._wave_lock:
            if self._wave is None or self._wave_consumed == self._wave_generation:
                return
            times, samples = self._wave
            self._wave_consumed = self._wave_generation
        i_series.replace([PyQt6.QtCore.QPointF(float(x), float(y)) for x, y in zip(times, samples.real)])
        q_series.replace([PyQt6.QtCore.QPointF(float(x), float(y)) for x, y in zip(times, samples.imag)])
        magnitude_series.replace([PyQt6.QtCore.QPointF(float(x), float(y)) for x, y in zip(times, np.abs(samples))])
        time_axis.setRange(0, max(float(times[-1]), 1.0))
        value_axis.setRange(-512, 511)

    def onAboutToQuit(self):
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._stop.set()
        for thread in self._decode_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=15)
        super().onAboutToQuit()

    def exec(self):
        self.initComplete.connect(self._on_framework_ready)
        self.initialize_pool(calibrate=False)
        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "packet-decoder-ui.qml",
            context_props={"iqcontrol": None, "observationModel": self.observation_model},
        )
        if self.args.duration > 0:
            PyQt6.QtCore.QTimer.singleShot(max(1, int(self.args.duration * 1000)), self.quit)
        return super().exec()


def main():
    app = PacketDecoderApplication(sys.argv)
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    interrupt_timer = PyQt6.QtCore.QTimer(app)
    interrupt_timer.start(250)
    interrupt_timer.timeout.connect(lambda: None)
    result = app.exec()
    app.logger.info(
        "final packet-decoder counts: %s; protocols=%s; Signal transport=%s "
        "(rolling complete-event rate %.2f/s)",
        app._counts,
        app._protocol_counts,
        app.pool.signal_capture_stats,
        app.capture_rate,
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
