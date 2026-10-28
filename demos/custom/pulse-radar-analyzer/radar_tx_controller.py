"""Reuse the IQ demo editor while the radar owns hardware and calibration."""
import copy
from types import SimpleNamespace
from pathlib import Path
from demos.custom.common.iq_tx_waveform import load_iq
from radar_waveform import beam_source, prepare_source
from espargos.waveforms import IQSamples
import numpy as np
from PyQt6.QtCore import pyqtSlot
from demos.custom.common.iq_tx_controller import IQTxController


class RadarTxController(IQTxController):
    RF_KEYS = dict(frequency="frequency_mhz", gain_a="tx_gain_a", gain_b="tx_gain_b",
                   bandwidth="tx_bandwidth", output="tx_output",
                   calibration_method="tx_calibration", calibration_path="calibration_path")

    def __init__(self, radar):
        super().__init__(SimpleNamespace(hosts=[], names=[]), parent=radar)
        self.radar = radar
        self.data.update(authorized=True, connected=True, samples=256, unlocked=True)
        for beam in self.data["beams"]:
            beam.update(type="lfm", start_mhz=-20, stop_mhz=20, duration_us=0.2, delay_samples=0, duration_samples=16, amplitude=10**(radar._tx_settings["tx_gain_db"]/20))
        az, el = np.radians([radar._tx_settings["tx_azimuth"], radar._tx_settings["tx_elevation"]])
        self.data["beams"][0].update(u=float(np.sin(az)*np.cos(el)), v=float(np.sin(el)), squint=radar._tx_settings["tx_squint"])
        radar._tx_settings["beams"] = copy.deepcopy(self.data["beams"][:1])
        self.data["monitor_enabled"] = False
        self.poll.stop()
        radar.txStateChanged.connect(self.refresh_radar)
        self.refresh_radar()

    def refresh_radar(self):
        for key, target in self.RF_KEYS.items():
            self.data[key] = self.radar._tx_settings[target]
        config = self.radar._tx_settings
        self.data["waveform_editor"] = config["waveform_editor"]
        rate = config.get("fs_hz", 80e6)
        self.data["fs_hz"] = rate
        self.data["sample_counts"] = [n for n in (256, 512, 1024, 2048, 4096, 8192) if n * 80e6 / rate <= 8192]
        period = 256 // int(config["pings_per_256"])
        self.data["pulse_period"] = period
        for beam in self.data["beams"]:
            beam["delay_samples"] = min(int(beam.get("delay_samples", 0)), period-1)
            beam.update(start_mhz=-config["bandwidth_hz"]/2e6, stop_mhz=config["bandwidth_hz"]/2e6,
                        duration_samples=min(int(config["chirp_frames"]), period-int(beam.get("delay_samples", 0))),
                        duration_us=int(config["chirp_frames"])/(rate/1e6),
                        delay_us=int(beam.get("delay_samples", 0))/(rate/1e6))
        self.radar._tx_settings["beams"] = copy.deepcopy(self.data["beams"][:self.data["count"]])
        self.data.update(playing=self.radar.txActive, exclusive=(self.radar.iqcontrol is not None and self.radar.iqcontrol.referenceBusy and not self.radar._tx_busy),
                         message=self.radar.txStatus)
        self.changed.emit()
        if self.pending and not self.timer.isActive():
            self.timer.start()

    @pyqtSlot(int, str, "QVariant")
    def setBeam(self, index, key, value):
        if self.data["exclusive"] or not 0 <= index < self.data["count"]:
            return
        if key == "duration_samples":
            self.radar.setTxSetting("chirp_frames", max(1, min(int(value), self.data["pulse_period"])))
            return
        if key == "delay_samples":
            period = 256 // int(self.radar._tx_settings["pings_per_256"])
            self.data["beams"][index][key] = min(max(0, int(value)), period-1)
            self.refresh_radar()
            self._queue("waveform")
            return
        super().setBeam(index, key, value)
        if key == "amplitude":
            # Legacy digital gain is an alias for beam 1, never a second multiplier.
            if index == 0:
                self.radar._tx_settings["tx_gain_db"] = float(20*np.log10(max(self.data["beams"][0]["amplitude"], 1e-6)))
            self.radar._tx_settings["beams"] = copy.deepcopy(self.data["beams"][:self.data["count"]])

    def preview_signature(self, index):
        config = self.radar._tx_settings
        return tuple(config[k] for k in ("chirp_frames", "bandwidth_hz", "pings_per_256", "waveform_editor")) + (config.get("fs_hz", 80e6), self.data["beams"][index]["delay_samples"], self.data["count"])

    @pyqtSlot(str, "QVariant")
    def setSetting(self, key, value):
        if key == "samples" and value not in self.data["sample_counts"]:
            return
        super().setSetting(key, value)

    def preview_samples(self, index):
        beam = self.data["beams"][index]
        config = self.radar._tx_settings
        # Files are loaded by the worker; the plot never reads them on the Qt thread.
        if config["waveform_editor"] and beam["type"] == "file":
            cached = self.file_previews.get(index)
            z = cached[1] if cached and cached[0][:2] == (beam["file"], self.data["samples"]) else np.zeros(self.data["samples"], complex)
        else:
            source = beam_source(beam, config, self.data["samples"])
            _, z = prepare_source(source, config, self.data["samples"])
        if config["waveform_editor"] and beam["type"] == "file":
            _, z = prepare_source(IQSamples(z), config, self.data["samples"])
        return z * beam["amplitude"] / self.data["count"]

    def _queue(self, *kinds):
        # The radar worker serializes RF/replay updates with RX calibration.
        self.pending.update(kinds)
        if not self.timer.isActive():
            self.timer.start()

    @pyqtSlot()
    def flush(self):
        if self.radar.txBusy or self.data["busy"] or self.closing:
            self.timer.start()
            return
        kinds, self.pending = self.pending, set()
        for key, target in self.RF_KEYS.items():
            self.radar._tx_settings[target] = self.data[key]
        self.radar._tx_settings.update(beams=copy.deepcopy(self.data["beams"][:self.data["count"]]), samples=self.data["samples"])
        first = self.data["beams"][0]
        self.radar._tx_settings["tx_gain_db"] = float(20*np.log10(max(first["amplitude"], 1e-6)))
        u, v = first["u"], first["v"]
        self.radar._tx_settings.update(tx_azimuth=float(np.degrees(np.arctan2(u, np.sqrt(max(0, 1-u*u-v*v))))),
                                       tx_elevation=float(np.degrees(np.arcsin(np.clip(v, -1, 1)))), tx_squint=first["squint"])
        files = [(i, b["file"]) for i, b in enumerate(self.data["beams"][:self.data["count"]]) if b["type"] == "file" and b["file"]]
        if files and "waveform" in kinds:
            samples = self.data["samples"]
            def previews():
                result = {}
                for index, filename in files:
                    stat = Path(filename).stat()
                    key = (filename, samples, stat.st_mtime_ns, stat.st_size)
                    cached = self.file_previews.get(index)
                    if cached is None or cached[0] != key:
                        words = load_iq(filename, samples).astype(np.int32)
                        i, q = words & 1023, (words >> 10) & 1023
                        result[index] = (key, (np.where(i >= 512, i-1024, i) + 1j*np.where(q >= 512, q-1024, q))/511)
                return result
            def finished(result):
                self.file_previews.update(result)
                self.radar._queue_live_tx()
            self._run(previews, finished)
            return
        self.radar._queue_live_tx()

    def _done(self, callback, result, error):
        super()._done(callback, result, error)
        if error:
            # A file error must not disable the radar's beam editor.
            self.data.update(authorized=True, connected=True)
            self.radar._tx_status = str(error)
            self.radar.txStateChanged.emit()

    @pyqtSlot()
    def toggleTx(self):
        self.radar.stopTx() if self.radar.txActive else self.radar.startTx()

    @pyqtSlot()
    def calibrate(self):
        if self.radar.iqcontrol is not None:
            self.radar.iqcontrol.sync_async()
