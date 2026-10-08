#!/usr/bin/env python
"""ESPARGOS demo: live IQ signal analyzer.

Puts the array into IQ sampling mode and renders, per sensor, one of three live
displays, built from INDEX-MATCHED chunk sets (the array-wide time sync makes
equal source_chunk_index the same instant, so rows/traces line up in time
across all sensors):

  - power waterfall: FFT rows on a fixed -90..0 dBFS scale (viridis)
  - relative-phase waterfall vs sensor 0 (twilight), with the fine per-sensor
    time/phase calibration applied when the Apply-calibration toggle is on
  - time-domain I/Q trace (all sensors show the same instant)
  - I/Q constellation (the same synchronized sample window per sensor)
  - amplitude spectrum over absolute RF frequency

Capture control (frequency, sample rate, analog filter bandwidth, RF switch,
gain, triggers), array time sync and phase calibration come from the reusable
espargos_iqsampling addon interfaces: IQPool assembles the chunk streams into
synchronized clusters and manages the IQ lifecycle, IQController adapts it to
the drawer UI (IQSettings.qml).

With several comma-separated hosts (a coherent multi-board setup, e.g. the
Aperture Kit) a tab bar selects which board's sensors are displayed — every
cluster spans all boards, so the tab switch just changes which slice is
rendered; config, sync (array-wide, REFTX-packet-anchored) and calibration
(global, via the master's reference tone) always apply to the whole array.

Usage::

    python3 addons/iqsampling/demos/signal-analyzer/ping_analyzer.py \
        -c config/single-espargos-one.yml

The combined-array YAML supplies the board host and the physical 2D antenna
layout. ``-s <host>`` remains available for a generated single-board layout.
(headless acceptance tests live in the addon's tests/ directory)
"""

import argparse
import iio
import adi
import pathlib
import sys
import threading
import time
import scipy

# pyespargos repository root (this demo lives four levels below the checkout)
repository_root = pathlib.Path(__file__).resolve().parents[4]
sys.path.insert(0, str(repository_root))

import numpy as np
import matplotlib.cm
import matplotlib.colors

import PyQt6.QtCore
import PyQt6.QtCharts

import espargos  # noqa: F401 -- importing espargos loads the addon packages onto sys.path
from demos.common import CombinedArrayMixin

from espargos import CHUNK_SAMPLES, DECIM_TO_FS, IQAccumCluster, SENSOR_COUNT
from demos.common.iq_application import ESPARGOSIQApplication
from demos.common.iq_waterfall import WaterfallImageProvider, power_db_row

WATERFALL_HEIGHT = 300
ADC_FULL_SCALE = 512.0
DB_MIN, DB_MAX = -90.0, 0.0
AMPLITUDE_TARGET = 2**14


PHASE_MAPPER = matplotlib.cm.ScalarMappable(norm=matplotlib.colors.Normalize(vmin=-np.pi, vmax=np.pi, clip=True), cmap="twilight")


