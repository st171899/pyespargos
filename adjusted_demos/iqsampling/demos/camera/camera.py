#!/usr/bin/env python
"""Live IQ beamforming heatmap over a camera image.

The demo consumes synchronized raw-IQ blocks, applies the addon's fine
per-frequency calibration, selects useful FFT bins, and incoherently combines
their rectangular-array FFT beamspaces.  A combined-power IQ waterfall shows
which bins feed the spatial overlay.
"""

import argparse
import pathlib
import sys
import threading
import time

# pyespargos repository root (this demo lives below addons/iqsampling/)
sys.path.append(str(pathlib.Path(__file__).absolute().parents[4]))

import numpy as np
import PyQt6.QtCore

import espargos  # noqa: F401 -- discovers the IQ addon
from demos.common import CameraView, CombinedArrayMixin
from espargos_iqsampling import CHUNK_SAMPLES, DECIM_TO_FS
from espargos_iqsampling.iq_application import ESPARGOSIQApplication
from espargos_iqsampling.iq_pool import iq_receiver_config
from espargos_iqsampling.spatial import ExponentialArraySpectrum, music_beamspace_power, rectangular_fft_beamspace_power, select_frequency_bins
from espargos_iqsampling.waterfall import WaterfallImageProvider, combined_power_dbfs, power_db_row

try:
    from .iq_overlay import IQCameraOverlay
except ImportError:
    from iq_overlay import IQCameraOverlay


ADC_FULL_SCALE = 512.0
WATERFALL_HEIGHT = 180


