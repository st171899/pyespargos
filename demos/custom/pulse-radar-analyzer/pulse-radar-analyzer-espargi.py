#!/usr/bin/env python
"""Mixed ESPARGOS IQ TX/RX pulse radar. See README.md for sensor selection."""

import argparse
import os
import pathlib
import sys
import threading
import time
from types import SimpleNamespace

# pyespargos repository root (this demo lives three levels below the checkout)
repository_root = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repository_root))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import numpy as np
import matplotlib.cm
import matplotlib.colors

import PyQt6.QtCore
import PyQt6.QtCharts

import espargos
from demos.custom.common.application import CombinedArrayMixin

from espargos import CHUNK_SAMPLES, DECIM_TO_FS, IQAccumCluster, SENSOR_COUNT
from espargos._iq_calibration.reciprocity_fit import DisconnectedLinks
from espargos.iq_tx_control import CalibrationMeasurementError
from demos.custom.common.iq_application import ESPARGOSIQApplication
from demos.custom.common.iq_waterfall import WaterfallImageProvider, power_db_row
from radar_tx import RadarTransmitter, configure_receivers, calibrate_radar_timing
from beamforming import AZIMUTH_DEG, ELEVATION_DEG, planar_power, horizontal_power
from clutter import RollingClutter
from radar_signal import chirp_reference, local_geometry, matched_filter_peaks

DISPLAY_SETTLE_SECONDS = 0.2
WATERFALL_HEIGHT = 300
ADC_FULL_SCALE = 512.0
DB_MIN, DB_MAX = -90.0, 0.0
AMPLITUDE_TARGET = 2**14

PHASE_MAPPER = matplotlib.cm.ScalarMappable(norm=matplotlib.colors.Normalize(vmin=-np.pi, vmax=np.pi, clip=True), cmap="twilight")


def receive_chirp_reference(waveform, config, sample_rate_hz=80e6):
    """Sample the 80 MSa/s replay reference on the receive ADC grid."""
    return chirp_reference(waveform, config, sample_rate_hz, 80e6)


