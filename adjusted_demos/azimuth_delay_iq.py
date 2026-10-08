#!/usr/bin/env python
"""Azimuth/sample-delay display driven by synchronized IQ chunks.

Example with the repository's single-board configuration::

    python3 adjusted_demos/azimuth_delay_iq.py -c config/single-espargos-one.yml myspargel.local
"""

import pathlib
import sys
import threading

import adi
import numpy as np
import PyQt6.QtCore
import PyQt6.QtCharts
from matplotlib import colormaps

repository_root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repository_root))
sys.path.insert(0, str(repository_root / "addons" / "iqsampling"))

from espargos_iqsampling import CHUNK_SAMPLES, DECIM_TO_FS, IQAccumCluster, SENSOR_COUNT
from espargos_iqsampling.iq_application import ESPARGOSIQApplication
from espargos.array_processing import music_spectrum


BEAMSPACE_OVERSAMPLING = 16
TX_SAMPLE_RATE_HZ = 40e6
TX_RF_FREQUENCY_HZ = 2.4e9
TX_AMPLITUDE = 2**14
SPEED_OF_LIGHT_M_PER_S = 299_792_458.0
ANTENNA_SPACING_WAVELENGTHS = 0.5
MUSIC_SCAN_POINTS = 721


class AzimuthDelayIQApp(ESPARGOSIQApplication):
    """Render one synchronized IQ chunk as an azimuth/sample-delay image."""

    DEFAULT_CONFIG = {
        "delay_min": 0,
        "delay_max": CHUNK_SAMPLES - 1,
        "delay_offset_samples": 0,
        "complete_only": False,
        "chirp_frames": 16,
        "bandwidth_hz": 40e6,
        "pings_per_256": 2,
        "tx_gain_db": -20,
    }

    dataChanged = PyQt6.QtCore.pyqtSignal(list)
    configChanged = PyQt6.QtCore.pyqtSignal()
    targetChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(self, argv):
        super().__init__(argv)
        self._pending_lock = threading.Lock()
        self._pending_clusters = []
        self._angle_size = 1
        self._delay_size = CHUNK_SAMPLES
        self.data = []
        self.iqcontrol = None
        self.sdr = None
        self._tx_waveform = None
        self._tx_lock = threading.Lock()
        self._rx_sample_rate_hz = DECIM_TO_FS[1]
        self._iq_lock = threading.Lock()
        self._latest_iq = [np.zeros(CHUNK_SAMPLES, dtype=np.complex64) for _ in range(SENSOR_COUNT)]
        self._iq_generation = 0
        self._iq_consumed = [-1] * SENSOR_COUNT
        self._music_angle_deg = float("nan")
        self._music_spectrum = np.zeros(MUSIC_SCAN_POINTS, dtype=np.float32)
        self._target_delay_sample = float("nan")
        self._update_delay_size()

        self.appConfigChanged.connect(self._on_iq_config_changed)

    def _on_iq_config_changed(self, newconfig):
        if "delay_min" in newconfig or "delay_max" in newconfig or "delay_offset_samples" in newconfig:
            self._update_delay_size()
            self.configChanged.emit()

    def _update_delay_size(self):
        delay_min = max(0, int(self.appconfig.get("delay_min")))
        delay_max = min(CHUNK_SAMPLES - 1, int(self.appconfig.get("delay_max")))
        self._delay_size = max(1, delay_max - delay_min + 1)

    def _delay_offset_samples(self):
        value = self.appconfig.get("delay_offset_samples")
        return 0 if value is None else int(value)

    def _ping_config_value(self, key, default):
        value = self.appconfig.get(key, default)
        return default if value is None else value

    def _current_ping_config(self):
        return {
            "chirp_frames": int(self._ping_config_value("chirp_frames", 16)),
            "bandwidth_hz": float(self._ping_config_value("bandwidth_hz", 40e6)),
            "pings_per_256": int(self._ping_config_value("pings_per_256", 2)),
            "tx_gain_db": float(self._ping_config_value("tx_gain_db", -20)),
        }

    def _build_ping_waveform(self, config=None):
        cfg = self._current_ping_config() if config is None else config
        pings_per_256 = max(1, min(int(cfg["pings_per_256"]), CHUNK_SAMPLES))
        burst_length = CHUNK_SAMPLES // pings_per_256
        chirp_frames = max(1, min(int(cfg["chirp_frames"]), burst_length))
        bandwidth_hz = float(cfg["bandwidth_hz"])
        t = np.arange(chirp_frames, dtype=np.float64) / TX_SAMPLE_RATE_HZ
        duration = chirp_frames / TX_SAMPLE_RATE_HZ
        start_hz = -bandwidth_hz / 2.0
        slope_hz_per_second = bandwidth_hz / duration
        phase = 2 * np.pi * (start_hz * t + 0.5 * slope_hz_per_second * t**2)
        chirp = np.exp(1j * phase).astype(np.complex64)
        burst = np.concatenate((chirp, np.zeros(burst_length - chirp_frames, dtype=np.complex64)))
        waveform = np.tile(burst, pings_per_256)[:CHUNK_SAMPLES] * TX_AMPLITUDE
        return np.real(waveform).astype(np.int16) + 1j * np.imag(waveform).astype(np.int16)

    @PyQt6.QtCore.pyqtSlot(float, int, float, int)
    def _start_ping_tx(self, gain_from_ui, chirp_frames=None, bandwidth_hz=None, pings_per_256=None):
        if self.sdr is not None:
            self._stop_ping_tx()
        config = self._current_ping_config()
        if chirp_frames is not None:
            config["chirp_frames"] = int(chirp_frames)
        if bandwidth_hz is not None:
            config["bandwidth_hz"] = float(bandwidth_hz)
        if pings_per_256 is not None:
            config["pings_per_256"] = int(pings_per_256)
        config["tx_gain_db"] = float(gain_from_ui)
        self.sdr = adi.Pluto("ip:192.168.2.1")
        self.sdr.sample_rate = int(TX_SAMPLE_RATE_HZ)
        self.sdr.tx_rf_bandwidth = int(config["bandwidth_hz"])
        self.sdr.tx_lo = int(TX_RF_FREQUENCY_HZ)
        self.sdr.tx_hardwaregain_chan0 = max(-89.75, min(0.0, config["tx_gain_db"]))
        self._tx_waveform = self._build_ping_waveform(config)
        with self._tx_lock:
            try:
                self.sdr.tx_destroy_buffer()
            except Exception:
                pass
            self.sdr.tx_cyclic_buffer = True
            self.sdr.tx(np.tile(self._tx_waveform, 8))
        self.logger.info(
            "Pluto TX started: chirp_frames=%d bandwidth_hz=%.0f pings_per_256=%d gain_db=%.2f",
            config["chirp_frames"], config["bandwidth_hz"], config["pings_per_256"], config["tx_gain_db"],
        )

    @PyQt6.QtCore.pyqtSlot()
    def _stop_ping_tx(self):
        if self.sdr is None:
            return
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

    def _matched_filter(self, samples):
        """Return complex correlation versus relative TX-to-RX sample lag."""
        if self._tx_waveform is None:
            return None
        reference = np.asarray(self._tx_waveform, dtype=np.complex64)
        received = np.asarray(samples, dtype=np.complex64)
        correlation = np.convolve(received, np.conj(reference[::-1]), mode="full")
        # Full-correlation index len(reference)-1 is zero lag. Keep positive
        # lags only so image rows map directly to receive delay samples.
        return correlation[reference.size - 1 : reference.size - 1 + received.size]

    def _music_azimuth(self, horizontal_aperture):
        """Estimate one azimuth from the four-column IQ aperture.

        ``horizontal_aperture`` has shape (4, delay_samples). Each delay
        sample is a snapshot of the four-element array. MUSIC eigendecomposes
        their covariance, projects candidate steering vectors onto the noise
        subspace, and selects the largest pseudospectrum peak.
        """
        antenna_count = horizontal_aperture.shape[0]
        if antenna_count != 4:
            return float("nan"), np.zeros(MUSIC_SCAN_POINTS, dtype=np.float32)
        covariance = horizontal_aperture @ horizontal_aperture.conj().T
        covariance /= max(1, horizontal_aperture.shape[1])
        covariance += np.eye(antenna_count, dtype=np.complex64) * (np.trace(covariance).real * 1e-6 + 1e-12)
        wavelength = SPEED_OF_LIGHT_M_PER_S / TX_RF_FREQUENCY_HZ
        spacing_m = ANTENNA_SPACING_WAVELENGTHS * wavelength
        angles = np.linspace(-90.0, 90.0, MUSIC_SCAN_POINTS)
        direction_cosines = np.sin(np.deg2rad(angles))
        element_indices = np.arange(antenna_count)
        # music_spectrum expects (rows, columns, azimuth, elevation). The
        # single row here is intentional: only the four-column azimuth
        # aperture participates in this estimate.
        # The shared helper conjugates steering internally; this sign keeps
        # its reported angle aligned with the spatial-FFT/plot convention.
        steering = np.exp(-2j * np.pi * spacing_m / wavelength * np.outer(element_indices, direction_cosines))
        steering = steering[np.newaxis, :, :, np.newaxis]
        spectrum = music_spectrum(covariance, steering, source_count=1)[:, 0]
        spectrum /= np.max(spectrum) if np.max(spectrum) else 1.0
        return float(angles[np.argmax(spectrum)]), spectrum.astype(np.float32)

    def _refresh_rx_sample_rate(self):
        try:
            config = self.pool.get_config()
            self._rx_sample_rate_hz = DECIM_TO_FS.get(int(config.get("adc_decimation", 1)), DECIM_TO_FS[1])
        except Exception:
            pass

    def _publish_iq_samples(self, samples_by_antenna):
        with self._iq_lock:
            self._latest_iq = [
                np.asarray(samples_by_antenna.get(antenna_id, np.zeros(CHUNK_SAMPLES)), dtype=np.complex64).copy()
                for antenna_id in range(SENSOR_COUNT)
            ]
            self._iq_generation += 1

    @PyQt6.QtCore.pyqtSlot(int, PyQt6.QtCharts.QLineSeries, PyQt6.QtCharts.QLineSeries)
    def updateIqChart(self, antenna_id, i_series, q_series):
        """Publish one raw received-IQ trace to the verification monitor."""
        if not 0 <= antenna_id < SENSOR_COUNT:
            return
        with self._iq_lock:
            generation = self._iq_generation
            if self._iq_consumed[antenna_id] == generation:
                return
            samples = self._latest_iq[antenna_id].copy()
            self._iq_consumed[antenna_id] = generation
        i_series.replace([PyQt6.QtCore.QPointF(index, float(value)) for index, value in enumerate(samples.real)])
        q_series.replace([PyQt6.QtCore.QPointF(index, float(value)) for index, value in enumerate(samples.imag)])

    @PyQt6.QtCore.pyqtSlot()
    def update_data(self):
        with self._pending_lock:
            clusters, self._pending_clusters = self._pending_clusters, []
        if not clusters or not self.pool.boards:
            return

        board = self.pool.boards[0]
        positions = [board.revision.antenna_id_to_row_col(antenna_id) for antenna_id in range(SENSOR_COUNT)]
        latest = None
        latest_index = -1
        for cluster in clusters:
            if isinstance(cluster, IQAccumCluster):
                completion = cluster.section_completion[0]
                sections = cluster.iq_sections[0]
                for lane in range(cluster.vector_chunks):
                    per = {
                        antenna_id: sections[row, col, lane]
                        for antenna_id, (row, col) in enumerate(positions)
                        if completion[row, col, lane]
                    }
                    index = cluster.source_chunk_start + lane
                    if per and index >= latest_index:
                        latest, latest_index = per, index
            else:
                completion = cluster.completion[0]
                samples = cluster.iq[0]
                per = {
                    antenna_id: samples[row, col]
                    for antenna_id, (row, col) in enumerate(positions)
                    if completion[row, col]
                }
                if per and cluster.chunk_index >= latest_index:
                    latest, latest_index = per, cluster.chunk_index

        if latest is None:
            return

        # Keep this raw monitor independent of TX and matched filtering so it
        # can verify the receiver while the transmitter is stopped.
        self._publish_iq_samples(latest)

        if bool(self.appconfig.get("complete_only", False)) and len(latest) < SENSOR_COUNT:
            return

        if self._tx_waveform is None:
            return

        self._refresh_rx_sample_rate()

        rows = max(row for row, _ in positions) + 1
        columns = max(col for _, col in positions) + 1
        array = np.zeros((rows, columns, CHUNK_SAMPLES), dtype=np.complex64)
        for antenna_id, samples in latest.items():
            row, column = positions[antenna_id]
            filtered = self._matched_filter(samples)
            if filtered is not None:
                array[row, column] = filtered

        # The regular single-board geometry is 2 rows x 4 columns. Sum the
        # vertical row dimension, leaving the four-element azimuth aperture.
        horizontal_aperture = np.sum(array, axis=0)
        self._music_angle_deg, self._music_spectrum = self._music_azimuth(horizontal_aperture)
        self.musicChanged.emit()
        beamspace = np.fft.fftshift(
            np.fft.fft(horizontal_aperture, n=columns * BEAMSPACE_OVERSAMPLING, axis=0),
            axes=0,
        )
        image = np.abs(beamspace) ** 2
        delay_min = max(0, int(self.appconfig.get("delay_min")))
        delay_max = min(CHUNK_SAMPLES - 1, int(self.appconfig.get("delay_max")))
        spatial_bins = beamspace.shape[0]
        target_psi = np.pi * np.sin(np.deg2rad(self._music_angle_deg))
        spatial_psi = 2 * np.pi * np.fft.fftshift(np.fft.fftfreq(spatial_bins))
        angle_bin = int(np.argmin(np.abs(spatial_psi - target_psi)))
        target_delay = int(np.argmax(image[angle_bin, delay_min : delay_max + 1])) + delay_min
        self._target_delay_sample = float(target_delay)
        self.targetChanged.emit()
        image = image[:, delay_min : delay_max + 1]
        image = np.append(image, image[0:1], axis=0)
        self._set_image(image)

    def _set_image(self, data):
        maximum = np.max(data)
        normalized = data / maximum if maximum else data
        rgba = colormaps.get_cmap("viridis")(np.transpose(normalized)) * 255
        self._angle_size = rgba.shape[1]
        self._delay_size = rgba.shape[0]
        self.data = rgba.astype(np.uint8).reshape(-1).tolist()
        self.dataChanged.emit(self.data)

    def _on_iq_cluster(self, cluster):
        with self._pending_lock:
            self._pending_clusters.append(cluster)
            if len(self._pending_clusters) > 2048:
                del self._pending_clusters[:1024]

    def _start_iq_gui(self):
        self.iqcontrol = self.create_iq_controller()
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)
        self.pool.add_iq_callback(self._on_iq_cluster, include_partial=True)
        self.pool.add_accumulation_callback(self._on_iq_cluster, include_partial=True)

        def enter_iq_mode():
            try:
                self.pool.sync()
                config = self.pool.get_config()
                config["mode"] = "iq"
                self.pool.apply_config(config)
            except Exception as error:
                self.logger.error(f"could not enter IQ mode: {error}")

        threading.Thread(target=enter_iq_mode, daemon=True).start()

    @PyQt6.QtCore.pyqtProperty(int, notify=configChanged)
    def delaySize(self):
        return self._delay_size

    @PyQt6.QtCore.pyqtProperty(int, notify=configChanged)
    def angleSize(self):
        return self._angle_size

    @PyQt6.QtCore.pyqtProperty(int, notify=configChanged)
    def delayMin(self):
        return int(self.appconfig.get("delay_min"))

    @PyQt6.QtCore.pyqtProperty(int, notify=configChanged)
    def delayMax(self):
        return int(self.appconfig.get("delay_max"))

    @PyQt6.QtCore.pyqtProperty(float, notify=configChanged)
    def delayMinUs(self):
        return 1e6 * (int(self.appconfig.get("delay_min")) + self._delay_offset_samples()) / self._rx_sample_rate_hz

    @PyQt6.QtCore.pyqtProperty(float, notify=configChanged)
    def delayMaxUs(self):
        return 1e6 * (int(self.appconfig.get("delay_max")) + self._delay_offset_samples()) / self._rx_sample_rate_hz

    musicChanged = PyQt6.QtCore.pyqtSignal()

    @PyQt6.QtCore.pyqtProperty(float, notify=musicChanged)
    def musicAzimuth(self):
        return self._music_angle_deg

    @PyQt6.QtCore.pyqtProperty(float, notify=targetChanged)
    def targetDelaySample(self):
        return self._target_delay_sample

    @PyQt6.QtCore.pyqtProperty(float, notify=targetChanged)
    def targetDelayUs(self):
        if not np.isfinite(self._target_delay_sample):
            return float("nan")
        return 1e6 * (self._target_delay_sample + self._delay_offset_samples()) / self._rx_sample_rate_hz

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def sensorCount(self):
        return SENSOR_COUNT

    @PyQt6.QtCore.pyqtProperty(int, constant=True)
    def iqSampleCount(self):
        return CHUNK_SAMPLES

    @PyQt6.QtCore.pyqtProperty(float, constant=True)
    def adcFullScale(self):
        return 512.0

    def exec(self):
        self.initComplete.connect(self._start_iq_gui)
        self.initialize_pool(calibrate=False)
        qml_file = pathlib.Path(__file__).resolve().with_name("azimuth-delay-iq.qml")
        self.initialize_qml(qml_file, context_props={"iqcontrol": None})
        if not self.engine.rootObjects():
            return -1
        return super().exec()

    def onAboutToQuit(self):
        self._stop_ping_tx()
        super().onAboutToQuit()


if __name__ == "__main__":
    sys.exit(AzimuthDelayIQApp(sys.argv).exec())