def pool_complex_to_width(z, w):
    """Reduce/expand a complex spectrum to w pixels by coherent averaging."""
    n = len(z)
    if w == n:
        return z
    if w < n:
        edges = (np.arange(w) * n) // w
        return np.add.reduceat(z, edges)
    return z[(np.arange(w) * n) // w]


class EspargosSignalAnalyzer(CombinedArrayMixin, ESPARGOSIQApplication):
    displayModeChanged = PyQt6.QtCore.pyqtSignal()
    traceSampleCountChanged = PyQt6.QtCore.pyqtSignal()
    angleChanged = PyQt6.QtCore.pyqtSignal()
    txStateChanged = PyQt6.QtCore.pyqtSignal()
    radarMapChanged = PyQt6.QtCore.pyqtSignal(list) # Add this signal for the heatmap vector
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
    "tx_gain_db": 0,
    "remove_los": False,
}

    # --- ping TX config ---
    PING_TX_DEFAULTS = {
    "chirp_frames": 16,
    "bandwidth_hz": 40e6,
    "pings_per_256": 2,
    "tx_gain_db": 0,
}

    def __init__(self, argv):
        parser = argparse.ArgumentParser(description="ESPARGOS Demo: live IQ signal analyzer", add_help=False)
        super().__init__(argv, argparse_parent=parser)

        if not isinstance(self, CombinedArrayMixin):
            raise RuntimeError("CombinedArrayMixin is not active; check the pyespargos repository import path")

        self.iqcontrol = None  # IQController QML adapter, created once the pool is up
        self.active_board = 0
        # clusters delivered by the pool's processing worker, drained by the
        # render thread (bounded so a stalled render cannot grow it unboundedly)
        self._pending_lock = threading.Lock()
        self._pending_clusters = []
        self.display_mode = self.get_initial_config("app", "display_mode")
        self.fft_size = int(self.get_initial_config("app", "fft_size"))
        self.complete_only = bool(self.get_initial_config("app", "complete_only"))
        self.display_width = CHUNK_SAMPLES
        self._win = np.hanning(self.fft_size).astype(np.float32)
        self._fft_scale = max(float(np.sum(self._win)) * ADC_FULL_SCALE, 1.0)
        self.block_acc = {}  # block_id -> {antid: {slot: iq}}
        self.block_time = {}  # block_id -> last-update wall time (stale eviction)
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
        self._angle_status = "Run IQ calibration, then start Pluto TX."
        self._radar_status = "Run IQ calibration, then start Pluto TX."
        self._radar_angle_size = 256
        self._radar_delay_size = 256
        self._radar_status = "Run IQ calibration, then start Pluto TX."

        self.providers = [WaterfallImageProvider(CHUNK_SAMPLES, WATERFALL_HEIGHT) for _ in range(SENSOR_COUNT)]
        for antid in range(SENSOR_COUNT):
            self.engine.addImageProvider(f"display{antid}", self.providers[antid])

        self.appConfigChanged.connect(self._on_app_config_changed)

    # ---- configuration ----

    def _on_app_config_changed(self, newcfg):
        if "display_mode" in newcfg:
            self._set_display_mode(str(newcfg["display_mode"]))
        if "fft_size" in newcfg:
            self._set_fft_size(int(newcfg["fft_size"]))
        if "complete_only" in newcfg:
            self.complete_only = bool(newcfg["complete_only"])
        if "remove_los" in newcfg:
            self.remove_los = bool(newcfg["remove_los"])
        if "apply_calibration" in newcfg:
            if self.iqcontrol is not None:
                self.iqcontrol.set_apply_calibration(bool(newcfg["apply_calibration"]))

    def _on_iq_calibration_changed(self):
        if self._tx_waveform is None:
            self._angle_status = "IQ calibration complete. Start Pluto TX to estimate angle."
        else:
            self._angle_status = "IQ calibration complete. Angle estimate is updating."
        self.angleChanged.emit()

    def _set_display_mode(self, mode):
        if mode != self.display_mode:
            self.display_mode = mode
            for p in self.providers:
                p.clear()
            if mode in ("time", "ping", "constellation", "spectrum", "angle", "radar"):
                self._publish_sample_windows({})
            self.displayModeChanged.emit()

    @property
    def remove_los(self):
        return getattr(self, "_remove_los", bool(self.appconfig.get("remove_los", False)))

    @remove_los.setter
    def remove_los(self, enabled):
        self._remove_los = bool(enabled)

    def _angle_geometry(self):
        """Map configured combined-array cells to local antenna IDs."""
        row_count = max(0, int(getattr(self, "n_rows", 0)))
        column_count = max(0, int(getattr(self, "n_cols", 0)))
        geometry = np.full((row_count, column_count), -1, dtype=int)
        if not hasattr(self, "pool") or not self.pool.boards or not 0 <= self.active_board < len(self.pool.boards):
            return geometry
        if not hasattr(self, "indexing_matrix"):
            return geometry
        revision = self.pool.boards[self.active_board].revision
        local_id_by_position = {
            revision.antenna_id_to_row_col(antenna_id): antenna_id
            for antenna_id in range(SENSOR_COUNT)
        }
        for row in range(row_count):
            for column in range(column_count):
                if row >= self.indexing_matrix.shape[0] or column >= self.indexing_matrix.shape[1]:
                    continue
                flat_index = int(self.indexing_matrix[row, column])
                board_index, local_flat = divmod(flat_index, SENSOR_COUNT)
                if board_index == self.active_board:
                    local_position = divmod(local_flat, 4)
                    antenna_id = local_id_by_position.get(local_position, -1)
                    if 0 <= antenna_id < SENSOR_COUNT:
                        geometry[row, column] = antenna_id
        return geometry
    

    def _cancel_line_of_sight(self, raw_traces, common_power):
        """Independently isolates, aligns, and cancels EVERY periodic line-of-sight

        ping inside the data block by dynamically scaling the cancellation count
        to match the active trace length and FFT size.
        """
        if self._tx_waveform is None or common_power.size == 0 or not raw_traces:
            return raw_traces

        config = self._current_ping_config()
        pings_per_256 = max(1, min(config["pings_per_256"], CHUNK_SAMPLES))
        burst_length = CHUNK_SAMPLES // pings_per_256
        chirp_frames = max(1, min(config["chirp_frames"], burst_length))
        
        sample_trace = next(iter(raw_traces.values()))
        trace_len = sample_trace.size
        is_matched_domain = np.max(np.abs(sample_trace)) > (ADC_FULL_SCALE * 2.0)

        # 1. Adaptively scale the reference template to match the active domain
        if is_matched_domain:
            reference = np.asarray(self._tx_waveform[:chirp_frames], dtype=np.complex128)
            autocorr = np.convolve(reference, np.conj(reference[::-1]), mode="full")
            center = chirp_frames - 1
            half_width = chirp_frames // 2
            raw_ref = autocorr[center - half_width : center + half_width + 1]
        else:
            raw_ref = np.asarray(self._tx_waveform[:chirp_frames], dtype=np.complex128)

        # 2. Dynamically determine how many total pings fit into this trace payload
        # This fixes the bug by scaling automatically with 256, 512, or 1024 sample arrays.
        total_pings_in_trace = trace_len // burst_length
        if total_pings_in_trace <= 0:
            return raw_traces

        # 3. Fold the global power profile to find the true relative arrival index
        # We wrap across the specific total_pings_in_trace window size cleanly
        global_power = np.nan_to_num(np.asarray(common_power[:trace_len], dtype=np.float64), nan=0.0)
        folded_power = np.sum(global_power[:total_pings_in_trace * burst_length].reshape(total_pings_in_trace, burst_length), axis=0)
        relative_coarse_peak = int(np.argmax(folded_power))

        canceled_traces = {}

        # 4. Process each antenna channel independently
        for antid, trace in raw_traces.items():
            clean_trace = np.asarray(trace, dtype=np.complex128).copy()

            # Compute local matched profile for fine-tuning this antenna's peak
            ant_matched = self._angle_matched_trace(clean_trace) if not is_matched_domain else clean_trace
            ant_power = np.abs(ant_matched) ** 2

            # Fold this antenna's profile to track its specific spatial micro-shift delay
            ant_folded = np.sum(ant_power[:total_pings_in_trace * burst_length].reshape(total_pings_in_trace, burst_length), axis=0)
            relative_ant_peak = int(np.argmax(ant_folded))

            # 5. Loop through and cancel EVERY single ping present across the entire active payload
            for p in range(total_pings_in_trace):
                # Calculate the absolute sample index for this specific period's ping interval
                ant_peak_idx = p * burst_length + relative_ant_peak
                
                if ant_peak_idx >= clean_trace.size:
                    continue

                # Center our cancellation window directly over this current peak
                half_len = raw_ref.size // 2
                start_win = max(0, ant_peak_idx - half_len)
                end_win = min(clean_trace.size, start_win + raw_ref.size)
                start_win = max(0, end_win - raw_ref.size)

                observed = clean_trace[start_win:end_win]
                current_template = raw_ref[:observed.size]
                current_energy = np.vdot(current_template, current_template).real

                # Stable Least-Squares Projection per individual pulse interval
                if observed.size > 0 and current_energy > 1e-12:
                    coefficient = np.vdot(current_template, observed) / current_energy
                    max_allowed_gain = 5.0 if is_matched_domain else 500.0
                    
                    if np.abs(coefficient) < max_allowed_gain:
                        clean_trace[start_win:end_win] -= coefficient * current_template

            canceled_traces[antid] = clean_trace.astype(np.complex64)

        return canceled_traces

    
    def _publish_sample_windows(self, traces):
        """Publish coherent display windows for the GUI-thread Qt Charts."""
        
        # 1. Capture a clean snapshot of the raw traces BEFORE mutating them
        raw_traces_snapshot = {antid: samples.copy() for antid, samples in traces.items()} if traces else {}

        if self._tx_waveform is not None and self.display_mode in ("ping", "angle", "radar") and traces:
            # Coherently compress incoming chirps into sharp range profile peaks
            matched_traces = {antid: self._angle_matched_trace(trace) for antid, trace in traces.items()}
            common_length = min((t.size for t in matched_traces.values()), default=0)
            
            if common_length > 0:
                matched_traces = {antid: t[:common_length] for antid, t in matched_traces.items()}
                common_power = np.sum([np.abs(t) ** 2 for t in matched_traces.values()], axis=0)
                
                # Route appropriate domains based on active mode context
                if self.display_mode in ("angle", "radar"):
                    # Angle and Radar modes expect pulse-compressed matched-filter vectors
                    if self.remove_los:
                        traces = self._cancel_line_of_sight(matched_traces, common_power)
                    else:
                        traces = matched_traces
                elif self.display_mode == "ping":
                    # Ping Chart Mode expects raw voltage vectors unless suppressed
                    if self.remove_los:
                        traces = self._cancel_line_of_sight(traces, common_power)
                    else:
                        pass

        # 2. Lock and publish variables to the UI thread cache
        with self._sample_window_lock:
            self._sample_window_present = [antid in traces for antid in range(SENSOR_COUNT)]
            self._sample_windows = [np.asarray(traces.get(antid, np.zeros(self.fft_size, dtype=np.complex64))).copy() for antid in range(SENSOR_COUNT)]
            self._sample_window_generation += 1

        # 3. Direct route channels to their respective rendering threads
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

        # FIX: Pass the raw, unmutated trace snapshot to the map renderer to slice individual bursts
        if self.display_mode == "radar" and raw_traces_snapshot: 
            if len(raw_traces_snapshot) == SENSOR_COUNT:
                try:
                    self._render_radar_heatmap(raw_traces_snapshot)
                except Exception as error:
                    self.logger.exception("Radar mapping view computation failed: %s", error)

    def _render_radar_heatmap(self, traces):
        """Generate a calibrated azimuth-versus-delay heatmap matching the CSI demo."""
        if self._tx_waveform is None:
            self._radar_status = "⚠️ No Transmitter: Start Pluto TX in the Display settings sidebar."
            self.radarStatusChanged.emit()
            return

        if len(traces) < SENSOR_COUNT:
            self._radar_status = f"⏳ Waiting for array channels: Got {len(traces)}/{SENSOR_COUNT} antennas."
            self.radarStatusChanged.emit()
            return

        config = self._current_ping_config()
        pings_per_256 = max(1, min(config.get("pings_per_256", 2), CHUNK_SAMPLES))
        burst_length = CHUNK_SAMPLES // pings_per_256

        corrected, calibration_ready = self._angle_corrected_traces(traces)
        
        if not calibration_ready:
            self._radar_status = "⚠️ Array Uncalibrated: Click 'Run Calibration' in the receiver settings drawer."
            self.radarStatusChanged.emit()
            return

        if corrected is None or corrected.shape[0] < SENSOR_COUNT:
            self._radar_status = "❌ Processing Error: Calibrated array matrix size invalid."
            self.radarStatusChanged.emit()
            return

        valid_burst_len = min(burst_length, corrected.shape[1])
        if valid_burst_len <= 0:
            return
        
        sliced_traces = corrected[:, :valid_burst_len]
        geometry = self._angle_geometry()
        row_count, column_count = geometry.shape
        if row_count == 0 or column_count == 0:
            return

        spatial_samples = np.zeros((row_count, column_count, valid_burst_len), dtype=np.complex64)
        for row in range(row_count):
            for column in range(column_count):
                antenna_id = int(geometry[row, column])
                if 0 <= antenna_id < sliced_traces.shape[0]:
                    spatial_samples[row, column] = sliced_traces[antenna_id]

        # Horizontal Angular processing space
        column_fft_size = 256
        spatial_window = np.hanning(column_count).astype(np.float32)[None, :, None]
        spatial_fft = np.fft.fftshift(
            np.fft.fft(spatial_samples * spatial_window, n=column_fft_size, axis=1),
            axes=1,
        )

        # Max intensity projection (compress vertical rows)
        power_map = np.sum(np.abs(spatial_fft) ** 2, axis=0)

        # FIXED: Resample/Pad the Delay/Range axis to a fixed 256x256 matrix layout
        # This keeps texture dimensions stable regardless of changes to pings_per_256
        ui_grid_size = 256
        final_power_grid = np.zeros((column_fft_size, ui_grid_size), dtype=np.float32)
        fill_delay_bins = min(valid_burst_len, ui_grid_size)
        final_power_grid[:, :fill_delay_bins] = power_map[:, :fill_delay_bins]

        # Convert to logarithmic scaling
        power_db = 10.0 * np.log10(np.maximum(final_power_grid, 1e-12))
        floor = float(np.percentile(power_db, 15.0))
        ceiling = float(np.percentile(power_db, 99.5))
        norm_map = np.clip((power_db - floor) / max(ceiling - floor, 1e-6), 0.0, 1.0)

                # Apply colormap to the stabilized 256x256 grid matrix
        colormap = matplotlib.colormaps.get_cmap("viridis")
        color_data = colormap(np.transpose(norm_map)) 
        np_data = (color_data * 255).astype(np.uint8)

        # FIX: Align array shape metrics directly with the active property outputs
        self._radar_angle_size = int(np_data.shape[1])  # Dynamic texture columns
        self._radar_delay_size = int(np_data.shape[0])  # Dynamic texture rows
        self.radarConfigChanged.emit()

        self._radar_status = "🟢 Processing Active: Streaming 2D Map (256x256) to GPU Shaders."
        self.radarStatusChanged.emit()

        # Flatten into a strict 262,144 byte RGBA stream (256 * 256 * 4)
        image_vector = np_data.flatten().tolist()
        self.radarMapChanged.emit(image_vector)



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
            if self.iqcontrol is not None:
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
        if self._tx_waveform is None:
            self._angle_estimate_deg = float("nan")
            self._elevation_estimate_deg = float("nan")
            self._angle_status = "Start Pluto TX to estimate angle."
            self.angleChanged.emit()
            return

        # Traces are now pre-matched and pre-canceled by _publish_sample_windows
        corrected, calibration_ready = self._angle_corrected_traces(traces)

        if not calibration_ready:
            self._angle_estimate_deg = float("nan")
            self._elevation_estimate_deg = float("nan")
            self._angle_status = "Run IQ calibration before estimating angle."
            self.angleChanged.emit()
            return
        if len(traces) < SENSOR_COUNT:
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
            self._angle_status = "Matched chirp window is empty."
            self.angleChanged.emit()
            return
        spatial_samples = np.zeros((row_count, column_count, sample_count), dtype=np.complex64)
        for row in range(row_count):
            for column in range(column_count):
                antenna_id = geometry[row, column]
                if 0 <= antenna_id < corrected.shape[0]:
                    spatial_samples[row, column] = corrected[antenna_id]

        # Transform the configured 2D aperture and average power over time.
        # This preserves row phase for elevation and column phase for azimuth.
        row_fft_size = max(16, row_count * 16)
        column_fft_size = max(64, column_count * 64)
        spatial_fft = np.fft.fftshift(
            np.fft.fft2(spatial_samples, s=(row_fft_size, column_fft_size), axes=(0, 1)),
            axes=(0, 1),
        )
        power_2d = np.mean(np.abs(spatial_fft) ** 2, axis=2)
        if np.max(power_2d) <= 1e-12:
            self._angle_estimate_deg = float("nan")
            self._elevation_estimate_deg = float("nan")
            self._angle_status = "Insufficient signal strength for angle estimation."
            self.angleChanged.emit()
            return

        row_frequency = np.fft.fftshift(np.fft.fftfreq(row_fft_size))
        column_frequency = np.fft.fftshift(np.fft.fftfreq(column_fft_size))
        u = 2.0 * column_frequency[np.newaxis, :]
        v = 2.0 * row_frequency[:, np.newaxis]
        visible = (u * u + v * v) <= 1.0
        elevation_grid = np.rad2deg(np.arcsin(np.clip(v, -1.0, 1.0)))
        azimuth_grid = np.rad2deg(np.arctan2(u, np.sqrt(np.maximum(1.0 - u * u - v * v, 0.0))))
        visible_power = np.where(visible, power_2d, 0.0)
        peak_flat = int(np.argmax(visible_power))
        peak_row, peak_column = np.unravel_index(peak_flat, visible_power.shape)
        if not (0 <= peak_row < azimuth_grid.shape[0] and 0 <= peak_column < azimuth_grid.shape[1]):
            self._angle_status = "Could not locate a valid spatial peak."
            self.angleChanged.emit()
            return
        self._angle_estimate_deg = float(azimuth_grid[peak_row, peak_column])
        # Elevation depends only on the row spatial frequency, so this grid
        # intentionally has one column and must not be indexed by peak_column.
        self._elevation_estimate_deg = float(elevation_grid[peak_row, 0])

        # The chart remains azimuth-only: select the strongest elevation
        # response nearest each requested azimuth value.

        # 1. Collapse the vertical elevation axis using a Maximum Intensity Projection
        # This keeps the maximum target energy for every horizontal column bin
        radar_1d_profile = np.sum(visible_power, axis=0) # Shape: (column_fft_size,)
        
        # 2. Extract the true horizontal azimuth angles for the center row of your FFT grid
        # This gives us the exact physical angles that match each of the 256 columns
        center_row = row_fft_size // 2
        fft_native_angles = azimuth_grid[center_row, :]

        # 3. Use 1D Linear Interpolation to map the 256 native bins to the 361 UI bins
        # This smoothly maps values without the "nearest-neighbor" index jumping artifact
        ui_angles = np.linspace(-90.0, 90.0, 361)
        spectrum_interp = np.interp(ui_angles, fft_native_angles, radar_1d_profile)
        
        # 4. Normalize the spectrum safely
        max_val = np.max(spectrum_interp)
        normalized_spectrum = spectrum_interp / max_val if max_val > 0 else spectrum_interp
        
        # 5. Commit directly to the UI spectrum (No blur filters applied!)
        self._angle_spectrum = normalized_spectrum.astype(np.float32)
        self._angle_status = "2D Spatial FFT using configured geometry and internal calibration."
        self.angleChanged.emit()


    def _set_fft_size(self, n):
        if n == self.fft_size or n < CHUNK_SAMPLES or n % CHUNK_SAMPLES != 0:
            return
        self.fft_size = n
        self._win = np.hanning(n).astype(np.float32)
        self._fft_scale = max(float(np.sum(self._win)) * ADC_FULL_SCALE, 1.0)
        self.block_acc = {}
        self.block_time = {}
        for p in self.providers:
            p.clear()
        self._publish_sample_windows({})
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
        """Build one 256-sample cyclic TX buffer with the current UI-config values."""
        cfg = self._current_ping_config() if config is None else config
        chirp_frames = int(cfg["chirp_frames"])
        bw_hz = float(cfg["bandwidth_hz"])
        pings_per_256 = int(cfg["pings_per_256"])
        fs_hz = 40e6

        pings_per_256 = max(1, min(pings_per_256, CHUNK_SAMPLES))
        burst_length = CHUNK_SAMPLES // pings_per_256
        chirp_frames = max(1, min(chirp_frames, burst_length))

        t = np.arange(chirp_frames, dtype=np.float64) / fs_hz
        chirp_duration = chirp_frames / fs_hz
        f_start = -bw_hz / 2.0
        f_end = bw_hz / 2.0
        k = (f_end - f_start) / chirp_duration
        phase = 2.0 * np.pi * (f_start * t + 0.5 * k * (t ** 2))
        active_chirp = np.exp(1j * phase).astype(np.complex64)

        zero_samples = max(0, burst_length - chirp_frames)
        single_burst = np.concatenate((active_chirp, np.zeros(zero_samples, dtype=np.complex64)))

        # Match periodic_ping.py: repeat the chirp-plus-silence block.
        waveform_256 = np.tile(single_burst, pings_per_256)[:CHUNK_SAMPLES]
        if waveform_256.size != CHUNK_SAMPLES:
            raise ValueError(f"TX waveform must contain exactly {CHUNK_SAMPLES} samples")

        waveform_scaled = waveform_256 * AMPLITUDE_TARGET
        waveform_int16 = np.real(waveform_scaled).astype(np.int16) + 1j * np.imag(waveform_scaled).astype(np.int16)
        return waveform_int16

    def _matched_filter(self, iq):
        """Return normalized chirp correlation and local peak indices."""
        if self._tx_waveform is None:
            return np.zeros(len(iq), dtype=np.float32), []
        config = self._current_ping_config()
        pings_per_256 = max(1, min(config["pings_per_256"], CHUNK_SAMPLES))
        burst_length = CHUNK_SAMPLES // pings_per_256
        chirp_frames = max(1, min(config["chirp_frames"], burst_length))
        reference = np.asarray(self._tx_waveform[:chirp_frames], dtype=np.complex64)
        received = np.asarray(iq, dtype=np.complex64)
        if reference.size == 0 or received.size == 0:
            return np.zeros(received.size, dtype=np.float32), []
        correlation = np.convolve(received, np.conj(reference[::-1]), mode="same")
        reference_energy = float(np.vdot(reference, reference).real)
        received_energy = np.convolve(np.abs(received) ** 2, np.ones(burst_length), mode="same")
        denominator = np.sqrt(np.maximum(reference_energy * received_energy, 1e-12))
        score = (np.abs(correlation) / denominator).astype(np.float32)
        plotted_correlation = np.abs(correlation).astype(np.float32);

        threshold = 0.35
        spacing = max(1, reference.size // 4)
        local_maximum = np.ones(score.size, dtype=bool)
        if score.size > 2:
            local_maximum[1:-1] = (score[1:-1] >= score[:-2]) & (score[1:-1] >= score[2:])
        candidates = np.flatnonzero((score >= threshold) & local_maximum)
        peaks = []
        for index in candidates[np.argsort(score[candidates])[::-1]]:
            if all(abs(int(index) - peak) >= spacing for peak in peaks):
                peaks.append(int(index))
        peaks.sort()
        return correlation,plotted_correlation, peaks

    def _angle_matched_trace(self, iq):
        """Return complex matched-filter output for the active chirp."""
        if self._tx_waveform is None:
            return np.asarray(iq, dtype=np.complex64)
        config = self._current_ping_config()
        pings_per_256 = max(1, min(config["pings_per_256"], CHUNK_SAMPLES))
        burst_length = CHUNK_SAMPLES // pings_per_256
        chirp_frames = max(1, min(config["chirp_frames"], burst_length))
        reference = np.asarray(self._tx_waveform[:chirp_frames], dtype=np.complex64)
        received = np.asarray(iq, dtype=np.complex64)
        if reference.size == 0 or received.size == 0:
            return received
        return np.convolve(received, np.conj(reference[::-1]), mode="same").astype(np.complex64)

    @PyQt6.QtCore.pyqtSlot(float, int, float, int)
    def _start_ping_tx(self, gain_from_ui, chirp_frames=None, bandwidth_hz=None, pings_per_256=None):
        """Initialize Pluto TX and submit a waveform built from the current UI values."""
        if getattr(self, "sdr", None) is not None:
            self._stop_ping_tx()

        cfg = self._current_ping_config()
        if chirp_frames is not None:
            cfg["chirp_frames"] = int(chirp_frames)
        if bandwidth_hz is not None:
            cfg["bandwidth_hz"] = float(bandwidth_hz)
        if pings_per_256 is not None:
            cfg["pings_per_256"] = int(pings_per_256)
        cfg["tx_gain_db"] = float(gain_from_ui if gain_from_ui is not None else cfg["tx_gain_db"])

        # Force a fresh Pluto instance and a fresh cyclic TX buffer each time
        # the UI restarts ping TX. This is required because changing chirp
        # length / burst count alters the 256-sample waveform contents, and
        # leaving the old buffer in place reuses the stale pattern.
        self.sdr = adi.Pluto("ip:192.168.2.1")
        self.sdr.sample_rate = int(40e6)
        self.sdr.tx_rf_bandwidth = int(cfg["bandwidth_hz"])
        self.sdr.tx_lo = int(2.4e9)
        gain_db = max(-89.75, min(0.0, cfg["tx_gain_db"]))
        self.sdr.tx_hardwaregain_chan0 = gain_db
        applied_gain_db = float(self.sdr.tx_hardwaregain_chan0)
        print(f"Pluto TX gain requested={cfg['tx_gain_db']:.2f} dB, applied={applied_gain_db:.2f} dB", file=sys.stderr)
        print(f"Pluto TX waveform config: chirp_frames={cfg['chirp_frames']}, bandwidth_hz={cfg['bandwidth_hz']}, pings_per_256={cfg['pings_per_256']}", file=sys.stderr)

        self._tx_waveform = self._build_ping_waveform(cfg)
        if self._tx_waveform.size != CHUNK_SAMPLES:
            raise ValueError(f"TX waveform must contain exactly {CHUNK_SAMPLES} samples")
        self._angle_estimate_deg = float("nan")
        self._elevation_estimate_deg = float("nan")
        self._angle_spectrum.fill(0)
        self._angle_status = "Pluto restarted. Run IQ calibration before estimating angle."
        with self._tx_lock:
            try:
                self.sdr.tx_destroy_buffer()
            except Exception:
                pass
            self.sdr.tx_cyclic_buffer = True
            self.sdr.tx(np.tile(self._tx_waveform, 8))
            self._last_tx_time = time.monotonic()
        self.txStateChanged.emit()

    @PyQt6.QtCore.pyqtSlot()
    def _stop_ping_tx(self):
        """Stop TX and destroy the cyclic buffer."""
        self._angle_estimate_deg = float("nan")
        self._elevation_estimate_deg = float("nan")
        self._angle_spectrum.fill(0)
        self._angle_status = "Run IQ calibration, then start Pluto TX."
        if getattr(self, "sdr", None) is not None:
            with self._tx_lock:
                try:
                    self.sdr.tx_destroy_buffer()
                except Exception:
                    pass
                try:
                    self.sdr.tx_cyclic_buffer = False
                except Exception:
                    pass
            self.sdr = None
            self._tx_waveform = None
        self.angleChanged.emit()
        self.txStateChanged.emit()

    @PyQt6.QtCore.pyqtSlot(int)
    def set_display_width(self, w):
        """Follow the window size: waterfall pixel width per sensor."""
        w = max(128, min(2048, (int(w) // 32) * 32))
        if w == self.display_width:
            return
        self.display_width = w
        for p in self.providers:
            p.resize(w)

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
        return getattr(self, '_tx_waveform', None) is not None

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
        return len(self.pool.boards) if hasattr(self, "pool") else 1

    @PyQt6.QtCore.pyqtProperty("QVariantList", constant=True)
    def boardHosts(self):
        return [b.host for b in self.pool.boards] if hasattr(self, "pool") else []

    @PyQt6.QtCore.pyqtProperty(bool, notify=displayModeChanged)
    def radarMode(self):
        return self.display_mode == "radar"

    @PyQt6.QtCore.pyqtProperty(str, notify=radarStatusChanged)
    def radarStatus(self):
        return self._radar_status

        # Ensure this property setup section explicitly matches the demo footprint
    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def angleSize(self):
        # The copied shader looks directly for 'angleSize' to parse the horizontal texture columns
        return getattr(self, "_radar_angle_size", 256)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def delaySize(self):
        # The copied shader looks directly for 'delaySize' to parse the vertical delay rows
        return getattr(self, "_radar_delay_size", 256)

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def delayMin(self):
        # Maps to the start boundary of your pulse repetition interval
        return 0

    @PyQt6.QtCore.pyqtProperty(int, constant=False, notify=radarConfigChanged)
    def delayMax(self):
        # Maps to the maximum delay cells inside your 256 matrix layout
        return 256




    @PyQt6.QtCore.pyqtSlot(int)
    def set_active_board(self, index):
        """Tab switch: render this board's waterfalls (only the active board
        is rendered — with 4+ boards rendering everything is too costly; the
        clusters span all boards, so switching just changes the slice)."""
        if index == self.active_board or not hasattr(self, "pool") or not (0 <= index < len(self.pool.boards)):
            return
        self.active_board = index
        self.block_acc = {}
        self.block_time = {}
        for p in self.providers:
            p.clear()
        if self.display_mode in ("time", "ping", "constellation", "spectrum", "angle", "radar"):
            self._publish_sample_windows({})

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
        
        # 1. Include "radar" in the active radar pre-processing context
        if self._tx_waveform is not None and self.display_mode in ("ping", "angle", "radar") and traces:
            # Coherently compress incoming chirps into sharp range profile peaks
            matched_traces = {antid: self._angle_matched_trace(trace) for antid, trace in traces.items()}
            common_length = min((t.size for t in matched_traces.values()), default=0)
            
            if common_length > 0:
                matched_traces = {antid: t[:common_length] for antid, t in matched_traces.items()}
                common_power = np.sum([np.abs(t) ** 2 for t in matched_traces.values()], axis=0)
                
                # Route appropriate domains based on active mode context
                if self.display_mode in ("angle", "radar"):
                    # Angle and Radar modes expect pulse-compressed matched-filter vectors
                    if self.remove_los:
                        traces = self._cancel_line_of_sight(matched_traces, common_power)
                    else:
                        traces = matched_traces
                elif self.display_mode == "ping":
                    # Ping Chart Mode expects raw voltage vectors unless suppressed
                    if self.remove_los:
                        traces = self._cancel_line_of_sight(traces, common_power)
                    else:
                        pass

        # 2. Lock and publish the variables to the UI thread cache
        with self._sample_window_lock:
            self._sample_window_present = [antid in traces for antid in range(SENSOR_COUNT)]
            self._sample_windows = [np.asarray(traces.get(antid, np.zeros(self.fft_size, dtype=np.complex64))).copy() for antid in range(SENSOR_COUNT)]
            self._sample_window_generation += 1

        # 3. Direct route channels to their respective rendering threads
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

        # Fix: Feed the pre-matched cached window structure to the map renderer
        if self.display_mode == "radar" and traces: 
            with self._sample_window_lock:
                # Use a thread-safe snapshot clone from the cached buffer
                current_snapshot = {sensor_id: self._sample_windows[sensor_id].copy() for sensor_id in range(SENSOR_COUNT) if self._sample_window_present[sensor_id]}
            if len(current_snapshot) == SENSOR_COUNT:
                self._render_radar_heatmap(current_snapshot)



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
        points = [
            PyQt6.QtCore.QPointF(-90.0 + index * 180.0 / max(1, spectrum.size - 1), float(value))
            for index, value in enumerate(spectrum)
        ]
        series.replace(points)

    def _refresh_spectrum_config(self):
        """Track RF center/sample rate without blocking the GUI thread."""
        now = time.monotonic()
        if now - self._spectrum_config_read_at < 1.0:
            return
        self._spectrum_config_read_at = now
        try:
            cfg = self.pool.get_config()
            sample_rate_hz = DECIM_TO_FS.get(int(cfg.get("adc_decimation", 1)), DECIM_TO_FS[1])
            receivers = cfg.get("receivers", [])
            center_hz = float(receivers[0].get("rf_freq_hz", 2437e6)) if receivers else 2437e6
            with self._sample_window_lock:
                self._spectrum_sample_rate_hz = sample_rate_hz
                self._spectrum_center_hz = center_hz
        except Exception as e:
            print(f"spectrum config refresh error: {e}", file=sys.stderr)

    def _on_iq_cluster(self, cluster):
        """Pool cluster callback (runs on the pool's processing thread; keep
        it light). Complete clusters arrive immediately, settled partial ones
        after the settle timeout — the block assembler reorders by index."""
        with self._pending_lock:
            self._pending_clusters.append(cluster)
            if len(self._pending_clusters) > 2048:
                del self._pending_clusters[:1024]

    def _drain_sets(self):
        """Drain raw or accumulated clusters into 256-sample display sets.

        An accumulated vector is split back into its ordered vector sections,
        using ``source_chunk_start + lane`` as the display-grid index. This is
        the same mapping as the controller web waterfall and lets the existing
        fft_size/256 block assembler consume either wire format unchanged.
        """
        with self._pending_lock:
            clusters, self._pending_clusters = self._pending_clusters, []
        board = self.active_board
        if not clusters or board >= len(self.pool.boards):
            return []
        revision = self.pool.boards[board].revision
        positions = [revision.antenna_id_to_row_col(antenna_id) for antenna_id in range(SENSOR_COUNT)]
        sets = []
        for cluster in clusters:
            if isinstance(cluster, IQAccumCluster):
                completion = cluster.section_completion
                sections = cluster.iq_sections
                for lane in range(cluster.vector_chunks):
                    per = {
                        antid: sections[board, row, col, lane]
                        for antid, (row, col) in enumerate(positions)
                        if completion[board, row, col, lane]
                    }
                    if per:
                        sets.append((cluster.source_chunk_start + lane, per))
            else:
                completion = cluster.completion
                iq = cluster.iq
                per = {antid: iq[board, row, col] for antid, (row, col) in enumerate(positions) if completion[board, row, col]}
                if per:
                    sets.append((cluster.chunk_index, per))
        sets.sort(key=lambda entry: entry[0])
        return sets

    def _render(self):
        while self.render_running:
            time.sleep(1 / 20)
            try:
                sets = self._drain_sets()
                if not sets:
                    continue
                if not self._first_sets_logged:
                    self._first_sets_logged = True
                    self.logger.info("first synchronized IQ chunk sets received, display running")
                mode = "time" if self.display_mode == "ping" else self.display_mode
                if mode == "spectrum":
                    self._refresh_spectrum_config()
                # Every display mode goes through the block assembler (B >= 1
                # chunks per display unit): it renders strictly ordered by
                # chunk index and thereby also absorbs the out-of-order
                # delivery of settled partial sets behind complete ones.
                self._render_block_rows(sets, mode)
            except Exception as e:
                print(f"render error: {e}", file=sys.stderr)

    def _render_block_rows(self, sets, mode):
        """Assemble fft_size/256 CONSECUTIVE chunks per sensor into one
        display unit, a waterfall row or one time-domain trace window (blocks
        aligned to the synchronized index grid, so rows and traces line up in
        time across sensors; a sensor that did not deliver a block gets a
        placeholder row).

        Eviction: a block renders once a NEWER block exists (the normal case —
        the session's unwrapped chunk index is monotonic) or once it has not
        been touched for a while (defense against a stale higher key, e.g. a
        post-sync straggler from the old time base, permanently masking the
        `b < max_block` rule — that failure mode rendered ~25% of rows broken
        after the first index wrap before the index was unwrapped)."""
        B = self.fft_size // CHUNK_SAMPLES
        now = time.time()
        for idx, per in sets:
            b = idx // B
            block = self.block_acc.setdefault(b, {})
            for antid, iq in per.items():
                block.setdefault(antid, {})[idx % B] = iq
            self.block_time[b] = now
        max_block = max(self.block_acc.keys())
        ready = sorted(b for b in self.block_acc.keys() if b < max_block or now - self.block_time.get(b, now) > 2.0)
        if mode in ("time", "constellation", "spectrum", "angle", "radar"):
            self._render_sample_blocks(ready, B, mode)
            return
        for b in ready:
            block = self.block_acc.pop(b, None)
            self.block_time.pop(b, None)
            if block is None:
                continue  # block_acc was swapped by a concurrent FFT-size change
            specs = {}
            for antid, slots in block.items():
                if len(slots) == B:
                    specs[antid] = self._display_spectrum(np.concatenate([slots[k] for k in range(B)]))
            if self.complete_only and len(specs) < SENSOR_COUNT:
                continue
            for antid in range(SENSOR_COUNT):
                if mode == "phase":
                    if antid in specs and 0 in specs:
                        rel = specs[antid] * np.conj(specs[0])
                        corr = self.iqcontrol.display_correction(self.active_board, antid, self.fft_size) if self.iqcontrol is not None else None
                        if corr is not None:
                            rel = rel * corr
                        row = self._phase_row(rel)
                    else:
                        row = self._placeholder_row()
                else:
                    row = self._power_row(specs[antid]) if antid in specs else self._placeholder_row()
                self.providers[antid].add_rows(row[np.newaxis, :])
                self.rows_rendered[antid] += 1

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
            if mode in ["angle", "radar"]:
                # Angle processing must see one synchronized chunk. Do not
                # concatenate the block's B chunks before matched filtering.
                common_slots = set.intersection(*(set(slots) for slots in block.values())) if block else set()
                if len(block) < SENSOR_COUNT or not common_slots:
                    continue
                slot = max(common_slots)
                chosen = {antid: slots[slot] for antid, slots in block.items()}
                break
            traces = {antid: np.concatenate([slots[k] for k in range(B)]) for antid, slots in block.items() if len(slots) == B}
            if len(traces) >= (SENSOR_COUNT if self.complete_only else 1):
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

    def _start_iq_gui(self):
        """After the framework pool is up: expose the pool's QML adapter,
        subscribe the display to delivered clusters, and enter IQ mode. Sync
        (anchors from WiFi packets) BEFORE entering IQ."""
        hosts = ", ".join(b.host for b in self.pool.boards)
        self.logger.info(f"IQ capture starting ({len(self.pool.boards)} board(s): {hosts})")
        self.iqcontrol = self.create_iq_controller()
        self.iqcontrol.set_apply_calibration(bool(self.appconfig.get("apply_calibration")))
        self.iqcontrol.calibrationChanged.connect(self._on_iq_calibration_changed)
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)
        # the display also wants settled partial sets (missing sensors render
        # as placeholder rows instead of stalling the waterfall)
        self.pool.add_iq_callback(self._on_iq_cluster, include_partial=True)
        self.pool.add_accumulation_callback(self._on_iq_cluster, include_partial=True)

        def _enter():
            try:
                self.logger.info("acquiring array-wide time sync...")
                self.pool.sync()
                self.logger.info("time sync done, switching the array to IQ sampling mode...")
                cfg = self.pool.get_config()
                cfg["mode"] = "iq"
                self.pool.apply_config(cfg)
                self.logger.info("IQ mode entered; waiting for synchronized chunk sets...")
            except Exception as e:
                self.logger.error(f"could not enter IQ mode: {e}")

        threading.Thread(target=_enter, daemon=True).start()
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
        self.render_running = False
        self._stop_ping_tx()
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
        qml_path = pathlib.Path(__file__).resolve().parent / "ping-analyzer-ui.qml"
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
    app = EspargosSignalAnalyzer(sys.argv)
    try:
        sys.exit(app.exec())
    except KeyboardInterrupt:
        print("Interrupted by user; shutting down cleanly.", file=sys.stderr)
        app.render_running = False
        app._stop_ping_tx()
        app.quit()
        sys.exit(130)