def pool_complex_to_width(z, w):
    """Reduce/expand a complex spectrum to w pixels by coherent averaging."""
    n = z.shape[-1]
    if w == n:
        return z
    if w < n:
        edges = (np.arange(w) * n) // w
        return np.add.reduceat(z, edges, axis=-1)
    return z[..., (np.arange(w) * n) // w]


class EspargosPulseRadarAnalyzer(CombinedArrayMixin, ESPARGOSIQApplication):
    waterfallFrameReady = PyQt6.QtCore.pyqtSignal()
    displayModeChanged = PyQt6.QtCore.pyqtSignal()
    traceSampleCountChanged = PyQt6.QtCore.pyqtSignal()
    angleChanged = PyQt6.QtCore.pyqtSignal()
    boardSelectionChanged = PyQt6.QtCore.pyqtSignal()
    txOperationDone = PyQt6.QtCore.pyqtSignal(object)
    radarCalibrationDone = PyQt6.QtCore.pyqtSignal(object)
    txStateChanged = PyQt6.QtCore.pyqtSignal()
    radarMapChanged = PyQt6.QtCore.pyqtSignal(list)
    radarConfigChanged = PyQt6.QtCore.pyqtSignal()
    radarStatusChanged = PyQt6.QtCore.pyqtSignal()

    DEFAULT_CONFIG = {
        "display_mode": "power",
        "fft_size": 1024,
        "complete_only": False,
        "apply_calibration": True,
        "chirp_frames": 16,
        "bandwidth_hz": 40e6,
        "pings_per_256": 2,
        "tx_gain_db": -20,
        "remove_los": False,
        "clutter_mode": "manual",
        "delay_min": 0,
        "delay_max": 256,
        "radar_min_db": -50,
        "radar_max_db": 0,
        "radar_scale": "db",
    }

    # --- ping TX config ---
    PING_TX_DEFAULTS = {
        "chirp_frames": 16,
        "bandwidth_hz": 40e6,
        "pings_per_256": 2,
        "tx_gain_db": -20,
    }

    clutterStatusChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(self, argv):
        os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Material")
        parser = argparse.ArgumentParser(description="ESPARGOS Demo: Pulse radar analyzer", add_help=False)
        parser.add_argument("--tx-sensors", nargs="+", default=[], metavar="BOARD[.ID]", help="TX board names or BOARD.ID firmware sensor IDs (0–7); others receive")
        parser.add_argument("--frequency-mhz", type=float, default=2400.0)
        parser.add_argument("--tx-gain-a", type=int, choices=range(8), default=0)
        parser.add_argument("--tx-gain-b", type=int, choices=range(8), default=0)
        parser.add_argument("--tx-bandwidth", type=int, choices=RadarTransmitter.RF_BANDWIDTHS, default=40)
        parser.add_argument("--tx-output", choices=("antenna_l", "antenna_r", "reference", "isolated"), default="antenna_r")
        parser.add_argument("--tx-azimuth", type=float, default=0.0)
        parser.add_argument("--tx-elevation", type=float, default=0.0)
        parser.add_argument("--tx-squint", action="store_true")
        parser.add_argument("--tx-calibration", choices=("monitor", "reciprocity"), default="reciprocity")
        parser.add_argument("--calibration-path", choices=("reference", "antenna_l", "antenna_r"), default="reference")
        parser.add_argument("--timing-path", choices=("reference", "antenna_l", "antenna_r"), default="reference", help="TX/RX range-zero calibration path (reference network by default)")
        super().__init__(argv, argparse_parent=parser)
        self.radar_tx = None
        self._active_ping_config = None
        self._tx_busy = False
        self._tx_worker = None
        self._shutting_down = False
        self._tx_calibrating = False
        self._calibrated_tx_setup = None
        self._tx_calibration_valid = False
        self._radar_timing = None
        self._tx_status = "Select transmit sensors, then apply and start."
        self._tx_settings = {key: getattr(self.args, key) for key in ("frequency_mhz", "tx_gain_a", "tx_gain_b", "tx_bandwidth", "tx_output", "tx_azimuth", "tx_elevation", "tx_squint", "tx_calibration", "calibration_path", "timing_path")}
        self._tx_settings.update({key: self.get_initial_config("app", key) for key in self.PING_TX_DEFAULTS})
        self._tx_settings["tx_sensors"] = list(self.args.tx_sensors)
        self.txOperationDone.connect(self._finish_tx_operation)
        self.radarCalibrationDone.connect(self._finish_radar_calibration)
        self.txStateChanged.connect(self.radarConfigChanged.emit)

        self.iqcontrol = None  # IQController QML adapter, created once the pool is up
        self.active_board = 0
        # clusters delivered by the pool's processing worker, drained by the
        # render thread (bounded so a stalled render cannot grow it unboundedly)
        self._pending_lock = threading.Lock()
        self._pending_clusters = []
        self._display_config = {}
        self._display_schedule = None
        self._display_epoch = None
        self._display_epoch_time = -1.0
        self.display_mode = self.get_initial_config("app", "display_mode")
        self.fft_size = int(self.get_initial_config("app", "fft_size"))
        self.complete_only = bool(self.get_initial_config("app", "complete_only"))
        self.display_width = CHUNK_SAMPLES
        self._win = np.hanning(self.fft_size).astype(np.float32)
        self._fft_scale = max(float(np.sum(self._win)) * ADC_FULL_SCALE, 1.0)
        self.block_acc = {}  # block_id -> {antid: {slot: iq}}
        self.last_display_block = -1
        self.block_time = {}  # block_id -> last-update monotonic time (stale eviction)
        self.rows_rendered = [0] * SENSOR_COUNT
        self.render_running = False
        self._first_sets_logged = False
        self._sample_window_lock = threading.Lock()
        self._sample_windows = [np.zeros(self.fft_size, dtype=np.complex64) for _ in range(SENSOR_COUNT)]
        self._sample_window_present = [False] * SENSOR_COUNT
        self._sample_window_generation = 0
        self._time_trace_consumed = [-1] * SENSOR_COUNT
        self._constellation_consumed = [-1] * SENSOR_COUNT
        self._spectrum_consumed = [-1] * SENSOR_COUNT
        self._spectrum_sample_rate_hz = DECIM_TO_FS[1]
        self._spectrum_center_hz = 2437e6
        self._spectrum_config_read_at = 0.0
        self._startup_abort_seconds = 20.0
        self._startup_abort_timer = PyQt6.QtCore.QTimer(self)
        self._startup_abort_timer.setSingleShot(True)
        self._startup_abort_timer.timeout.connect(self._abort_if_stuck)
        self._tx_lock = threading.Lock()
        self._tx_waveform = None
        self._angle_estimate_deg = float("nan")
        self._elevation_estimate_deg = float("nan")
        self._angle_spectrum = np.zeros(361, dtype=np.float32)
        self._angle_status = "Run IQ calibration to estimate angle."
        self._radar_status = "Waiting for IQ samples."
        self._radar_angle_size = 256
        self._radar_delay_size = 256
        self._radar_last_render_time = 0.0
        self._clutter_lock = threading.Lock()
        self._automatic_clutter = RollingClutter()
        self._clutter_mode = str(self.appconfig.get("clutter_mode") or "manual")
        self._remove_los = False
        self._clutter_calibrating = False
        self._clutter_calibrated = False
        self._clutter_collected_blocks = 0
        self._clutter_target_blocks = 200
        self._clutter_buffer = {antid: [] for antid in range(SENSOR_COUNT)}
        self._clutter_reference = {}
        self._clutter_status_text = "Clutter removal: Inactive"

        self.providers = [WaterfallImageProvider(CHUNK_SAMPLES, WATERFALL_HEIGHT) for _ in range(SENSOR_COUNT)]
        for antid in range(SENSOR_COUNT):
            self.engine.addImageProvider(f"display{antid}", self.providers[antid])

        self.appConfigChanged.connect(self._on_app_config_changed)
        self.remove_los = bool(self.appconfig.get("remove_los"))

    def _create_receiver(self, owner):
        self.radar_tx = RadarTransmitter(owner, self.args)
        self.radar_tx.configure()
        return owner.iq

    def _rx_sensor_ids(self):
        tx = getattr(self, "radar_tx", None)
        return tx.receivers(self.active_board) if tx else tuple(range(SENSOR_COUNT))

    def _reference_ping_config(self):
        return self._active_ping_config or self._current_ping_config()

    # ---- configuration ----

    def _on_app_config_changed(self, newcfg):
        if "display_mode" in newcfg:
            self._set_display_mode(str(newcfg["display_mode"]))
        if "fft_size" in newcfg:
            self._set_fft_size(int(newcfg["fft_size"]))
        if "complete_only" in newcfg:
            self.complete_only = bool(newcfg["complete_only"])
        if "clutter_mode" in newcfg:
            mode = str(newcfg["clutter_mode"])
            if mode in ("manual", "automatic") and mode != self._clutter_mode:
                self._clutter_mode = mode
                self._reset_clutter()
        if "remove_los" in newcfg:
            self.remove_los = bool(newcfg["remove_los"])
        if "apply_calibration" in newcfg:
            if self.iqcontrol is not None:
                self.iqcontrol.set_apply_calibration(bool(newcfg["apply_calibration"]))
        if any(key in newcfg for key in ("delay_min", "delay_max", "radar_min_db", "radar_max_db", "radar_scale")):
            self.radarConfigChanged.emit()

    def _on_iq_calibration_changed(self):
        self.block_acc.clear()
        self.block_time.clear()
        self.last_display_block = -1
        self._display_epoch = None
        with self._pending_lock:
            self._pending_clusters.clear()
        self._angle_status = "IQ calibration complete. Angle estimate is updating."
        self.angleChanged.emit()

    def _set_display_mode(self, mode):
        if mode != self.display_mode:
            self.display_mode = mode
            self.block_acc.clear()
            self.block_time.clear()
            self.last_display_block = -1
            for p in self.providers:
                p.clear()
            if mode in ("time", "ping", "constellation", "spectrum", "angle", "radar"):
                self._publish_sample_windows({})
            self.waterfallFrameReady.emit()
            self.displayModeChanged.emit()

    @property
    def remove_los(self):
        return getattr(self, "_remove_los", bool(self.appconfig.get("remove_los")))

    @remove_los.setter
    def remove_los(self, enabled):
        enabled = bool(enabled)
        if enabled != getattr(self, "_remove_los", False):
            self._remove_los = enabled
            self._reset_clutter()

    @PyQt6.QtCore.pyqtProperty(str, notify=clutterStatusChanged)
    def clutterMode(self):
        return self._clutter_mode

    def _reset_clutter(self):
        """Never carry a background between boards, modes, or RX/TX epochs."""
        with self._clutter_lock:
            self._automatic_clutter.clear()
            self._clutter_calibrating = False
            self._clutter_calibrated = False
            self._clutter_collected_blocks = 0
            self._clutter_buffer = {}
            self._clutter_reference = {}
            self._clutter_status_text = "Clutter removal: Inactive" if not self._remove_los else "Automatic clutter: 16 chunks / 0.4 s; waiting for IQ" if self._clutter_mode == "automatic" else "Manual clutter: press Calibrate Clutter (200 chunks)"
        # Buffered display chunks may already contain automatic residuals.
        self.block_acc.clear()
        self.block_time.clear()
        self.last_display_block = -1
        self.clutterStatusChanged.emit()

    @PyQt6.QtCore.pyqtProperty(str, notify=clutterStatusChanged)
    def clutterStatusText(self):
        with self._clutter_lock:
            return self._clutter_status_text

    @PyQt6.QtCore.pyqtSlot()
    def calibrateClutter(self):
        """Button-triggered, fixed background from 200 received IQ chunks."""
        self._clutter_mode = "manual"
        self._remove_los = True
        self._reset_clutter()
        with self._clutter_lock:
            self._clutter_calibrating = True
            self._clutter_calibrated = False
            self._clutter_collected_blocks = 0
            self._clutter_target_blocks = 200
            self._clutter_buffer = {antid: [] for antid in range(SENSOR_COUNT)}
            self._clutter_reference = {}
            self._clutter_status_text = "Calibrating clutter..."
            self._remove_los = True
        if hasattr(self, "appconfig"):
            self.appconfig.set({"remove_los": True, "clutter_mode": "manual"})
        self.clutterStatusChanged.emit()

    def _angle_geometry(self):
        """Physical aperture of the selected receive board only."""
        # MULTI-BOARD GRID: replace this board-local map with measured antenna
        # positions (including board offsets/orientations) when arrays are
        # mounted on a known regular grid. Also change _drain_sets to retain
        # (board, antenna) identities and _angle_fine_corrected_traces to apply
        # each board's calibration. Angle and heatmap processing both consume
        # this geometry; concatenating local IDs would alias distinct sensors.
        # The YAML indexing matrix alone is not a measured board placement.
        if not hasattr(self, "pool") or not 0 <= self.active_board < len(self.pool.boards):
            return np.full((2, 4), -1, dtype=int)
        return local_geometry(self.pool.boards[self.active_board].revision, self._rx_sensor_ids())

    def _collect_clutter_snapshot(self, raw_traces):
        """Collect complete raw-IQ periods for a static-environment baseline."""
        expected = self._rx_sensor_ids()
        if not expected or not raw_traces or any(antid not in raw_traces for antid in expected):
            return
        common_length = min(np.asarray(raw_traces[antid]).size for antid in expected)
        complete_periods = common_length // CHUNK_SAMPLES
        if complete_periods <= 0:
            return

        calibration_finished = False
        with self._clutter_lock:
            if not self._clutter_calibrating:
                return
            for period_index in range(complete_periods):
                if self._clutter_collected_blocks >= self._clutter_target_blocks:
                    break
                start = period_index * CHUNK_SAMPLES
                end = start + CHUNK_SAMPLES
                for antenna_id in expected:
                    trace = np.asarray(raw_traces[antenna_id], dtype=np.complex64).reshape(-1)
                    self._clutter_buffer.setdefault(antenna_id, []).append(trace[start:end].copy())
                self._clutter_collected_blocks += 1

            if self._clutter_collected_blocks >= self._clutter_target_blocks:
                references = {}
                for antenna_id in expected:
                    history = self._clutter_buffer.get(antenna_id, [])
                    if len(history) != self._clutter_target_blocks:
                        break
                    references[antenna_id] = np.mean(np.stack(history), axis=0, dtype=np.complex128).astype(np.complex64)
                if len(references) == len(expected):
                    self._clutter_reference = references
                    self._clutter_calibrated = True
                    self._clutter_status_text = "Clutter removal active"
                else:
                    self._clutter_calibrated = False
                    self._clutter_status_text = "Clutter calibration incomplete; retry with all antennas receiving."
                self._clutter_calibrating = False
                self._clutter_buffer.clear()
                calibration_finished = True
        if calibration_finished:
            self.clutterStatusChanged.emit()

    def _cancel_line_of_sight(self, raw_traces):
        """Subtract the configured, phase-aligned static environment template.

        Captured references represent one 256-sample raw-IQ period. Longer
        windows are tiled by that period so their timing stays aligned.
        """
        if not raw_traces or not self._remove_los:
            return raw_traces
        if getattr(self, "_clutter_mode", "manual") == "automatic":
            return raw_traces  # Already cancelled once in _render_block_rows.
        with self._clutter_lock:
            if self._clutter_calibrating or not self._clutter_calibrated:
                return raw_traces
            reference_snapshot = {antid: ref.copy() for antid, ref in self._clutter_reference.items()}

        cleaned = {}
        for antenna_id, values in raw_traces.items():
            trace = np.asarray(values, dtype=np.complex64).reshape(-1)
            reference = reference_snapshot.get(antenna_id)
            if reference is None or reference.size != CHUNK_SAMPLES:
                cleaned[antenna_id] = trace.copy()
                continue
            repeated_reference = np.resize(reference, trace.size)
            cleaned[antenna_id] = (trace - repeated_reference).astype(np.complex64)
        return cleaned

    def _render_radar_heatmap(self, traces):
        """Render available samples, using calibration and matched filtering when available."""
        config = self._reference_ping_config()
        pings_per_256 = max(1, min(config.get("pings_per_256", 2), CHUNK_SAMPLES))
        burst_length = CHUNK_SAMPLES // pings_per_256

        corrected, calibration_ready = self._angle_corrected_traces(traces)

        if corrected is None or corrected.shape[0] < SENSOR_COUNT:
            self._radar_status = "❌ Processing Error: Calibrated array matrix size invalid."
            self.radarStatusChanged.emit()
            return

        # The calibration helper pads to fft_size; only received samples are
        # measurements. Do not put that padding into the delay axis or scale.
        sample_count = min((len(trace) for trace in traces.values() if len(trace)), default=CHUNK_SAMPLES)
        sample_count = min(sample_count, corrected.shape[1])
        if sample_count <= 0:
            return

        # Extract live configuration states dynamically
        s_min, s_max = EspargosPulseRadarAnalyzer._radar_delay_bounds(self, sample_count, burst_length)

        sliced_traces2d = corrected[:, s_min:s_max]
        sliced_traces = sliced_traces2d.shape[1]

        geometry = self._angle_geometry()
        row_count, column_count = geometry.shape
        if row_count == 0 or column_count == 0:
            return

        spatial_samples = np.zeros((row_count, column_count, sliced_traces), dtype=np.complex64)
        for row in range(row_count):
            for column in range(column_count):
                antenna_id = int(geometry[row, column])
                if 0 <= antenna_id < sliced_traces2d.shape[0]:
                    spatial_samples[row, column] = sliced_traces2d[antenna_id]

        # Matched-filter output is scaled by chirp energy. Divide by the RMS
        # correlation gain to recover received-IQ amplitude before dBFS.
        if self._tx_waveform is not None:
            config = self._reference_ping_config()
            tx_reference, _ = receive_chirp_reference(self._tx_waveform, config, getattr(self, "_spectrum_sample_rate_hz", 80e6))
            chirp_length = tx_reference.size
            matched_energy = float(np.vdot(tx_reference, tx_reference).real)
            matched_gain = np.sqrt(matched_energy * chirp_length)
            if matched_gain > 1e-12:
                spatial_samples /= matched_gain

        # RX steering scans candidate directions independently of the TX beam.
        # The shader expects uniform horizontal direction-cosine bins. The
        # actual RF wavelength matters: a raw FFT assumes d = wavelength/2.
        power_map = horizontal_power(spatial_samples, getattr(self, "_spectrum_center_hz", 2437e6), espargos.constants.ANTENNA_SEPARATION)

        # fft() is unnormalized and antennas add coherently by row. Normalize
        # against each configured row's maximum coherent full-scale gain so a
        # full-scale, boresight return maps to approximately 0 dBFS rather
        # than saturating every pixel regardless of the selected color limits.
        active_per_row = np.count_nonzero(geometry >= 0, axis=1).astype(np.float64)
        reference_power = max(float(np.sum(active_per_row**2)) * ADC_FULL_SCALE**2, 1e-12)
        power_db = 10.0 * np.log10(np.maximum(power_map / reference_power, 1e-12))
        configured_min = self.appconfig.get("radar_min_db") if hasattr(self, "appconfig") else None
        configured_max = self.appconfig.get("radar_max_db") if hasattr(self, "appconfig") else None
        display_min_db = float(-50 if configured_min is None else configured_min)
        display_max_db = float(0 if configured_max is None else configured_max)
        if display_max_db <= display_min_db:
            display_max_db = display_min_db + 1.0
        if hasattr(self, "appconfig") and self.appconfig.get("radar_scale") == "linear":
            lower, upper = 10 ** (display_min_db / 10), 10 ** (display_max_db / 10)
            norm_map = np.clip((power_map / reference_power - lower) / (upper - lower), 0.0, 1.0)
        else:
            norm_map = np.clip((power_db - display_min_db) / (display_max_db - display_min_db), 0.0, 1.0)

        colormap = matplotlib.colormaps.get_cmap("viridis")
        color_data = colormap(np.transpose(norm_map))
        np_data = (color_data * 255).astype(np.uint8)

        # Expose the texture dimensions to QML.
        dimensions = (int(np_data.shape[1]), int(np_data.shape[0]))
        if dimensions != (self._radar_angle_size, self._radar_delay_size):
            self._radar_angle_size, self._radar_delay_size = dimensions
            self.radarConfigChanged.emit()

        if not traces:
            self._radar_status = "Waiting for IQ samples."
        else:
            processing = "Matched-filtered" if self._tx_waveform is not None else "Raw IQ (TX inactive)"
            calibration = "calibrated" if calibration_ready else "uncalibrated"
            self._radar_status = f"{processing} · {calibration} · {len(traces)}/{SENSOR_COUNT} antennas"
        self.radarStatusChanged.emit()

        # QML reads the current angleSize and delaySize before uploading RGBA.
        image_vector = np_data.flatten().tolist()
        self.radarMapChanged.emit(image_vector)

    def _radar_delay_bounds(self, sample_count=None, burst_length=None):
        """Return the effective UI delay range limited to one pulse period."""
        if sample_count is None:
            sample_count = CHUNK_SAMPLES
        if burst_length is None:
            config = self._current_ping_config()
            burst_length = CHUNK_SAMPLES // max(1, min(config.get("pings_per_256", 2), CHUNK_SAMPLES))
        appconfig = getattr(self, "appconfig", None)
        configured_min = appconfig.get("delay_min") if appconfig is not None else 0
        configured_max = appconfig.get("delay_max") if appconfig is not None else min(int(sample_count), CHUNK_SAMPLES)
        delay_min = max(0, int(configured_min if configured_min is not None else 0))
        delay_max = int(configured_max if configured_max is not None else burst_length)
        # The radar delay axis may use any part of the one-chunk payload. The
        # periodic TX burst length is not the display-axis length.
        upper = min(int(sample_count), CHUNK_SAMPLES)
        delay_min = min(delay_min, max(0, upper - 1))
        delay_max = max(delay_min + 1, min(delay_max, upper))
        return delay_min, delay_max

    def _angle_fine_corrected_traces(self, traces):
        """Apply the existing relative-phase calibration to all antenna traces."""
        corrected = []
        calibration_ready = self.iqcontrol is not None
        for antid in range(SENSOR_COUNT):
            source = np.asarray(traces.get(antid, np.zeros(0, dtype=np.complex64)), dtype=np.complex64).reshape(-1)
            trace = np.zeros(max(0, self.fft_size), dtype=np.complex64)
            copy_count = min(trace.size, source.size)
            if copy_count:
                trace[:copy_count] = source[:copy_count]
            if self.iqcontrol is not None and antid in self._rx_sensor_ids():
                try:
                    correction = self.iqcontrol.cal_correction(self.active_board, antid, self.fft_size)
                except Exception:
                    correction = None
                if correction is not None:
                    spectrum = np.fft.fftshift(np.fft.fft(trace))
                    trace = np.fft.ifft(np.fft.ifftshift(spectrum * correction)).astype(np.complex64)
                else:
                    calibration_ready = False
            corrected.append(trace)
        return np.asarray(corrected), calibration_ready

    def _angle_corrected_traces(self, traces):
        return self._angle_fine_corrected_traces(traces)

    def _update_angle_estimate(self, traces):
        # Use raw received IQ when TX is off; otherwise use the matched-filter
        # samples supplied by _publish_sample_windows.
        corrected, calibration_ready = self._angle_corrected_traces(traces)

        if not calibration_ready:
            self._angle_estimate_deg = float("nan")
            self._elevation_estimate_deg = float("nan")
            self._angle_status = "Run IQ calibration before estimating angle."
            self.angleChanged.emit()
            return
        if not set(self._rx_sensor_ids()).issubset(traces):
            return

        geometry = self._angle_geometry()
        if np.count_nonzero(geometry >= 0) < 4:
            self._angle_estimate_deg = float("nan")
            self._elevation_estimate_deg = float("nan")
            self._angle_status = "Configured geometry has fewer than four active antennas."
            self.angleChanged.emit()
            return

        row_count, column_count = geometry.shape
        if row_count == 0 or column_count == 0 or self.fft_size <= 0:
            self._angle_status = "Configured antenna geometry is empty."
            self.angleChanged.emit()
            return
        sample_count = corrected.shape[1]
        if sample_count == 0:
            self._angle_status = "IQ sample window is empty."
            self.angleChanged.emit()
            return
        spatial_samples = np.zeros((row_count, column_count, sample_count), dtype=np.complex64)
        for row in range(row_count):
            for column in range(column_count):
                antenna_id = geometry[row, column]
                if 0 <= antenna_id < corrected.shape[0]:
                    spatial_samples[row, column] = corrected[antenna_id]

        # Conventional delay-and-sum response (ATWC Lecture 4, Eqs. 4.1–4.2).
        # Evaluate the spatial DTFT directly at physical look directions.
        power_2d = planar_power(spatial_samples, self._spectrum_center_hz, espargos.constants.ANTENNA_SEPARATION)
        if np.max(power_2d) <= 1e-12:
            self._angle_estimate_deg = float("nan")
            self._elevation_estimate_deg = float("nan")
            self._angle_status = "Insufficient signal strength for angle estimation."
            self.angleChanged.emit()
            return

        peak_row, peak_column = np.unravel_index(int(np.argmax(power_2d)), power_2d.shape)
        self._angle_estimate_deg = float(AZIMUTH_DEG[peak_column])
        self._elevation_estimate_deg = float(ELEVATION_DEG[peak_row])

        # A physical azimuth cut at the estimated elevation. All chart values
        # and the angle readout use the same response and coordinate mapping.
        spectrum = power_2d[peak_row]
        self._angle_spectrum = (spectrum / np.max(spectrum)).astype(np.float32)
        processing = "Matched-filtered IQ" if self._tx_waveform is not None else "Received IQ (TX inactive)"
        self._angle_status = f"{processing}: conventional beamforming; azimuth cut at {self._elevation_estimate_deg:.1f}° elevation."
        self.angleChanged.emit()

    def _set_fft_size(self, n):
        if n == self.fft_size or n < CHUNK_SAMPLES or n % CHUNK_SAMPLES != 0:
            return
        self.fft_size = n
        self._win = np.hanning(n).astype(np.float32)
        self._fft_scale = max(float(np.sum(self._win)) * ADC_FULL_SCALE, 1.0)
        self.block_acc = {}
        self.last_display_block = -1
        self.block_time = {}
        for p in self.providers:
            p.clear()
        self._publish_sample_windows({})
        self.waterfallFrameReady.emit()
        self.traceSampleCountChanged.emit()

    def _ping_config_value(self, key, default):
        value = self.appconfig.get(key, default)
        return default if value is None else value

    def _current_ping_config(self):
        """Return the live UI-config snapshot used to build the TX ping waveform."""
        return {
            "chirp_frames": int(self._ping_config_value("chirp_frames", self.PING_TX_DEFAULTS["chirp_frames"])),
            "bandwidth_hz": float(self._ping_config_value("bandwidth_hz", self.PING_TX_DEFAULTS["bandwidth_hz"])),
            "pings_per_256": int(self._ping_config_value("pings_per_256", self.PING_TX_DEFAULTS["pings_per_256"])),
            "tx_gain_db": float(self._ping_config_value("tx_gain_db", self.PING_TX_DEFAULTS["tx_gain_db"])),
        }

    def _build_ping_waveform(self, config=None):
        """
        Builds a high-precision, continuous complex reference template that
        precisely tracks the discrete bin boundaries used by the C hardware generator.
        """
        cfg = self._current_ping_config() if config is None else config
        chirp_frames = int(cfg["chirp_frames"])
        bw_hz = float(cfg["bandwidth_hz"])
        if not np.isfinite(bw_hz) or not 0 < bw_hz <= 80e6:
            raise ValueError("Chirp bandwidth must be greater than 0 and at most 80 MHz")
        pings_per_256 = int(cfg["pings_per_256"])

        # Read dynamic sampling rate from the interface config
        fs_hz = float(cfg.get("fs_hz", 80e6))

        CHUNK_SAMPLES = 256
        if pings_per_256 <= 0 or CHUNK_SAMPLES % pings_per_256:
            raise ValueError("Chirps per 256 must divide 256 (1, 2, 4, 8, …, 256)")
        burst_length = CHUNK_SAMPLES // pings_per_256
        chirp_frames = max(1, min(chirp_frames, burst_length))

        # 1. Replicate the C code's discrete bin conversion logic
        f_bin_resolution = fs_hz / CHUNK_SAMPLES
        s_start_bin = int(np.round((-bw_hz / 2.0) / f_bin_resolution))
        s_stop_bin = int(np.round((bw_hz / 2.0) / f_bin_resolution))

        # 2. Map those discrete bins back out to physical analog frequencies
        # This step aligns your continuous template with the hardware's true output
        f_start = s_start_bin * f_bin_resolution
        f_end = s_stop_bin * f_bin_resolution

        # 3. Generate high-precision continuous phase tracking arrays
        t = np.arange(chirp_frames, dtype=np.float64) / fs_hz
        chirp_duration = chirp_frames / fs_hz

        # Calculate sweep rate using the updated discrete bounds
        k = (f_end - f_start) / chirp_duration
        phase = 2.0 * np.pi * (f_start * t + 0.5 * k * (t**2))

        # 4. Continuous exponential synthesis
        active_chirp = np.exp(1j * phase).astype(np.complex64)

        # Pad tail silence to complete the burst window
        zero_samples = max(0, burst_length - chirp_frames)
        single_burst = np.concatenate((active_chirp, np.zeros(zero_samples, dtype=np.complex64)))

        # Repeat block across the full buffer sequence
        waveform_256 = np.tile(single_burst, pings_per_256)[:CHUNK_SAMPLES]

        # Scale to the UI amplitude target configuration
        amplitude_target = float(cfg.get("amplitude_target", 1.0))
        waveform_scaled = waveform_256 * amplitude_target

        return waveform_scaled

    def _matched_filter(self, iq):
        """Return chirp correlation and pulse starts in RX-window coordinates."""
        if self._tx_waveform is None:
            return np.zeros(len(iq), dtype=np.complex64), np.zeros(len(iq), dtype=np.float32), []
        config = self._reference_ping_config()
        reference, burst_length = receive_chirp_reference(self._tx_waveform, config, getattr(self, "_spectrum_sample_rate_hz", 80e6))
        received = np.asarray(iq, dtype=np.complex64)
        if reference.size == 0 or received.size == 0:
            return np.zeros(received.size, dtype=np.complex64), np.zeros(received.size, dtype=np.float32), []
        # Full correlation index M-1 is lag zero: output k now means a
        # reference starting at received sample k, for odd and even lengths.
        correlation = np.convolve(received, np.conj(reference[::-1]), mode="full")[reference.size - 1 : reference.size - 1 + received.size]
        plotted_correlation = np.abs(correlation).astype(np.float32)
        peaks = matched_filter_peaks(received, reference, correlation)
        return correlation, plotted_correlation, peaks

    def _angle_matched_trace(self, iq):
        """Return complex matched-filter output for the active chirp."""
        if self._tx_waveform is None:
            return np.asarray(iq, dtype=np.complex64)
        config = self._reference_ping_config()
        reference, _ = receive_chirp_reference(self._tx_waveform, config, getattr(self, "_spectrum_sample_rate_hz", 80e6))
        received = np.asarray(iq, dtype=np.complex64)
        if reference.size == 0 or received.size == 0:
            return received
        correlation = np.convolve(received, np.conj(reference[::-1]), mode="full")
        return correlation[reference.size - 1 : reference.size - 1 + received.size].astype(np.complex64)

    @PyQt6.QtCore.pyqtProperty("QVariantMap", notify=txStateChanged)
    def txSettings(self):
        return self._tx_settings

    @PyQt6.QtCore.pyqtProperty("QVariantList", constant=True)
    def txRFBandwidths(self):
        return list(RadarTransmitter.RF_BANDWIDTHS)

    @PyQt6.QtCore.pyqtProperty("QVariantList", notify=txStateChanged)
    def txBoardNames(self):
        return list(self.pool.names) if hasattr(self, "pool") else []

    @PyQt6.QtCore.pyqtProperty(bool, notify=txStateChanged)
    def txBusy(self):
        return self._tx_busy or (self.iqcontrol is not None and self.iqcontrol.referenceBusy)

    @PyQt6.QtCore.pyqtProperty(str, notify=txStateChanged)
    def txStatus(self):
        return self._tx_status

    @PyQt6.QtCore.pyqtSlot(str, "QVariant")
    def setTxSetting(self, key, value):
        if not self.txBusy and key in self._tx_settings and key != "tx_sensors":
            integer_keys = {"tx_gain_a", "tx_gain_b", "tx_bandwidth", "chirp_frames", "pings_per_256"}
            if key in integer_keys:
                value = int(value)
            self._tx_settings[key] = value
            if key == "tx_calibration" and value == "monitor":
                self._tx_settings["calibration_path"] = "reference"
            self._check_tx_settings_calibration()
            self.txStateChanged.emit()

    @PyQt6.QtCore.pyqtSlot(float, float)
    def steerTx(self, u, v):
        if self.txBusy or not np.isfinite([u, v]).all():
            return
        norm = max(1.0, np.hypot(u, v))
        u, v = u / norm, v / norm
        self._tx_settings["tx_azimuth"] = float(np.degrees(np.arctan2(u, np.sqrt(max(0, 1 - u * u - v * v)))))
        self._tx_settings["tx_elevation"] = float(np.degrees(np.arcsin(v)))
        self.txStateChanged.emit()

    @PyQt6.QtCore.pyqtSlot(str, int, bool)
    def setTxSensor(self, board, antenna, selected):
        if self.txBusy or board not in self.txBoardNames:
            return
        tokens = set(self._tx_settings["tx_sensors"])
        for a in (range(8) if antenna == -1 else [antenna]):
            if not 0 <= a < 8:
                return
            token = f"{board}.{a}"
            if selected:
                tokens.add(token)
            else:
                tokens.discard(token)
        self._tx_settings["tx_sensors"] = sorted(tokens)
        self._check_tx_settings_calibration()
        self.txStateChanged.emit()

    @staticmethod
    def _tx_setup(settings):
        # Method/path select the next measurement; editing them changes no
        # running hardware and cannot invalidate an existing measurement.
        keys = ("frequency_mhz", "tx_gain_a", "tx_gain_b", "tx_bandwidth", "tx_output")
        return tuple(settings[k] for k in keys) + (tuple(sorted(settings["tx_sensors"])), settings.get("timing_path", "reference"))

    def _check_tx_settings_calibration(self):
        if self.iqcontrol is not None and self._tx_setup(self._tx_settings) != self._calibrated_tx_setup:
            self.iqcontrol._set_sync_needed(True)

    @PyQt6.QtCore.pyqtSlot()
    def startTx(self):
        if self._tx_busy or not self.ready:
            return
        if self.iqcontrol is None or self.iqcontrol.referenceBusy:
            self._tx_status = "Wait for IQ synchronization to finish."
            self.txStateChanged.emit()
            return
        settings = self._tx_settings.copy()
        try:
            candidate = RadarTransmitter(self.pool, SimpleNamespace(**settings))
            if not candidate.antennas:
                raise ValueError("Select at least one TX sensor in the Transmission tab")
            cfg = {key: settings[key] for key in self.PING_TX_DEFAULTS}
            waveform = self._build_ping_waveform(cfg)
        except Exception as error:
            self._tx_status = str(error)
            self.txStateChanged.emit()
            return

        def start():
            if self._tx_setup(settings) != self._calibrated_tx_setup:
                # Applying a different RF setup retires the old fit. Merely
                # returning the controls to earlier values cannot revive it.
                self._calibrated_tx_setup = None
                self._tx_calibration_valid = False
            self._stop_ping_tx()
            self.radar_tx = candidate
            candidate.configure()
            reference = candidate.start(waveform, cfg["tx_gain_db"])
            valid = self._tx_setup(settings) == self._calibrated_tx_setup and candidate.calibration_valid()
            return reference, cfg, bool(valid)

        if not self.iqcontrol._begin_reference():
            return
        self._tx_calibrating = True
        self._run_tx_operation(start, "Applying TX settings…")

    @PyQt6.QtCore.pyqtSlot()
    def stopTx(self):
        if not self.txBusy:
            self._run_tx_operation(self._stop_ping_tx, "Stopping TX…")

    def _run_tx_operation(self, operation, status):
        self._tx_busy = True
        self._tx_status = status
        self.txStateChanged.emit()

        def run():
            try:
                result = operation()
            except Exception as error:
                self.logger.exception("Radar TX operation failed")
                try:
                    self._stop_ping_tx()
                except Exception as stop_error:
                    error = RuntimeError(f"{error}; could not confirm TX stop: {stop_error}")
                self.txOperationDone.emit((None, str(error)))
            else:
                self.txOperationDone.emit((result, None))

        self._tx_worker = threading.Thread(target=run, name="radar-tx")
        self._tx_worker.start()

    def _finish_tx_operation(self, outcome):
        result, error = outcome
        self.block_acc.clear()
        self.block_time.clear()
        self.last_display_block = -1
        with self._pending_lock:
            self._pending_clusters.clear()
        if self.iqcontrol is not None:
            if self._tx_calibrating:
                valid = error is None and result is not None and result[2]
                self._tx_calibration_valid = valid
                self.iqcontrol._end_reference(valid)
            else:
                self.iqcontrol._correction_cache.clear()
        self._tx_calibrating = False
        if result is not None:
            self._tx_waveform, self._active_ping_config = result[:2]
            self.appconfig.set(self._active_ping_config)
            if self.remove_los:
                self._reset_clutter()
        calibrated = self.iqcontrol is not None and not self.iqcontrol.syncNeeded
        self._tx_status = (
            f"TX failed: {error}" if error else (("ESPARGOS TX running · TX/RX phase and timing calibrated." if calibrated else "ESPARGOS TX running · Use Sync + Calibration to calibrate TX and RX.") if result is not None else "TX stopped.")
        )
        self._angle_status = self._tx_status
        self._tx_busy = False
        self.angleChanged.emit()
        self.txStateChanged.emit()

    @PyQt6.QtCore.pyqtSlot(float, int, float, int)
    def _start_ping_tx(self, gain, chirp_frames, bandwidth_hz, pings_per_256):
        self._tx_settings.update(tx_gain_db=gain, chirp_frames=chirp_frames, bandwidth_hz=bandwidth_hz, pings_per_256=pings_per_256)
        self.startTx()

    def _stop_ping_tx(self):
        """Synchronous mute for the worker and application shutdown."""
        with self._tx_lock:
            if self.radar_tx is not None:
                self.radar_tx.stop()
            self._tx_waveform = None
            self._active_ping_config = None

    @PyQt6.QtCore.pyqtSlot(int)
    def set_display_width(self, w):
        """Follow the window size: waterfall pixel width per sensor."""
        w = max(128, min(2048, (int(w) // 32) * 32))
        if w == self.display_width:
            return
        self.display_width = w
        for p in self.providers:
            p.resize(w)
        self.waterfallFrameReady.emit()

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return SENSOR_COUNT

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def timeDomain(self):
        return self.display_mode in ("time", "ping")

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def pingMode(self):
        return self.display_mode == "ping"

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def angleMode(self):
        return self.display_mode == "angle"

    @PyQt6.QtCore.pyqtProperty(float, notify=angleChanged)
    def angleEstimate(self):
        return self._angle_estimate_deg

    @PyQt6.QtCore.pyqtProperty(float, notify=angleChanged)
    def elevationEstimate(self):
        return self._elevation_estimate_deg

    @PyQt6.QtCore.pyqtProperty(str, notify=angleChanged)
    def angleStatus(self):
        return self._angle_status

    @PyQt6.QtCore.pyqtProperty(bool, notify=txStateChanged)
    def txActive(self):
        return getattr(self, "_tx_waveform", None) is not None

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def constellation(self):
        return self.display_mode == "constellation"

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def spectrum(self):
        return self.display_mode == "spectrum"

    @PyQt6.QtCore.pyqtProperty(int, notify=traceSampleCountChanged)
    def traceSampleCount(self):
        return self.fft_size

    @PyQt6.QtCore.pyqtProperty(float, constant=True)
    def adcFullScale(self):
        return ADC_FULL_SCALE

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def boardCount(self):
        return len(self.receiver.boards) if hasattr(self, "pool") else 1

    @PyQt6.QtCore.pyqtProperty("QVariantList", constant=True)
    def boardHosts(self):
        return [b.host for b in self.receiver.boards] if hasattr(self, "pool") else []

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def radarMode(self):
        return self.display_mode == "radar"

    @PyQt6.QtCore.pyqtProperty(str, notify=radarStatusChanged)
    def radarStatus(self):
        return self._radar_status

    @PyQt6.QtCore.pyqtProperty(float, notify=radarConfigChanged)
    def radarMinDb(self):
        value = self.appconfig.get("radar_min_db")
        return -50.0 if value is None else float(value)

    @PyQt6.QtCore.pyqtProperty(float, notify=radarConfigChanged)
    def radarMaxDb(self):
        value = self.appconfig.get("radar_max_db")
        return 0.0 if value is None else float(value)

    @PyQt6.QtCore.pyqtProperty(bool, notify=radarConfigChanged)
    def radarLinearScale(self):
        return self.appconfig.get("radar_scale") == "linear"

    @PyQt6.QtCore.pyqtProperty(float, notify=radarConfigChanged)
    def radarMetersPerSample(self):
        # Equivalent monostatic range: propagation traverses the path twice.
        return espargos.constants.SPEED_OF_LIGHT / (2 * self._spectrum_sample_rate_hz)

    @PyQt6.QtCore.pyqtSlot(float, bool, result=bool)
    def setRadarRangeMeters(self, meters, upper):
        """Snap a physical range cutoff to the nearest valid RX sample."""
        zero = self.radarZeroSample
        if not np.isfinite(meters) or not np.isfinite(zero):
            return False
        sample = int(np.floor(meters / self.radarMetersPerSample + zero + 0.5))
        lower_sample, upper_sample = self._radar_delay_bounds()
        if upper:
            sample = max(lower_sample + 1, min(CHUNK_SAMPLES, sample))
        else:
            sample = max(0, min(upper_sample - 1, sample))
        self.appconfig.set({"delay_max" if upper else "delay_min": sample})
        self.radarConfigChanged.emit()
        return True

    @PyQt6.QtCore.pyqtProperty(float, notify=radarConfigChanged)
    def radarZeroSample(self):
        timing = self._radar_timing
        if timing is None or not self._tx_calibration_valid or (self.iqcontrol is not None and self.iqcontrol.syncNeeded):
            return float("nan")
        # TX probes bypass TX delay correction. Include the correction applied
        # to the restored waveform, then average circularly across replay wrap.
        corrections = {c.state.antenna: c.delay_s for g in timing.calibration.select(direction="tx").groups for c in g.corrections}
        delays = [delay + corrections[a] for a, delay in timing.delays_s.items()]
        period = CHUNK_SAMPLES / 80e6
        phase = np.mean(np.exp(2j * np.pi * np.asarray(delays) / period))
        return float((np.angle(phase) % (2 * np.pi)) * period / (2 * np.pi) * self._spectrum_sample_rate_hz)

    @PyQt6.QtCore.pyqtSlot(float, result=str)
    def radarColorbarLabel(self, fraction):
        lower, upper = self.radarMinDb, self.radarMaxDb
        if self.radarLinearScale:
            value = 10 ** (lower / 10) + fraction * (10 ** (upper / 10) - 10 ** (lower / 10))
            return f"{value:.2g}"
        return f"{lower + fraction * (upper - lower):.0f}"

        # Ensure this property setup section explicitly matches the demo footprint

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def angleSize(self):
        # Number of angular bins in the heatmap texture.
        return getattr(self, "_radar_angle_size", 256)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def delaySize(self):
        # Number of delay bins in the heatmap texture.
        return getattr(self, "_radar_delay_size", 256)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def delayMin(self):
        return self._radar_delay_bounds()[0]

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def delayMax(self):
        return self._radar_delay_bounds()[1]

    @PyQt6.QtCore.pyqtProperty(int, notify=boardSelectionChanged)
    def activeBoard(self):
        return self.active_board

    @PyQt6.QtCore.pyqtSlot(int)
    def set_active_board(self, index):
        """Select the board used by every receive view, including angle/radar."""
        if index == self.active_board or not hasattr(self, "pool") or not (0 <= index < len(self.receiver.boards)):
            return
        self.active_board = index
        self.boardSelectionChanged.emit()
        self._angle_estimate_deg = float("nan")
        self._elevation_estimate_deg = float("nan")
        self._angle_spectrum.fill(0)
        self._angle_status = "Waiting for selected board IQ."
        self.angleChanged.emit()
        if self.remove_los:
            self._reset_clutter()
        self.block_acc = {}
        self.last_display_block = -1
        self.block_time = {}
        for p in self.providers:
            p.clear()
        if self.display_mode in ("time", "ping", "constellation", "spectrum", "angle", "radar"):
            self._publish_sample_windows({})

        self.waterfallFrameReady.emit()

    # ---- rendering ----

    def _placeholder_row(self):
        return np.full((self.display_width, 4), (40, 40, 46, 255), dtype=np.uint8)

    def _display_spectrum(self, x):
        """Windowed, fftshifted FFT; x must be fft_size samples long."""
        return np.fft.fftshift(np.fft.fft(x * self._win))

    def _power_row(self, spec):
        db = 20 * np.log10(np.maximum(np.abs(spec) / self._fft_scale, 1e-12))
        return power_db_row(db, self.display_width, DB_MIN, DB_MAX)

    def _phase_row(self, rel):
        pooled = pool_complex_to_width(rel, self.display_width)
        return (PHASE_MAPPER.to_rgba(np.angle(pooled)[np.newaxis, :]) * 255).astype(np.uint8)[0]

    def start_render(self):
        if self.render_running:
            return
        self.render_running = True
        threading.Thread(target=self._render, daemon=True).start()

    def _publish_sample_windows(self, traces):
        """Publish coherent display windows for the GUI-thread Qt Charts."""

        if not traces:
            if self.display_mode == "radar":
                self._radar_status = "Waiting for IQ samples."
                self.radarStatusChanged.emit()
                empty = np.zeros((self.delaySize, self.angleSize, 4), dtype=np.uint8)
                self.radarMapChanged.emit(empty.reshape(-1).tolist())
            return

        # Clutter calibration/subtraction is defined in raw IQ, before
        # matched filtering. Restrict it to the requested radar-related modes.
        if self.display_mode in ("ping", "angle", "radar") and self.remove_los:
            traces = self._cancel_line_of_sight(traces)

        # Compress the chirps when a transmit reference is available.
        if self._tx_waveform is not None and self.display_mode in ("ping", "angle", "radar") and traces:
            # Coherently compress incoming chirps into sharp range profile peaks
            matched_traces = {antid: self._angle_matched_trace(trace) for antid, trace in traces.items()}
            common_length = min((t.size for t in matched_traces.values()), default=0)

            if common_length > 0:
                matched_traces = {antid: t[:common_length] for antid, t in matched_traces.items()}

                # Route appropriate domains based on active mode context
                if self.display_mode in ("angle", "radar"):
                    # Angle and Radar modes use pulse-compressed residual IQ.
                    traces = matched_traces
                elif self.display_mode == "ping":
                    # Keep raw, environment-subtracted voltage traces for the
                    # chart; its correlation overlay uses the same residual.
                    pass

        if self.display_mode not in ("angle", "radar") and self.iqcontrol is not None:
            calibrated = {}
            for antid, values in traces.items():
                values = np.asarray(values, dtype=np.complex64)
                correction = self.iqcontrol.display_correction(self.active_board, antid, values.size)
                calibrated[antid] = values if correction is None else np.fft.ifft(np.fft.ifftshift(np.fft.fftshift(np.fft.fft(values)) * correction)).astype(np.complex64)
            traces = calibrated

        # Publish chart samples under the shared lock.
        with self._sample_window_lock:
            self._sample_window_present = [antid in traces for antid in range(SENSOR_COUNT)]
            self._sample_windows = [np.asarray(traces.get(antid, np.zeros(self.fft_size, dtype=np.complex64))).copy() for antid in range(SENSOR_COUNT)]
            self._sample_window_generation += 1

        # Update the spatial display from the same samples.
        if self.display_mode == "angle" and traces:
            try:
                self._update_angle_estimate(traces)
            except Exception as error:
                self.logger.exception("angle estimation failed: %s", error)
                self._angle_estimate_deg = float("nan")
                self._elevation_estimate_deg = float("nan")
                self._angle_status = f"Angle estimation skipped: {error}"
                self._angle_spectrum.fill(0)
                self.angleChanged.emit()

        # Missing channels are zero-filled by the calibration helper.
        if self.display_mode == "radar":
            now = time.monotonic()
            if now - self._radar_last_render_time >= 0.1:
                self._radar_last_render_time = now
                self._render_radar_heatmap(traces)

    @PyQt6.QtCore.pyqtSlot(int, PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QScatterSeries, result="QVariantMap")
    def updateTimeChart(self, antid, i_series, q_series, correlation_series, peak_series):
        """Replace one chart's I/Q and matched-filter series."""
        if not (0 <= antid < SENSOR_COUNT):
            return {}
        with self._sample_window_lock:
            generation = self._sample_window_generation
            if self._time_trace_consumed[antid] == generation:
                return {}
            iq = self._sample_windows[antid].copy()
            self._time_trace_consumed[antid] = generation
            all_traces = {sensor_id: samples.copy() for sensor_id, samples in enumerate(self._sample_windows)}

        i_series.replace([PyQt6.QtCore.QPointF(x, float(y)) for x, y in enumerate(iq.real)])
        q_series.replace([PyQt6.QtCore.QPointF(x, float(y)) for x, y in enumerate(iq.imag)])
        correlation_limit = 1.0
        if self.display_mode == "ping" and getattr(self, "_tx_waveform", None) is not None:
            complex_correlation, correlation, peaks = self._matched_filter(iq)
            correlation_series.replace([PyQt6.QtCore.QPointF(x, float(y)) for x, y in enumerate(correlation)])
            peak_series.replace([PyQt6.QtCore.QPointF(index, float(correlation[index])) for index in peaks])
            correlation_limit = max(float(np.max(np.abs(correlation))), 1.0) if correlation.size else 1.0
        else:
            correlation_series.replace([])
            peak_series.replace([])
        magnitude = np.abs(iq)
        rms = float(np.sqrt(np.mean(np.square(magnitude), dtype=np.float64)))
        peak = float(np.max(magnitude)) if magnitude.size else 0.0

        def dbfs(value):
            return float(20 * np.log10(value / ADC_FULL_SCALE)) if value > 0 else float("-inf")

        result = {"rmsDbfs": dbfs(rms), "peakDbfs": dbfs(peak)}
        if self.display_mode == "ping":
            result["pingIndices"] = ", ".join(str(index) for index in peaks) if getattr(self, "_tx_waveform", None) is not None else ""
            result["correlationLimit"] = correlation_limit
        return result

    @PyQt6.QtCore.pyqtSlot(int, PyQt6.QtCharts.QScatterSeries)
    def updateConstellationChart(self, antid, series):
        """Replace one sensor's constellation from the newest IQ window."""
        if not (0 <= antid < SENSOR_COUNT):
            return
        with self._sample_window_lock:
            generation = self._sample_window_generation
            if self._constellation_consumed[antid] == generation:
                return
            present = self._sample_window_present[antid]
            iq = self._sample_windows[antid].copy()
            self._constellation_consumed[antid] = generation
        if not present:
            series.replace([])
            return
        series.replace([PyQt6.QtCore.QPointF(float(value.real), float(value.imag)) for value in iq])

    @PyQt6.QtCore.pyqtSlot(int, PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QValueAxis)
    def updateSpectrumChart(self, antid, series, frequency_axis):
        """Replace one sensor's two-sided, windowed amplitude spectrum."""
        if not (0 <= antid < SENSOR_COUNT):
            return
        with self._sample_window_lock:
            generation = self._sample_window_generation
            if self._spectrum_consumed[antid] == generation:
                return
            present = self._sample_window_present[antid]
            iq = self._sample_windows[antid].copy()
            sample_rate_hz = self._spectrum_sample_rate_hz
            center_hz = self._spectrum_center_hz
            self._spectrum_consumed[antid] = generation
        frequency_axis.setRange((center_hz - sample_rate_hz / 2) / 1e6, (center_hz + sample_rate_hz / 2) / 1e6)
        if not present:
            series.replace([])
            return
        spectrum = self._display_spectrum(iq)
        amplitude_dbfs = 20 * np.log10(np.maximum(np.abs(spectrum) / self._fft_scale, 1e-12))
        frequencies_mhz = (center_hz + np.fft.fftshift(np.fft.fftfreq(len(iq), d=1 / sample_rate_hz))) / 1e6
        series.replace([PyQt6.QtCore.QPointF(float(frequency), float(amplitude)) for frequency, amplitude in zip(frequencies_mhz, amplitude_dbfs)])

    @PyQt6.QtCore.pyqtSlot(PyQt6.QtCharts.QLineSeries)
    def updateAngleChart(self, series):
        """Publish the current spatial FFT/MUSIC angle spectrum to QML."""
        if self.display_mode != "angle":
            return
        spectrum = np.asarray(self._angle_spectrum, dtype=np.float32).reshape(-1)
        if spectrum.size == 0 or not np.all(np.isfinite(spectrum)):
            series.replace([])
            return
        points = [PyQt6.QtCore.QPointF(-90.0 + index * 180.0 / max(1, spectrum.size - 1), float(value)) for index, value in enumerate(spectrum)]
        series.replace(points)

    def _refresh_spectrum_config(self):
        """Track RF center/sample rate without blocking the GUI thread."""
        now = time.monotonic()
        if now - self._spectrum_config_read_at < 1.0:
            return
        self._spectrum_config_read_at = now
        try:
            cfg = self.receiver.boards[self.active_board].iq.get_config()
            sample_rate_hz = DECIM_TO_FS.get(int(cfg.get("adc_decimation", 1)), DECIM_TO_FS[1])
            receivers = cfg.get("receivers", [])
            center_hz = float(receivers[0].get("rf_freq_hz", 2437e6)) if receivers else 2437e6
            with self._sample_window_lock:
                rate_changed = self._spectrum_sample_rate_hz != sample_rate_hz
                self._spectrum_sample_rate_hz = sample_rate_hz
                self._spectrum_center_hz = center_hz
            if rate_changed:
                self.radarConfigChanged.emit()
        except Exception as e:
            print(f"spectrum config refresh error: {e}", file=sys.stderr)

    def _on_iq_cluster(self, cluster):
        """Pool cluster callback (runs on the pool's processing thread; keep
        it light). Complete clusters arrive immediately, settled partial ones
        after the settle timeout — the block assembler reorders by index."""
        if self._tx_busy or self.receiver.calibrating or (self.iqcontrol is not None and self.iqcontrol.referenceBusy):
            return
        with self._pending_lock:
            self._pending_clusters.append((time.monotonic(), cluster))
            if len(self._pending_clusters) > 2048:
                del self._pending_clusters[:1024]

    def _drain_sets(self):
        """Drain raw or accumulated clusters into 256-sample display sets.

        An accumulated vector is split back into its ordered vector sections,
        using ``chunk_index + lane`` on the unwrapped display grid.
        """
        with self._pending_lock:
            cutoff = time.monotonic() - DISPLAY_SETTLE_SECONDS
            count = 0
            while count < len(self._pending_clusters) and self._pending_clusters[count][0] <= cutoff:
                count += 1
            clusters = self._pending_clusters[:count]
            del self._pending_clusters[:count]

        # MULTI-BOARD GRID: this is the intentional board-selection boundary.
        # For a measured joint aperture, retain all boards here with unique
        # (board, antenna) keys, then supply positions in _angle_geometry.
        board = self.active_board
        if not clusters or board >= len(self.receiver.boards):
            return []
        revision = self.receiver.boards[board].revision
        positions = [revision.antenna_id_to_row_col(antenna_id) for antenna_id in range(SENSOR_COUNT)]
        sets = []

        for arrived, cluster in clusters:
            if not np.any(cluster.completion[board]):
                # Controller generations are local to each board. A neighbor's
                # accumulation window must not reset this board's display.
                continue
            if isinstance(cluster, IQAccumCluster):
                epoch = ("accum", tuple(np.unique(cluster.config_generation[board][cluster.completion[board]])))
            else:
                present = np.zeros_like(cluster.completion)
                present[board] = cluster.completion[board]
                epoch = ("raw", int(cluster.sync_info[present][0]) & 0xFFFF, int(cluster.sample_rate_hz[present][0]), bool(cluster.flags[present][0] & 256))
            if epoch != self._display_epoch:
                if arrived < self._display_epoch_time:
                    continue
                self._display_epoch = epoch
                if getattr(self, "_remove_los", False):
                    self._reset_clutter()
                self.block_acc = {}
                self.block_time = {}
                self.last_display_block = -1
                sets.clear()
            self._display_epoch_time = arrived
            if isinstance(cluster, IQAccumCluster):
                rate, center = cluster.sample_rate_hz, cluster.center_freq_hz
            else:
                rate = cluster.sample_rate_hz[present][0]
                center = cluster.center_freq_hz[present][0]
            with self._sample_window_lock:
                rate_changed = self._spectrum_sample_rate_hz != float(rate)
                self._spectrum_sample_rate_hz = float(rate)
                self._spectrum_center_hz = float(center)
            if rate_changed:
                self.radarConfigChanged.emit()

            if isinstance(cluster, IQAccumCluster):
                completion = cluster.section_completion
                sections = cluster.iq_sections
                for lane in range(cluster.vector_chunks):
                    per = {antid: sections[board, row, col, lane] for antid, (row, col) in enumerate(positions) if completion[board, row, col, lane] and antid in self._rx_sensor_ids()}
                    if per:
                        sets.append((cluster.chunk_index + lane, per))
            else:
                completion = cluster.completion
                iq = cluster.iq
                per = {antid: iq[board, row, col] for antid, (row, col) in enumerate(positions) if completion[board, row, col] and antid in self._rx_sensor_ids()}
                if per:
                    sets.append((cluster.chunk_index, per))

        sets.sort(key=lambda entry: entry[0])

        return sets

    def _render(self):
        # Include processing time in the 25-Hz frame budget. Sleeping a full
        # interval after rendering made the scroll speed depend on FFT/copy
        # cost. QML refreshes only when this worker publishes a complete frame.
        next_frame = time.monotonic()
        while self.render_running:
            time.sleep(max(0.0, next_frame - time.monotonic()))
            if not self.render_running:
                break
            next_frame = time.monotonic() + 1 / 25
            if self._tx_busy or (self.iqcontrol is not None and self.iqcontrol.referenceBusy):
                continue
            try:
                sets = self._drain_sets()
                if not sets and not self.block_acc:
                    continue
                if sets and not self._first_sets_logged:
                    self._first_sets_logged = True
                    self.logger.info("first synchronized IQ chunk sets received, display running")
                mode = "time" if self.display_mode == "ping" else self.display_mode
                if mode in ("spectrum", "angle"):
                    self._refresh_spectrum_config()
                # Every display mode assembles consecutive chunks into FFT
                # blocks, allowing partial clusters to arrive out of order.
                self._render_block_rows(sets, mode)
            except Exception as e:
                print(f"render error: {e}", file=sys.stderr)

    def _render_block_rows(self, sets, mode):
        """Assemble fft_size/256 CONSECUTIVE chunks per sensor into one
        display unit, a waterfall row or one time-domain trace window (blocks
        aligned to the synchronized index grid, so rows and traces line up in
        time across sensors; a sensor that did not deliver a block gets a
        placeholder row).

        Clusters have already had a short settling window. A small additional
        window joins adjacent chunks crossing a render tick. Retire blocks in
        index order, once only; late data cannot create duplicate grey rows.
        """
        B = self.fft_size // CHUNK_SAMPLES
        config = self._display_config
        trigger = config.get("trigger", {})
        schedule = tuple(trigger[k] for k in ("interval_chunks", "offset_chunks", "burst_chunks")) if trigger.get("mode") == "interval" else None
        if schedule != self._display_schedule:
            self._display_schedule = schedule
            self.block_acc.clear()
            self.block_time.clear()
            self.last_display_block = -1
        now = time.monotonic()
        if self._clutter_calibrating:
            # Learn from untouched synchronized IQ chunks, independent of
            # which visualization mode currently owns the render path.
            for _, per_antenna in sets:
                self._collect_clutter_snapshot(per_antenna)
        for idx, per in sets:
            b = idx // B
            if b <= self.last_display_block:
                continue
            block = self.block_acc.setdefault(b, {})
            if getattr(self, "_remove_los", False) and self._clutter_mode == "automatic":
                first_background = not self._automatic_clutter.history
                with self._clutter_lock:
                    residual = self._automatic_clutter.process(idx, per, now)
                    self._clutter_status_text = "Automatic clutter active · 16 chunks / 0.4 s"
                if first_background:
                    self.clutterStatusChanged.emit()
                if mode in ("time", "angle", "radar") and self.display_mode in ("ping", "angle", "radar"):
                    per = residual
            for antid, iq in per.items():
                block.setdefault(antid, {})[idx % B] = iq
            self.block_time.setdefault(b, now)
        ready = []
        for b in sorted(self.block_acc):
            if now - self.block_time[b] < DISPLAY_SETTLE_SECONDS:
                break
            ready.append(b)
        if ready and schedule and len(schedule) == 3 and self.last_display_block >= 0:
            period, phase, length = schedule
            if period > 0 and length > 0:
                # Infer only scheduled Interval blocks, never the intentional
                # idle space between bursts. Bound work after a long outage.
                expanded = []
                previous = self.last_display_block
                for b in ready:
                    n = max(0, ((previous + 1) * B - phase - length) // period + 1)
                    n = max(n, (b * B - phase) // period - WATERFALL_HEIGHT)
                    missing = max(previous + 1, (phase + n * period) // B)
                    while missing < b:
                        self.block_acc.setdefault(missing, {})
                        expanded.append(missing)
                        if (missing + 1) * B >= phase + n * period + length:
                            n += 1
                        missing = max(missing + 1, (phase + n * period) // B)
                    expanded.append(b)
                    previous = b
                ready = expanded
        if ready:
            self.last_display_block = ready[-1]
        # Older rows would scroll off the image in this same frame. Retire
        # them without allocating FFT/color buffers for an invisible backlog.
        for b in ready[:-WATERFALL_HEIGHT]:
            self.block_acc.pop(b, None)
            self.block_time.pop(b, None)
        ready = ready[-WATERFALL_HEIGHT:]
        if mode in ("time", "constellation", "spectrum", "angle", "radar"):
            self._render_sample_blocks(ready, B, mode)
            return
        # Batch FFT/color conversion: hundreds of tiny NumPy calls per
        # frame otherwise contend with the acquisition thread at 80 MSa/s.
        windows, masks = [], []
        for b in ready:
            block = self.block_acc.pop(b, {})
            self.block_time.pop(b, None)
            present = [len(block.get(a, {})) == B for a in range(SENSOR_COUNT)]
            if self.complete_only and not all(present[a] for a in self._rx_sensor_ids()):
                continue
            window = np.zeros((SENSOR_COUNT, self.fft_size), dtype=np.complex64)
            for a in range(SENSOR_COUNT):
                if present[a]:
                    window[a] = np.concatenate([block[a][k] for k in range(B)])
            windows.append(window)
            masks.append(present)
        if windows:
            spectra = np.fft.fftshift(np.fft.fft(np.asarray(windows) * self._win, axis=-1), axes=-1)
            masks = np.asarray(masks)
            if mode == "phase":
                reference_id = next(iter(self._rx_sensor_ids()), 0)
                if self.iqcontrol is not None:
                    for a in range(SENSOR_COUNT):
                        corr = self.iqcontrol.display_correction(self.active_board, a, self.fft_size)
                        if corr is not None:
                            spectra[:, a] *= corr
                spectra *= np.conj(spectra[:, reference_id : reference_id + 1]).copy()
                pooled = pool_complex_to_width(spectra, self.display_width)
                pixels = (PHASE_MAPPER.cmap(PHASE_MAPPER.norm(np.angle(pooled))) * 255).astype(np.uint8)
                masks &= masks[:, reference_id : reference_id + 1].copy()
            else:
                db = 20 * np.log10(np.maximum(np.abs(spectra) / self._fft_scale, 1e-12))
                pixels = power_db_row(db, self.display_width, DB_MIN, DB_MAX)
            pixels[~masks] = self._placeholder_row()
            for a in range(SENSOR_COUNT):
                self.providers[a].add_rows(pixels[:, a])
                self.rows_rendered[a] += len(windows)
            self.waterfallFrameReady.emit()

    def _render_sample_blocks(self, ready, B, mode):
        """Chart display at fft_size above one chunk: all sensors show the SAME
        newest ready block of fft_size samples (the multi-chunk analogue of
        _render_sample_windows, same complete_only semantics: with it,
        only blocks where ALL sensors delivered every chunk are shown; without
        it, the newest block with at least one full per-sensor trace is shown
        and missing sensors are drawn as an empty grid)."""
        chosen = None
        for b in reversed(ready):
            block = self.block_acc.get(b)
            if block is None:
                continue
            if mode == "radar":
                # Keep one common timestamp, even when only part of the array
                # arrived. Missing antennas contribute zero to the heatmap.
                slots = set().union(*(set(samples) for samples in block.values()))
                if not slots:
                    continue
                slot = max(slots)
                chosen = {antid: samples[slot] for antid, samples in block.items() if slot in samples}
                break
            if mode == "angle":
                # Angle processing must see one synchronized chunk.
                if not self._rx_sensor_ids() or not set(self._rx_sensor_ids()).issubset(block):
                    continue
                common_slots = set.intersection(*(set(block[antid].keys()) for antid in self._rx_sensor_ids()))
                if not common_slots:
                    continue
                slot = max(common_slots)
                chosen = {antid: block[antid][slot] for antid in self._rx_sensor_ids()}
                break

            traces = {antid: np.concatenate([slots[k] for k in range(B)]) for antid, slots in block.items() if len(slots) == B}
            if len(traces) >= (len(self._rx_sensor_ids()) if self.complete_only else 1):
                chosen = traces
                break
        for b in ready:
            self.block_acc.pop(b, None)
            self.block_time.pop(b, None)
        if chosen is None:
            return
        self._publish_sample_windows(chosen)
        for antid in range(SENSOR_COUNT):
            self.rows_rendered[antid] += 1

    # ---- application lifecycle ----

    def _update_display_config(self, config):
        self._display_config = {**self._display_config, **config}

    def _synchronize_radar(self):
        """The central button calibrates the requested roles, even with TX stopped."""
        settings = self._tx_settings.copy()
        resume = self.txActive
        stage = "validating requested setup"
        self._radar_timing = None
        try:
            cfg = {key: settings[key] for key in self.PING_TX_DEFAULTS}
            waveform = self._build_ping_waveform(cfg)
            candidate = RadarTransmitter(self.pool, SimpleNamespace(**settings))
            self._stop_ping_tx()
            self.radar_tx = candidate
            for attempt in range(3):
                stage = "applying TX/RX roles and RF settings"
                candidate.configure()
                if attempt == 0:
                    # Sync measures the whole demo pool afresh. Retire both
                    # directions: a previous coherent RX group may include
                    # sensors that now transmit (or the reverse).
                    with self.pool._lock:
                        self.pool.calibrations = self.pool.calibrations.without(self.pool.antennas)
                        self.pool.iq._calibration = None
                stage = "preparing the user waveform"
                reference = candidate.prepare(waveform, cfg["tx_gain_db"]) if candidate.antennas else None
                if candidate.antennas:
                    stage = "preparing silent RX pilot helpers"
                    candidate.prepare_calibration_helpers()
                try:
                    stage = "measuring and validating TX/RX calibration"
                    self.pool.calibrate(tx_method=settings["tx_calibration"], path=settings["calibration_path"], per_board=False)
                    break
                except (DisconnectedLinks, CalibrationMeasurementError):
                    if attempt == 2:
                        raise
                    # Transient interference can invalidate both halves of a
                    # reciprocal link. Acquire a fresh, independently checked
                    # fit rather than publishing incomplete corrections.
                    self.logger.info("Retrying calibration with fresh independent pilot captures")
            if reference is not None:
                stage = "measuring zero-range TX/RX delay through " + settings["timing_path"]
                self._radar_timing = calibrate_radar_timing(self.pool, candidate.antennas, settings["timing_path"], self.logger)
            resume = resume and not getattr(self, "_shutting_down", False)
            if resume and reference is not None:
                candidate.session.mute(False)
            self._calibrated_tx_setup = self._tx_setup(settings)
            self._tx_calibration_valid = True
            if reference is not None:
                self.logger.info("TX/RX array and zero-range timing calibration complete")
            self.radarCalibrationDone.emit((reference, cfg, resume, None))
        except Exception as error:
            self._radar_timing = None
            error.add_note(f"Radar calibration stage: {stage}; selected TX={settings['tx_sensors']}; " f"RF={settings['frequency_mhz']} MHz; method={settings['tx_calibration']}; " f"array path={settings['calibration_path']}; timing path={settings.get('timing_path')}")
            self._calibrated_tx_setup = None
            self._tx_calibration_valid = False
            try:
                self._stop_ping_tx()
            except Exception as cleanup:
                error.add_note(f"Could not confirm TX stop: {cleanup}")
            self.radarCalibrationDone.emit((None, None, False, str(error)))
            raise

    def _finish_radar_calibration(self, outcome):
        reference, cfg, resume, error = outcome
        if resume and reference is not None:
            self._tx_waveform, self._active_ping_config = reference, cfg
            self.appconfig.set(cfg)
        self._tx_status = (
            f"Calibration failed: {error}"
            if error
            else (
                "TX/RX calibrated. Start TX whenever ready."
                if reference is not None and not resume
                else "ESPARGOS TX running · TX/RX phase and timing calibrated." if resume and reference is not None else "RX calibrated. Select TX sensors and use Sync + Calibration to calibrate transmission."
            )
        )
        self.txStateChanged.emit()
        self.radarConfigChanged.emit()

    def _start_iq_gui(self):
        """After the framework pool is up: expose the pool's QML adapter,
        subscribe the display to delivered clusters, and synchronize/calibrate IQ."""
        hosts = ", ".join(b.host for b in self.receiver.boards)
        self.logger.info(f"IQ capture starting ({len(self.receiver.boards)} board(s): {hosts})")
        self.iqcontrol = self.create_iq_controller(sync_operation=self._synchronize_radar, config_operation=lambda config: configure_receivers(self.pool, config))
        self.iqcontrol.syncNeededChanged.connect(self.radarConfigChanged.emit)
        self.iqcontrol.calibration_guard = lambda: (self._tx_calibration_valid and self._tx_setup(self._tx_settings) == self._calibrated_tx_setup)
        self.iqcontrol.calibrationChanged.connect(self._on_iq_calibration_changed)
        self.iqcontrol.referenceBusyChanged.connect(self.txStateChanged.emit)
        self.iqcontrol.configChanged.connect(self._update_display_config)
        self.iqcontrol.set_apply_calibration(bool(self.appconfig.get("apply_calibration")))
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)
        # Observe each cluster from its first arrival; the display has its own
        # bounded settling window, independent of calibration/library consumers.
        self.receiver.add_callback(self._on_iq_cluster, callback_predicate=lambda cluster: True)
        self.receiver.add_accumulation_callback(self._on_iq_cluster, callback_predicate=lambda cluster: True)

        self._tx_settings["tx_sensors"] = [f"{self.pool.names[b]}.{a}" for b, a in sorted(self.radar_tx.selected)]
        self.txStateChanged.emit()
        self.iqcontrol.sync_async()
        self.start_render()

    def _abort_if_stuck(self):
        """Abort startup if the pool/UI never becomes ready.

        This prevents a silent hang when the board never responds, when QML fails
        to load, or when the app gets stuck in the init path with no way for the
        user to interrupt it from the terminal.
        """
        if getattr(self, "ready", False):
            return
        print("Startup timed out; aborting before the process can hang indefinitely.", file=sys.stderr)
        self.render_running = False
        self._stop_ping_tx()
        if hasattr(self, "engine") and not self.engine.rootObjects():
            print("QML UI did not load; aborting execution.", file=sys.stderr)
        self.quit()

    def onAboutToQuit(self):
        self._shutting_down = True
        self.render_running = False
        if self._tx_worker is not None:
            self._tx_worker.join()
        try:
            self._stop_ping_tx()
        finally:
            super().onAboutToQuit()  # ESPARGOSIQApplication restores WiFi mode

    # ---- entry point ----

    def exec(self):
        self.initComplete.connect(self._start_iq_gui)
        self.initComplete.connect(lambda: self._startup_abort_timer.stop())

        # If the pool or the UI never becomes ready, abort instead of waiting forever.
        self._startup_abort_timer.start(int(self._startup_abort_seconds * 1000))

        self.initialize_pool(calibrate=False)
        # iqcontrol is created after pool init; QML needs the name defined from
        # the start (bindings re-evaluate when the real adapter replaces None)
        qml_path = pathlib.Path(__file__).resolve().parent / "pulse-radar-analyzer-ui-espargi.qml"
        if not qml_path.exists():
            print(f"QML UI not found: {qml_path}", file=sys.stderr)
            self._abort_if_stuck()
            return -1

        self.initialize_qml(qml_path, context_props={"iqcontrol": None})
        if not self.engine.rootObjects():
            print("QML UI failed to load; aborting execution.", file=sys.stderr)
            self._abort_if_stuck()
            return -1
        return super().exec()


if __name__ == "__main__":
    app = EspargosPulseRadarAnalyzer(sys.argv)
    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        print("Interrupted by user; shutting down cleanly.", file=sys.stderr)
        app.render_running = False
        app._stop_ping_tx()
        app.quit()
        sys.exit(130)