class EspargosIQCamera(CombinedArrayMixin, ESPARGOSIQApplication):
    """IQ camera for a rectangular ESPARGOS combined array."""

    DEFAULT_CONFIG = {
        "camera": CameraView.DEFAULT_CONFIG,
        "beamformer": {
            "type": "FFT",
            "music_sources": 1,
            "fft_size": 1024,
            "bin_mode": "active",
            "reject_dc": True,
            "dc_half_width": 2,
            "threshold_db": 4.0,
            "min_coherence": 0.55,
            "selection_integration_s": 0.75,
            "max_bins": 64,
            "band_low_mhz": 2420.0,
            "band_high_mhz": 2450.0,
            "resolution_azimuth": 64,
            "resolution_elevation": 32,
            "integration_s": 0.35,
        },
        "visualization": {
            "space": "camera",
            "waterfall": True,
            "dynamic_range_db": 18.0,
            "azimuth_correction": 0.0,
            "elevation_correction": 0.0,
        },
    }

    def _add_argparse_arguments(self, parser):
        super()._add_argparse_arguments(parser)
        parser.add_argument("--no-camera", action="store_true", help="Show the spatial overlay on black instead of opening a video camera")
        parser.add_argument("--no-auto-calibration", action="store_true", help="Start without the reference-tone calibration sweep")

    def __init__(self, argv):
        parser = argparse.ArgumentParser(description="ESPARGOS IQ camera", add_help=False)
        super().__init__(argv, argparse_parent=parser)
        if self.args.no_camera:
            self.appconfig.set({"camera": {"enable": False}})

        self.iqcontrol = None
        self.fft_size = int(self.appconfig.get("beamformer", "fft_size"))
        self._window = np.hanning(self.fft_size).astype(np.float32)
        self._fft_scale = max(float(np.sum(self._window)) * ADC_FULL_SCALE, 1.0)
        self._block_lock = threading.Lock()
        self._blocks = {}
        self._latest_block = None
        self._latest_block_index = -1
        self._smoothed_power = None
        self._last_spatial_update = None
        self._last_peak_log = 0.0
        self._capture_signature = None
        self._spectral_history = ExponentialArraySpectrum(time_constant_s=float(self.appconfig.get("beamformer", "selection_integration_s")))

        self.waterfall = WaterfallImageProvider(self.fft_size, WATERFALL_HEIGHT)
        self.engine.addImageProvider("iq-camera-waterfall", self.waterfall)
        self.overlay = IQCameraOverlay(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.camera_view = CameraView(self.appconfig, parent=self, update_signal=self.appConfigChanged)
        self.video_camera = self.camera_view.initialize()
        self.appConfigChanged.connect(self._on_camera_config_changed)

    def _on_camera_config_changed(self, changed):
        beamformer = changed.get("beamformer", {}) if isinstance(changed, dict) else {}
        if not isinstance(beamformer, dict):
            return
        if "selection_integration_s" in beamformer:
            self._reset_processing_state()
        if "type" in beamformer or "music_sources" in beamformer:
            self._smoothed_power = None
        if "fft_size" not in beamformer:
            return
        fft_size = int(beamformer["fft_size"])
        if fft_size == self.fft_size or fft_size < CHUNK_SAMPLES or fft_size % CHUNK_SAMPLES:
            return
        with self._block_lock:
            self.fft_size = fft_size
            self._window = np.hanning(fft_size).astype(np.float32)
            self._fft_scale = max(float(np.sum(self._window)) * ADC_FULL_SCALE, 1.0)
            self._blocks = {}
            self._latest_block = None
        self.waterfall.resize(fft_size)
        self._reset_processing_state()

    def _reset_processing_state(self):
        self._smoothed_power = None
        self._last_spatial_update = None
        self._capture_signature = None
        self._spectral_history = ExponentialArraySpectrum(time_constant_s=float(self.appconfig.get("beamformer", "selection_integration_s")))

    def _prepare_pool_init(self, additional_calibrate_args):
        additional_calibrate_args = super()._prepare_pool_init(additional_calibrate_args)
        self._combined_array_calibration_args = {key: additional_calibrate_args[key] for key in ("cable_lengths", "cable_velocity_factors") if key in additional_calibrate_args}
        return additional_calibrate_args

    def _on_iq_cluster(self, cluster):
        """Assemble complete, index-aligned multi-chunk FFT blocks."""

        iq = self._build_combined_array_data(cluster.iq).astype(np.complex64, copy=False)
        self._store_iq_chunk(cluster.chunk_index, iq)

    def _on_accum_cluster(self, cluster):
        """Split an accumulated vector into the normal 256-sample FFT grid."""

        iq = self._build_combined_array_data(cluster.iq).astype(np.complex64, copy=False)
        for lane in range(cluster.vector_chunks):
            start = lane * CHUNK_SAMPLES
            self._store_iq_chunk(
                cluster.source_chunk_start + lane,
                iq[..., start : start + CHUNK_SAMPLES],
            )

    def _store_iq_chunk(self, chunk_index, iq):
        """File one array-wide 256-sample section into the FFT assembler."""

        with self._block_lock:
            chunks_per_block = self.fft_size // CHUNK_SAMPLES
            block_index = chunk_index // chunks_per_block
            slot = chunk_index % chunks_per_block
            block = self._blocks.setdefault(block_index, {})
            block[slot] = iq
            if len(block) == chunks_per_block and all(index in block for index in range(chunks_per_block)):
                self._latest_block = np.concatenate([block[index] for index in range(chunks_per_block)], axis=-1)
                self._latest_block_index = block_index
                self._blocks = {key: value for key, value in self._blocks.items() if key > block_index}
            elif len(self._blocks) > 16:
                for key in sorted(self._blocks)[:-8]:
                    self._blocks.pop(key, None)

    def _take_latest_block(self):
        with self._block_lock:
            block = self._latest_block
            self._latest_block = None
            return block

    def _capture_parameters(self):
        config = self.pool.get_config()
        sample_rate = DECIM_TO_FS.get(int(config.get("adc_decimation", 1)), DECIM_TO_FS[1])
        center_hz = float(iq_receiver_config(config).get("rf_freq_hz", 2437e6))
        return center_hz, sample_rate

    def _calibration_correction(self, center_hz, sample_rate):
        correction = self.pool.cal_correction(self.fft_size)
        if correction is None:
            return None
        calibrated_center = self.pool.calibration_center_frequency
        calibrated_rate = self.pool.calibration_sample_rate
        if calibrated_center != center_hz or calibrated_rate != sample_rate:
            return None
        return self._build_combined_array_data(correction)

    def _build_combined_array_data(self, data):
        """Map board-local IQ data into the configured rectangular array."""
        combined = espargos.combined_array.build_combined_array_data(
            self.indexing_matrix,
            np.asarray(data)[np.newaxis, ...],
        )
        return combined[0]

    @PyQt6.QtCore.pyqtSlot()
    def updateSpatialSpectrum(self):
        block = self._take_latest_block()
        if block is None:
            return
        try:
            center_hz, sample_rate = self._capture_parameters()
            reject_dc = bool(self.appconfig.get("beamformer", "reject_dc"))
            samples = block
            if reject_dc:
                samples = samples - np.mean(samples, axis=-1, keepdims=True)
            spectra = np.fft.fftshift(np.fft.fft(samples * self._window, axis=-1), axes=-1)
            instantaneous_power_db = combined_power_dbfs(spectra, self._fft_scale)
            frequencies = center_hz + np.fft.fftshift(np.fft.fftfreq(self.fft_size, d=1.0 / sample_rate))

            correction = self._calibration_correction(center_hz, sample_rate)
            if correction is None:
                self.overlay.set_capture_status("Uncalibrated — press Calibrate")
            else:
                spectra = spectra * correction
                self.overlay.set_capture_status("Calibrated")

            now = time.monotonic()
            capture_signature = (center_hz, sample_rate, self.fft_size)
            if capture_signature != self._capture_signature:
                self._reset_processing_state()
                self._capture_signature = capture_signature
            elapsed = now - (self._last_spatial_update or now - 0.05)
            self._spectral_history.update(spectra, elapsed)
            averaged_power = self._spectral_history.mean_power()
            power_db = 10.0 * np.log10(np.maximum(averaged_power / (self._fft_scale**2), 1e-12))
            coherence = self._spectral_history.coherence()
            if not self._spectral_history.ready:
                coherence = np.zeros_like(power_db)

            mode = str(self.appconfig.get("beamformer", "bin_mode"))
            selection = select_frequency_bins(
                power_db,
                mode,
                reject_dc=reject_dc,
                dc_half_width=int(self.appconfig.get("beamformer", "dc_half_width")),
                threshold_db=float(self.appconfig.get("beamformer", "threshold_db")),
                max_bins=int(self.appconfig.get("beamformer", "max_bins")),
                frequencies_hz=frequencies,
                band_low_hz=float(self.appconfig.get("beamformer", "band_low_mhz")) * 1e6,
                band_high_hz=float(self.appconfig.get("beamformer", "band_high_mhz")) * 1e6,
                coherence=coherence,
                min_coherence=float(self.appconfig.get("beamformer", "min_coherence")),
            )

            row = power_db_row(instantaneous_power_db, self.fft_size)
            unselected = ~selection.selected
            row[unselected, :3] = np.asarray(row[unselected, :3], dtype=np.float32) * 0.28
            row[selection.dc_mask, :3] = np.asarray((110, 25, 35), dtype=np.uint8)
            self.waterfall.add_rows(row)

            display_peak_index = selection.peak_index
            if selection.indices.size:
                display_peak_index = int(selection.indices[np.argmax(power_db[selection.indices])])
            peak_frequency = frequencies[display_peak_index] / 1e6 if display_peak_index is not None else float("nan")
            peak_coherence = coherence[display_peak_index] if display_peak_index is not None else float("nan")
            peak_snr = power_db[display_peak_index] - selection.noise_floor_db if display_peak_index is not None else float("nan")
            beamformer_type = str(self.appconfig.get("beamformer", "type")).upper()
            status = f"{beamformer_type} · {mode.title()} · {selection.indices.size} bin{'s' if selection.indices.size != 1 else ''} · peak {peak_frequency:.3f} MHz · coh {peak_coherence:.2f}"
            self.overlay.publish_waterfall(frequencies[0] / 1e6, frequencies[-1] / 1e6, status)
            self.overlay.publish_receiver_statistics(float(np.max(instantaneous_power_db)), block.shape[0] * block.shape[1])

            normalized_spectra = spectra / self._fft_scale
            resolution_azimuth = int(self.appconfig.get("beamformer", "resolution_azimuth"))
            resolution_elevation = int(self.appconfig.get("beamformer", "resolution_elevation"))
            if beamformer_type == "MUSIC":
                if self._spectral_history.ready:
                    spatial_power = music_beamspace_power(
                        self._spectral_history.covariance / (self._fft_scale**2),
                        selection.indices,
                        rows=block.shape[0],
                        columns=block.shape[1],
                        resolution_azimuth=resolution_azimuth,
                        resolution_elevation=resolution_elevation,
                        source_count=int(self.appconfig.get("beamformer", "music_sources")),
                        frequencies_hz=frequencies,
                        reference_frequency_hz=center_hz,
                    )
                else:
                    spatial_power = np.zeros((resolution_azimuth, resolution_elevation), dtype=np.float64)
            else:
                spatial_power = rectangular_fft_beamspace_power(
                    normalized_spectra,
                    selection.indices,
                    resolution_azimuth=resolution_azimuth,
                    resolution_elevation=resolution_elevation,
                )

            integration = max(0.0, float(self.appconfig.get("beamformer", "integration_s")))
            if self._smoothed_power is None or self._smoothed_power.shape != spatial_power.shape or integration == 0:
                self._smoothed_power = spatial_power
            else:
                alpha = 1.0 - np.exp(-elapsed / max(integration, 1e-6))
                self._smoothed_power = (1.0 - alpha) * self._smoothed_power + alpha * spatial_power
            self._last_spatial_update = now
            self.overlay.publish_spatial_spectrum(self._smoothed_power)

            if now - self._last_peak_log >= 2.0:
                summary = f"IQ camera: {beamformer_type}, {selection.indices.size} {mode} bins, peak {peak_frequency:.3f} MHz, " f"SNR {peak_snr:.1f} dB, coh {peak_coherence:.2f}"
                if np.any(self._smoothed_power):
                    azimuth_index, elevation_index = np.unravel_index(np.argmax(self._smoothed_power), self._smoothed_power.shape)
                    azimuth_sine = np.clip(2.0 * (azimuth_index - self._smoothed_power.shape[0] // 2) / self._smoothed_power.shape[0], -1.0, 1.0)
                    elevation_sine = np.clip(2.0 * (elevation_index - self._smoothed_power.shape[1] // 2) / self._smoothed_power.shape[1], -1.0, 1.0)
                    summary += f", beam az {np.degrees(np.arcsin(azimuth_sine)):+.1f}°, el {np.degrees(np.arcsin(elevation_sine)):+.1f}°"
                else:
                    summary += ", no coherent signal selected"
                self.logger.info(summary)
                self._last_peak_log = now
        except Exception as error:
            self.logger.error(f"IQ camera render failed: {error}")

    def _on_calibration_changed(self):
        self._reset_processing_state()
        self.overlay.set_capture_status("Calibrated" if self.pool.calibration is not None else "Uncalibrated — press Calibrate")

    def _start_iq_camera(self):
        self.iqcontrol = self.create_iq_controller(
            **self._combined_array_calibration_args,
        )
        self.iqcontrol.set_apply_calibration(True)
        self.iqcontrol.calibrationChanged.connect(self._on_calibration_changed)
        self.engine.rootContext().setContextProperty("iqcontrol", self.iqcontrol)
        self.pool.add_iq_callback(self._on_iq_cluster)
        self.pool.add_accumulation_callback(
            self._on_accum_cluster,
            callback_predicate=lambda cluster: cluster.is_complete,
        )

        def enter_and_calibrate():
            try:
                self.overlay.set_capture_status("Synchronizing")
                for attempt in range(2):
                    try:
                        self.pool.sync()
                        break
                    except RuntimeError:
                        if attempt:
                            raise
                        self.logger.warning("IQ camera sync attempt failed; retrying once")
                        time.sleep(1.0)
                config = self.pool.get_config()
                config["mode"] = "iq"
                self.pool.apply_config(config)
                if not self.args.no_auto_calibration:
                    self.overlay.set_capture_status("Calibrating")
                    center_hz, _sample_rate = self._capture_parameters()
                    self.pool.calibrate(
                        center_hz=center_hz,
                        **self._combined_array_calibration_args,
                    )
                self._reset_processing_state()
                self.overlay.set_capture_status("Calibrated" if self.pool.calibration is not None else "Uncalibrated — press Calibrate")
                self.logger.info("IQ camera is receiving synchronized blocks")
            except Exception as error:
                self.overlay.set_capture_status("Startup failed")
                self.logger.error(f"could not start IQ camera: {error}")

        threading.Thread(target=enter_and_calibrate, daemon=True).start()

    def onAboutToQuit(self):
        self.camera_view.stop()
        super().onAboutToQuit()

    def exec(self):
        self.initComplete.connect(self._start_iq_camera)
        self.initialize_pool(calibrate=False)
        self.initialize_qml(
            pathlib.Path(__file__).resolve().parent / "camera-ui.qml",
            context_props={
                "iqcontrol": None,
                "CameraView": self.camera_view,
                "overlayModel": self.overlay,
                "WebCam": self.video_camera,
            },
        )
        self.camera_view.start()
        return super().exec()


if __name__ == "__main__":
    application = EspargosIQCamera(sys.argv)
    sys.exit(application.exec())
