"""Hardware setup for the ESPARGOS pulse radar, using the IQ TX demo API."""

import math
import threading

from espargos import Antenna
from espargos.board import EspargosHTTPStatusError
from espargos.calibration import CalibrationState
from espargos._iq_calibration.reference import _map
from espargos.iq_tx_control import _rpc, _waveform
from espargos.steering import Beamspace
from espargos.waveforms import IQSamples
from espargos.iq_tx_rx_delay import TxRxDelayMeasurementError


def air_path_delays(transmitters, receivers):
    """Establish range zero from direct coupling, neglecting propagation time."""
    return {(tx, rx): 0.0 for tx in transmitters for rx in receivers}


def calibrate_radar_timing(pool, transmitters, path, logger):
    """Use MIMO's timing probes, increasing gain for weak inter-board links."""
    kwargs = {"path": path}
    if path != "reference":
        transmitters = set(transmitters)
        kwargs["reference_delays_s"] = air_path_delays(transmitters, set(pool.antennas) - transmitters)
    for gain in (24, 42):
        try:
            return pool.calibrate_tx_rx_delay(**kwargs, rx_gain=gain)
        except TxRxDelayMeasurementError as error:
            if gain == 42 or error.reason not in ("weak", "unstable"):
                raise
            logger.info("TX/RX timing probe was %s at %s dB RX gain; retrying at 42 dB", error.reason, gain)


def configure_receivers(pool, config):
    """Apply the RX drawer to receiving sensors, preserving all TX settings.

    Pool.configure requires an exclusively RX selection for gain/sample rate,
    and whole boards for routing/rate. Mixed boards therefore use the native
    capture API, whose rate and RF switch configure the receive path only.
    """
    config = dict(config)
    if "mode" in config or "modes" in config:
        raise ValueError("Change radar sensor roles in the Transmission tab")
    settings = {key: config.pop(key) for key in ("rf_freq_hz", "gain_mode", "rx_gain", "expert_gain_words") if key in config}
    if settings and "receivers" in config:
        raise ValueError("Cannot combine receiver lists with array-wide RX settings")
    if isinstance(settings.get("gain_mode"), int):
        settings["gain_mode"] = ("auto", "manual", "expert")[settings["gain_mode"]]
    sensitive = {"adc_decimation", "adc_source_sel", "bw_mhz", "second_chan", "filter_bw_mhz", "loopback", "tx_tone_enable"}
    with pool._lock:
        plans, invalid = [], set()
        for board in pool.boards:
            old = board.iq.get_config()
            ids = [a for a, mode in enumerate(old["modes"]) if mode == "iq_rx"]
            if not ids:
                continue
            payload = dict(config)
            if settings:
                payload["receivers"] = [dict(antid=a, **settings) for a in ids]
            elif "receivers" in payload:
                payload["receivers"] = [r for r in payload["receivers"] if r["antid"] in ids]
            if any(key in payload and payload[key] != old.get(key) for key in sensitive):
                invalid.update(Antenna(board.host, a) for a in ids)
            for receiver in payload.get("receivers", []):
                a = receiver["antid"]
                if "rf_freq_hz" in receiver and receiver["rf_freq_hz"] != old["receivers"][a]["rf_freq_hz"]:
                    invalid.add(Antenna(board.host, a))
            plans.append((board, payload))
        if invalid:
            pool.calibrations = pool.calibrations.without(invalid)
            pool.iq._calibration = None
        for board, payload in plans:
            board.iq.set_config(payload)


