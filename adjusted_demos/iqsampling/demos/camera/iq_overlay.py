"""QML-facing state for the deliberately small IQ camera overlay."""

from __future__ import annotations

import numpy as np
import PyQt6.QtCore


class IQCameraOverlay(PyQt6.QtCore.QObject):
    receiverPowerChanged = PyQt6.QtCore.pyqtSignal(float)
    receiverPowerUnitChanged = PyQt6.QtCore.pyqtSignal()
    activeAntennasChanged = PyQt6.QtCore.pyqtSignal(float)
    beamspacePowerImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
    polarizationImagedataChanged = PyQt6.QtCore.pyqtSignal(list)
    macListChanged = PyQt6.QtCore.pyqtSignal(list)
    visualizationSpaceChanged = PyQt6.QtCore.pyqtSignal()
    polarizationVisibleChanged = PyQt6.QtCore.pyqtSignal()
    gridSpacingChanged = PyQt6.QtCore.pyqtSignal()
    resolutionAzimuthChanged = PyQt6.QtCore.pyqtSignal()
    resolutionElevationChanged = PyQt6.QtCore.pyqtSignal()
    macListEnabledChanged = PyQt6.QtCore.pyqtSignal()
    azimuthCorrectionChanged = PyQt6.QtCore.pyqtSignal()
    elevationCorrectionChanged = PyQt6.QtCore.pyqtSignal()
    waterfallGenerationChanged = PyQt6.QtCore.pyqtSignal()
    waterfallVisibleChanged = PyQt6.QtCore.pyqtSignal()
    spectrumStatusChanged = PyQt6.QtCore.pyqtSignal()
    frequencySpanChanged = PyQt6.QtCore.pyqtSignal()
    captureStatusChanged = PyQt6.QtCore.pyqtSignal()

    def __init__(self, appconfig, parent=None, update_signal=None):
        super().__init__(parent)
        self.appconfig = appconfig
        self._receiver_power = float("-inf")
        self._active_antennas = 0.0
        self._waterfall_generation = 0
        self._spectrum_status = "Waiting for IQ data"
        self._capture_status = "Starting"
        self._frequency_low_mhz = 0.0
        self._frequency_high_mhz = 0.0
        signal = self.appconfig.updateAppState if update_signal is None else update_signal
        signal.connect(self._on_update_app_state)

    def publish_receiver_statistics(self, power_dbfs, active_antennas):
        self._receiver_power = float(power_dbfs)
        self._active_antennas = float(active_antennas)
        self.receiverPowerChanged.emit(self._receiver_power)
        self.activeAntennasChanged.emit(self._active_antennas)

    def publish_spatial_spectrum(self, power):
        power = np.asarray(power, dtype=np.float64)
        db = 10.0 * np.log10(np.maximum(power, 1e-15))
        finite = db[np.isfinite(db)]
        peak = float(np.max(finite)) if finite.size else 0.0
        dynamic_range = max(1.0, float(self.appconfig.get("visualization", "dynamic_range_db")))
        intensity = np.clip((db - peak + dynamic_range) / dynamic_range, 0.0, 1.0) ** 2
        image = np.zeros(4 * power.size, dtype=np.uint8)
        image[1::4] = np.asarray(np.swapaxes(intensity, 0, 1).ravel() * 255.0, dtype=np.uint8)
        image[3::4] = 255
        self.beamspacePowerImagedataChanged.emit(image.tolist())

    def publish_waterfall(self, low_mhz, high_mhz, status):
        span_changed = low_mhz != self._frequency_low_mhz or high_mhz != self._frequency_high_mhz
        self._frequency_low_mhz = float(low_mhz)
        self._frequency_high_mhz = float(high_mhz)
        if span_changed:
            self.frequencySpanChanged.emit()
        if status != self._spectrum_status:
            self._spectrum_status = str(status)
            self.spectrumStatusChanged.emit()
        self._waterfall_generation += 1
        self.waterfallGenerationChanged.emit()

    def set_capture_status(self, status):
        status = str(status)
        if status != self._capture_status:
            self._capture_status = status
            self.captureStatusChanged.emit()

    @PyQt6.QtCore.pyqtSlot(dict)
    def _on_update_app_state(self, changed):
        visualization = changed.get("visualization", {}) if isinstance(changed, dict) else {}
        if isinstance(visualization, dict):
            if "space" in visualization:
                self.visualizationSpaceChanged.emit()
            if "azimuth_correction" in visualization:
                self.azimuthCorrectionChanged.emit()
            if "elevation_correction" in visualization:
                self.elevationCorrectionChanged.emit()
            if "waterfall" in visualization:
                self.waterfallVisibleChanged.emit()
        beamformer = changed.get("beamformer", {}) if isinstance(changed, dict) else {}
        if isinstance(beamformer, dict):
            if "resolution_azimuth" in beamformer:
                self.resolutionAzimuthChanged.emit()
            if "resolution_elevation" in beamformer:
                self.resolutionElevationChanged.emit()

    @PyQt6.QtCore.pyqtProperty(int, notify=resolutionAzimuthChanged)
    def resolutionAzimuth(self):
        return int(self.appconfig.get("beamformer", "resolution_azimuth"))

    @PyQt6.QtCore.pyqtProperty(int, notify=resolutionElevationChanged)
    def resolutionElevation(self):
        return int(self.appconfig.get("beamformer", "resolution_elevation"))

    @PyQt6.QtCore.pyqtProperty(str, notify=visualizationSpaceChanged)
    def visualizationSpace(self):
        return str(self.appconfig.get("visualization", "space"))

    @PyQt6.QtCore.pyqtProperty(float, notify=receiverPowerChanged)
    def receiverPower(self):
        return self._receiver_power

    @PyQt6.QtCore.pyqtProperty(str, notify=receiverPowerUnitChanged)
    def receiverPowerUnit(self):
        return "dBFS"

    @PyQt6.QtCore.pyqtProperty(float, notify=activeAntennasChanged)
    def activeAntennas(self):
        return self._active_antennas

    @PyQt6.QtCore.pyqtProperty(bool, notify=macListEnabledChanged)
    def macListEnabled(self):
        return False

    @PyQt6.QtCore.pyqtProperty(list, notify=macListChanged)
    def macList(self):
        return []

    @PyQt6.QtCore.pyqtProperty(bool, notify=polarizationVisibleChanged)
    def polarizationVisible(self):
        return False

    @PyQt6.QtCore.pyqtProperty(float, notify=gridSpacingChanged)
    def gridSpacing(self):
        return 24.0

    @PyQt6.QtCore.pyqtProperty(float, notify=azimuthCorrectionChanged)
    def azimuth_correction(self):
        return float(self.appconfig.get("visualization", "azimuth_correction"))

    @PyQt6.QtCore.pyqtProperty(float, notify=elevationCorrectionChanged)
    def elevation_correction(self):
        return float(self.appconfig.get("visualization", "elevation_correction"))

    @PyQt6.QtCore.pyqtProperty(int, notify=waterfallGenerationChanged)
    def waterfallGeneration(self):
        return self._waterfall_generation

    @PyQt6.QtCore.pyqtProperty(bool, notify=waterfallVisibleChanged)
    def waterfallVisible(self):
        return bool(self.appconfig.get("visualization", "waterfall"))

    @PyQt6.QtCore.pyqtProperty(str, notify=spectrumStatusChanged)
    def spectrumStatus(self):
        return self._spectrum_status

    @PyQt6.QtCore.pyqtProperty(str, notify=captureStatusChanged)
    def captureStatus(self):
        return self._capture_status

    @PyQt6.QtCore.pyqtProperty(float, notify=frequencySpanChanged)
    def frequencyLowMhz(self):
        return self._frequency_low_mhz

    @PyQt6.QtCore.pyqtProperty(float, notify=frequencySpanChanged)
    def frequencyHighMhz(self):
        return self._frequency_high_mhz