class RadarTransmitter:
    # These are RF filter selections, independent of the 80 MSa/s DAC rate
    # and the digital chirp sweep. Match the transmit beamforming demo.
    RF_BANDWIDTHS = (0, 13, 20, 40, 60)

    def __init__(self, pool, args):
        self.pool, self.args = pool, args
        self.session = None
        selected = set()
        for token in args.tx_sensors:
            if token in pool.names:
                board, ids = pool.names.index(token), range(8)
            else:
                name, separator, number = token.rpartition(".")
                if not separator or name not in pool.names or not number.isdigit() or not 0 <= int(number) < 8:
                    raise ValueError(f"Unknown TX sensor {token!r}; use BOARD or BOARD.ID (0–7)")
                board, ids = pool.names.index(name), [int(number)]
            selected.update((board, a) for a in ids)
        if len(selected) == len(pool.names) * 8:
            raise ValueError("Keep at least one sensor in IQ RX mode")
        self.selected = selected
        self.antennas = [Antenna(pool.hosts[b], a) for b, a in sorted(selected)]

    def receivers(self, board):
        return tuple(a for a in range(8) if (board, a) not in self.selected)

    def configure(self):
        args = self.args
        if not math.isfinite(args.frequency_mhz) or not all(math.isfinite(x) for x in (args.tx_azimuth, args.tx_elevation)):
            raise ValueError("Frequency and steering angles must be finite")
        if not 2310 <= args.frequency_mhz <= 2640:
            raise ValueError("Frequency must be between 2310 and 2640 MHz")
        if args.tx_gain_a not in range(8) or args.tx_gain_b not in range(8):
            raise ValueError("Use RF gains A/B 0–7")
        if args.tx_bandwidth not in self.RF_BANDWIDTHS:
            raise ValueError(f"Unsupported TX RF filter {args.tx_bandwidth} MHz. " "Choose Open (0), 13, 20, 40, or 60 MHz; " "80 MSa/s is the replay rate, not an RF filter selection.")
        if args.tx_output not in ("antenna_l", "antenna_r", "reference", "isolated"):
            raise ValueError("Unknown TX output")
        if args.tx_calibration not in ("monitor", "reciprocity") or args.calibration_path not in ("reference", "antenna_l", "antenna_r"):
            raise ValueError("Unknown calibration method or path")
        if args.tx_calibration == "monitor" and args.calibration_path != "reference":
            raise ValueError("Monitor calibration requires the reference path")
        center = round(args.frequency_mhz * 1e6)
        configs = _map(lambda b: b.iq.get_config(), self.pool.boards)
        # Avoid re-firing RX/TX on Start after a calibration. In particular,
        # rewriting every sensor through RX destroys the measured TX origins.
        for board, config in zip(self.pool.boards, configs):
            receivers = [dict(antid=a, rf_freq_hz=center) for a, r in enumerate(config["receivers"]) if r["rf_freq_hz"] != center]
            update = {"receivers": receivers} if receivers else {}
            if "wifi" in config["modes"]:
                update["modes"] = ["iq_rx"] * 8
            if update:
                board.iq.set_config(update)
        if self.antennas:
            self.session = self.pool.tx
            if not all(self.session.authorization):
                raise PermissionError("Enable TX authorization in each connected board's web Settings")
            modes = [["iq_tx" if (b, a) in self.selected else "iq_rx" for a in range(8)] for b in range(len(self.pool.names))]
            if any(c["modes"] != m for c, m in zip(configs, modes)):
                self.session.modes(modes)
            rf = dict(
                frequency_hz=round(args.frequency_mhz * 1e6),
                rf_gain=dict(a=args.tx_gain_a, b=args.tx_gain_b),
                bandwidth_mhz=args.tx_bandwidth,
                output=args.tx_output,
                amplitude=1.0,
                steering=dict(phase_deg=[0] * 8, delay_samples=[0] * 8),
            )
            try:
                states = self.session.read()

                def apply(pair):
                    bi, board = pair
                    changes = []
                    for b, a in sorted(self.selected):
                        if b != bi:
                            continue
                        sensor = states[bi]["sensors"][a]
                        changed = {k: v for k, v in rf.items() if k != "steering" and sensor.get(k) != v}
                        if changed:
                            changes.append(dict(antid=a, **changed))
                    payload = {"transmitters": changes} if changes else {}
                    if states[bi]["steering"] != rf["steering"]:
                        payload["steering"] = rf["steering"]
                    if payload:
                        _rpc(board, "set_iq_tx", payload)

                _map(apply, enumerate(self.pool.boards))
                self.session.read()
            except EspargosHTTPStatusError as error:
                raise RuntimeError(f"Applying TX RF settings failed: frequency={args.frequency_mhz} MHz, " f"RF filter={args.tx_bandwidth} MHz, gains A/B={args.tx_gain_a}/{args.tx_gain_b}, " f"output={args.tx_output}. {error}") from error
        else:
            for board, config in zip(self.pool.boards, configs):
                if config["modes"] != ["iq_rx"] * 8:
                    board.iq.set_config({"modes": ["iq_rx"] * 8})

    def prepare(self, waveform, gain_db):
        """Prepare a muted replay without measuring or invalidating its timing."""
        if self.session is None:
            raise ValueError("Choose transmit sensors in the Transmission tab")
        amplitude = 10 ** (gain_db / 20)
        if not math.isfinite(amplitude) or not 0 < amplitude <= 1.0:
            raise ValueError("TX digital gain must be at most 0 dB (amplitude ≤ 1)")
        source = IQSamples(waveform)
        try:
            self.session.mute(True)
            with self.session.update(samples=256):
                self.session.signals.clear()
                self.session.signals.add(
                    waveform=source,
                    amplitude=amplitude,
                    antennas=self.antennas,
                    steering=Beamspace(azimuth_deg=self.args.tx_azimuth, elevation_deg=self.args.tx_elevation, squint=self.args.tx_squint),
                )
        except Exception:
            self.session.mute(True)
            raise
        # One common quantized chirp; never use the per-sensor steering or
        # calibration coefficients to correlate the receive channels.
        return source.synthesize(256)

    def start(self, waveform, gain_db):
        """Start immediately; the central Sync + Calibration action measures TX/RX."""
        reference = self.prepare(waveform, gain_db)
        self.session.mute(False)
        return reference

    def prepare_calibration_helpers(self, timeout=10):
        """Give RX-only pilot helpers a known silent source before calibration.

        Controller reboot loses its saved waveform recipes while sensors can
        retain dormant replay buffers. Such an unknown buffer cannot be resized
        for a temporary pilot. The demo owns these inactive sources and prepares
        silence explicitly in RX; selected TX waveforms remain untouched.
        """
        if self.args.tx_calibration != "reciprocity" or len(self.pool.boards) < 2 or len(self.selected) < 2:
            return

        def prepare(pair):
            bi, board = pair
            antennas = self.receivers(bi)
            if not antennas:
                return
            saved_trigger = board.iq.get_config()["trigger"]
            # Accumulation emits different packets, and Signal can remain
            # quiet indefinitely. Calibration readiness must not depend on
            # the user's capture schedule.
            generation = None
            seen, ready = set(), threading.Event()

            def receive(message):
                if message.antenna_id in antennas and message.payload.config_generation == generation:
                    seen.add(message.antenna_id)
                    if len(seen) == len(antennas):
                        ready.set()

            subscription = board.iq.subscribe_chunks(receive)
            try:
                board.iq.set_config({"trigger": dict(mode="interval", interval_chunks=8192, burst_chunks=1)})
                generation = board.iq.get_config()["config_generation"]
                if not ready.wait(timeout):
                    missing = sorted(set(antennas) - seen)
                    raise TimeoutError(f"{board.host}: RX helpers {missing} did not supply current-generation " f"interval chunks (generation {generation})")
                _waveform(board, dict(type="cw", frequency_hz=0, samples=1024, amplitude=0, antennas=list(antennas)), timeout, wait=self.session.capture.wait)
            finally:
                try:
                    board.iq.unsubscribe(subscription)
                finally:
                    board.iq.set_config({"trigger": saved_trigger})

        _map(prepare, enumerate(self.pool.boards))

    def calibration_valid(self):
        """Check the actual replay origins against the central calibration."""
        states = self.session.read() if self.session is not None else []
        try:
            selected = [CalibrationState.from_tx_status(self.pool.hosts[bi], states[bi]["sensors"][a]) for bi, a in sorted(self.selected)]
        except ValueError:
            return False
        return bool(selected and self.pool.calibrations.covers(selected) and self.pool.iq.calibration is not None)

    def stop(self):
        if self.session is not None:
            self.session.mute(True)
