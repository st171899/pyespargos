"""Hardware-free mixed-role TX and receive-phase regression checks."""

import importlib.util
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
import time
from unittest.mock import Mock, patch

import numpy as np

DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
from radar_tx import RadarTransmitter, configure_receivers

spec = importlib.util.spec_from_file_location("espargi_radar", DEMO / "pulse-radar-analyzer-espargi.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Analyzer = module.EspargosPulseRadarAnalyzer


class RadarTXTest(unittest.TestCase):
    def test_reference_timing_retries_weak_or_unstable_probes_at_higher_gain(self):
        from radar_tx import calibrate_radar_timing
        from espargos.iq_tx_rx_delay import TxRxDelayMeasurementError

        for reason in ("weak", "unstable"):
            pool, logger = Mock(), Mock()
            expected = object()
            pool.calibrate_tx_rx_delay.side_effect = [TxRxDelayMeasurementError("weak timing", reason=reason), expected]
            self.assertIs(calibrate_radar_timing(pool, [], "reference", logger), expected)
            self.assertEqual(pool.calibrate_tx_rx_delay.call_args_list, [unittest.mock.call(path="reference", rx_gain=24), unittest.mock.call(path="reference", rx_gain=42)])
            logger.info.assert_called_once()

    def test_timing_gain_recovery_preserves_quality_and_clipping_failures(self):
        from radar_tx import calibrate_radar_timing
        from espargos.iq_tx_rx_delay import TxRxDelayMeasurementError

        pool = Mock()
        pool.calibrate_tx_rx_delay.side_effect = TxRxDelayMeasurementError("still weak", reason="weak")
        with self.assertRaises(TxRxDelayMeasurementError):
            calibrate_radar_timing(pool, [], "reference", Mock())
        self.assertEqual(pool.calibrate_tx_rx_delay.call_count, 2)
        for error in (TxRxDelayMeasurementError("clipped", reason="clipped"), ValueError("invalid calibration group")):
            pool = Mock()
            pool.calibrate_tx_rx_delay.side_effect = error
            with self.assertRaises(type(error)):
                calibrate_radar_timing(pool, [], "reference", Mock())
            pool.calibrate_tx_rx_delay.assert_called_once_with(path="reference", rx_gain=24)

    def test_optional_antenna_timing_still_uses_zero_propagation(self):
        from radar_tx import calibrate_radar_timing

        pool = Mock(antennas=["tx", "rx"])
        calibrate_radar_timing(pool, ["tx"], "antenna_r", Mock())
        pool.calibrate_tx_rx_delay.assert_called_once_with(path="antenna_r", reference_delays_s={("tx", "rx"): 0.0}, rx_gain=24)

    def test_air_timing_needs_no_positions_or_distances(self):
        from radar_tx import air_path_delays

        self.assertEqual(air_path_delays(["tx0", "tx1"], ["rx0", "rx1"]), {(t, r): 0.0 for t in ("tx0", "tx1") for r in ("rx0", "rx1")})

    def test_meter_cutoffs_snap_to_samples_and_preserve_a_valid_interval(self):
        for rate in (80e6, 20e6):
            config = dict(delay_min=0, delay_max=256)
            backend = SimpleNamespace(
                radarZeroSample=10.25, radarMetersPerSample=299792458 / (2 * rate),
                _radar_delay_bounds=lambda: (config["delay_min"], config["delay_max"]),
                appconfig=SimpleNamespace(set=config.update), radarConfigChanged=Mock(),
            )
            def enter(sample, upper):
                meters = (sample - backend.radarZeroSample) * backend.radarMetersPerSample
                self.assertTrue(Analyzer.setRadarRangeMeters(backend, meters, upper))

            enter(20.4, False)
            enter(90.6, True)
            self.assertEqual(config, dict(delay_min=20, delay_max=91))
            enter(200, False)
            self.assertEqual(config["delay_min"], 90)
            enter(-100, True)
            self.assertEqual(config["delay_max"], 91)
            enter(-100, False)
            enter(300, True)
            self.assertEqual(config, dict(delay_min=0, delay_max=256))
            self.assertFalse(Analyzer.setRadarRangeMeters(backend, float("nan"), False))
            backend.radarZeroSample = float("nan")
            self.assertFalse(Analyzer.setRadarRangeMeters(backend, 10, True))
            self.assertEqual(config, dict(delay_min=0, delay_max=256))

    def test_range_zero_includes_tx_correction_and_receive_rate(self):
        # Two transmitters straddle replay wrap before their TX corrections.
        corrections = [SimpleNamespace(state=SimpleNamespace(antenna=a), delay_s=d / 80e6) for a, d in ((0, 12), (1, -8))]
        timing = SimpleNamespace(delays_s={0: 254 / 80e6, 1: 18 / 80e6}, calibration=SimpleNamespace(select=lambda **kw: SimpleNamespace(groups=[SimpleNamespace(corrections=corrections)])))
        backend = SimpleNamespace(_radar_timing=timing, _tx_calibration_valid=True, iqcontrol=SimpleNamespace(syncNeeded=False), _spectrum_sample_rate_hz=80e6)
        self.assertAlmostEqual(Analyzer.radarZeroSample.fget(backend), 10)
        backend._spectrum_sample_rate_hz = 20e6
        self.assertAlmostEqual(Analyzer.radarZeroSample.fget(backend), 2.5)
        self.assertAlmostEqual(Analyzer.radarMetersPerSample.fget(backend), 299792458 / 40e6)
        backend.iqcontrol.syncNeeded = True
        self.assertTrue(np.isnan(Analyzer.radarZeroSample.fget(backend)))
        backend.iqcontrol.syncNeeded = False
        backend._tx_calibration_valid = False
        self.assertTrue(np.isnan(Analyzer.radarZeroSample.fget(backend)))

    def test_colorbar_ticks_follow_selected_normalization(self):
        backend = SimpleNamespace(radarMinDb=-20, radarMaxDb=0, radarLinearScale=False)
        self.assertEqual(Analyzer.radarColorbarLabel(backend, 0.5), "-10")
        backend.radarLinearScale = True
        self.assertEqual(Analyzer.radarColorbarLabel(backend, 0), "0.01")
        self.assertEqual(Analyzer.radarColorbarLabel(backend, 0.5), "0.51")
        self.assertEqual(Analyzer.radarColorbarLabel(backend, 1), "1")

    def test_heatmap_color_scale_and_observation_crop(self):
        samples = np.ones((8, 256), np.complex64)
        config = dict(delay_min=8, delay_max=24, radar_min_db=-20, radar_max_db=0)
        backend = SimpleNamespace(
            _tx_waveform=None, _reference_ping_config=lambda: dict(pings_per_256=2),
            _angle_corrected_traces=lambda traces: (samples, True),
            _angle_geometry=lambda: np.arange(8).reshape(2, 4),
            appconfig=SimpleNamespace(get=config.get), _radar_angle_size=256, _radar_delay_size=256,
            radarConfigChanged=Mock(), radarStatusChanged=Mock(), radarMapChanged=Mock(),
        )
        # A return at -10 dBFS is halfway up the dB scale but about 9% up
        # the linear scale, with identical manual -20..0 dBFS endpoints.
        power = np.full((256, 16), 0.1 * 32 * module.ADC_FULL_SCALE**2)
        for scale, expected in (("db", 0.5), ("linear", 0.09 / 0.99)):
            config["radar_scale"] = scale
            with patch.object(module, "horizontal_power", return_value=power) as beamform:
                Analyzer._render_radar_heatmap(backend, {0: samples[0]})
            self.assertEqual(beamform.call_args.args[0].shape, (2, 4, 16))
            self.assertEqual(backend._radar_delay_size, 16)
            rgba = np.asarray(backend.radarMapChanged.emit.call_args.args[0]).reshape(16, 256, 4)
            expected_color = (np.asarray(module.matplotlib.colormaps["viridis"](expected)) * 255).astype(np.uint8)
            np.testing.assert_array_equal(rgba[0, 0], expected_color)

    def setup_tx(self, sensors):
        args = SimpleNamespace(tx_sensors=sensors, frequency_mhz=2380, tx_gain_a=0, tx_gain_b=0, tx_bandwidth=40, tx_output="antenna_r", tx_azimuth=25, tx_elevation=10, tx_squint=True, tx_calibration="reciprocity", calibration_path="reference", timing_path="reference")
        pool = Mock(names=["espargosrx", "espargostx"], hosts=["10.42.0.35", "10.42.0.119"])
        pool._lock = nullcontext()
        pool.antennas = tuple(module.espargos.Antenna(host, a) for host in pool.hosts for a in range(8))
        pool.tx.authorization = [True, True]
        pool.tx.update.side_effect = lambda **kw: nullcontext()
        pool.boards = [Mock(), Mock()]
        for board in pool.boards:
            board.iq.get_config.return_value = dict(modes=["iq_rx"] * 8, receivers=[dict(rf_freq_hz=2380e6) for _ in range(8)], adc_decimation=1)
            board.control.post_json.return_value = "ok"
        pool.tx.read.return_value = [dict(steering=dict(phase_deg=[0] * 8, delay_samples=[0] * 8), sensors=[dict(frequency_hz=2380e6, rf_gain=dict(a=0, b=0), bandwidth_mhz=40, output="antenna_r", amplitude=1) for _ in range(8)]) for _ in range(2)]
        return RadarTransmitter(pool, args), pool

    def test_receive_drawer_preserves_tx_on_mixed_and_tx_only_boards(self):
        tx, pool = self.setup_tx(["espargostx", "espargosrx.3"])
        pool._lock = nullcontext()
        pool.antennas = tuple(module.espargos.Antenna(host, a) for host in pool.hosts for a in range(8))
        pool.boards[0].host, pool.boards[1].host = pool.hosts
        pool.boards[0].iq.get_config.return_value["modes"][3] = "iq_tx"
        pool.boards[1].iq.get_config.return_value["modes"] = ["iq_tx"] * 8
        calibrations = pool.calibrations
        configure_receivers(pool, dict(rx_gain=30, gain_mode=1, adc_decimation=2, rf_switch=2))
        payload = pool.boards[0].iq.set_config.call_args.args[0]
        self.assertEqual([r["antid"] for r in payload["receivers"]], [0, 1, 2, 4, 5, 6, 7])
        self.assertTrue(all(r["gain_mode"] == "manual" for r in payload["receivers"]))
        self.assertEqual(payload["adc_decimation"], 2)
        self.assertNotIn("modes", payload)
        pool.boards[1].iq.set_config.assert_not_called()
        invalid = calibrations.without.call_args.args[0]
        self.assertEqual({a.antid for a in invalid}, {0, 1, 2, 4, 5, 6, 7})
        self.assertTrue(all(a.host == pool.hosts[0] for a in invalid))
        self.assertIsNone(pool.iq._calibration)

    def test_selection_and_mixed_modes(self):
        tx, pool = self.setup_tx(["espargostx", "espargosrx.3"])
        tx.configure()
        self.assertEqual(tx.receivers(0), (0, 1, 2, 4, 5, 6, 7))
        self.assertEqual(tx.receivers(1), ())
        modes = pool.tx.modes.call_args.args[0]
        self.assertEqual(modes[0][3], "iq_tx")
        self.assertEqual(modes[1], ["iq_tx"] * 8)
        self.assertEqual(len(tx.antennas), 9)

    def test_invalid_selections(self):
        for sensors in (["missing"], ["espargostx.8"], ["espargosrx", "espargostx"]):
            with self.subTest(sensors=sensors), self.assertRaises(ValueError):
                self.setup_tx(sensors)

    def test_reference_excludes_steering_and_preserves_received_phase(self):
        tx, pool = self.setup_tx(["espargostx"])
        tx.configure()
        cfg = dict(chirp_frames=16, bandwidth_hz=40e6, pings_per_256=2)
        waveform = Analyzer._build_ping_waveform(None, cfg)
        reference = tx.start(waveform, -20)
        settings = pool.tx.signals.add.call_args.kwargs
        self.assertEqual(settings["steering"].azimuth_deg, 25)
        np.testing.assert_array_equal(reference, settings["waveform"].synthesize(256))
        pool.calibrate.assert_not_called()
        self.assertEqual(pool.tx.mute.call_args.args, (False,))
        backend = SimpleNamespace(_tx_waveform=reference, _reference_ping_config=lambda: cfg)
        received = np.roll(reference, 12)
        a = Analyzer._angle_matched_trace(backend, received)
        b = Analyzer._angle_matched_trace(backend, received * np.exp(0.7j))
        np.testing.assert_allclose(b, a * np.exp(0.7j), atol=2e-6)
        self.assertEqual(np.argmax(np.abs(a)), 12)
        tx.stop()
        self.assertEqual(pool.tx.mute.call_args.args, (True,))

    def test_failed_waveform_update_mutes(self):
        tx, pool = self.setup_tx(["espargostx.0"])
        tx.configure()
        pool.tx.signals.add.side_effect = RuntimeError("waveform failed")
        with self.assertRaises(RuntimeError):
            tx.start(np.ones(256, complex), -20)
        self.assertEqual(pool.tx.mute.call_args.args, (True,))

    def test_prepare_keeps_tx_muted_without_calibrating(self):
        tx, pool = self.setup_tx(["espargostx"])
        tx.configure()
        tx.prepare(np.ones(256, complex), -20)
        pool.calibrate.assert_not_called()
        self.assertEqual(pool.tx.mute.call_args.args, (True,))

    def test_calibration_prepares_only_rx_helpers_with_silent_known_sources(self):
        tx, pool = self.setup_tx(["espargostx"])
        tx.configure()
        callbacks = []
        for board in pool.boards:
            board.iq.get_config.return_value["config_generation"] = 4
            board.iq.get_config.return_value["trigger"] = dict(mode="accumulate", vector_chunks=1)

            def subscribe(callback):
                callbacks.append(callback)
                return 9

            board.iq.subscribe_chunks.side_effect = subscribe

        def wait(timeout):
            for callback in callbacks:
                for a in range(8):
                    callback(SimpleNamespace(antenna_id=a, payload=SimpleNamespace(config_generation=4)))
            return True

        event = Mock()
        event.wait.side_effect = wait
        with patch("radar_tx._waveform") as waveform, patch("radar_tx.threading.Event", return_value=event), patch("radar_tx._map", side_effect=lambda f, values: list(map(f, values))):
            tx.prepare_calibration_helpers()
        waveform.assert_called_once()
        self.assertIs(waveform.call_args.args[0], pool.boards[0])
        recipe = waveform.call_args.args[1]
        self.assertEqual(recipe["amplitude"], 0)
        self.assertEqual(recipe["samples"], 1024)
        self.assertEqual(recipe["antennas"], list(range(8)))
        pool.boards[0].iq.unsubscribe.assert_called_once_with(9)
        self.assertEqual(pool.boards[0].iq.set_config.call_args.args[0], {"trigger": dict(mode="accumulate", vector_chunks=1)})

    def test_failed_helper_preparation_restores_trigger_and_unsubscribes(self):
        tx, pool = self.setup_tx(["espargostx"])
        for board in pool.boards:
            board.iq.get_config.return_value.update(config_generation=7, trigger=dict(mode="signal"))
        with self.assertRaisesRegex(TimeoutError, "current-generation interval chunks"):
            tx.prepare_calibration_helpers(timeout=0)
        pool.boards[0].iq.unsubscribe.assert_called_once()
        self.assertEqual(pool.boards[0].iq.set_config.call_args.args[0], {"trigger": dict(mode="signal")})

    def test_unchanged_configuration_does_not_rewrite_rx_or_roles(self):
        tx, pool = self.setup_tx(["espargostx"])
        pool.boards[1].iq.get_config.return_value["modes"] = ["iq_tx"] * 8
        tx.configure()
        pool.configure.assert_not_called()
        pool.tx.modes.assert_not_called()
        for board in pool.boards:
            board.iq.set_config.assert_not_called()

    def test_angle_frame_waits_only_for_receivers(self):
        rx = (1, 2, 3, 4, 5, 6)
        traces = {a: {0: np.ones(256, complex)} for a in rx}
        backend = SimpleNamespace(block_acc={7: traces}, block_time={7: 0}, _rx_sensor_ids=lambda: rx, _publish_sample_windows=Mock(), rows_rendered=[0] * 8)
        Analyzer._render_sample_blocks(backend, [7], 1, "angle")
        self.assertEqual(set(backend._publish_sample_windows.call_args.args[0]), set(rx))
        self.assertEqual(backend.block_acc, {})

    def test_central_button_calibrates_selected_tx_and_rx_while_stopped(self):
        from espargos.calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState

        tx, pool = self.setup_tx(["espargostx"])
        settings = dict(vars(tx.args), chirp_frames=16, bandwidth_hz=40e6, pings_per_256=2, tx_gain_db=-20)
        for active in (False, True):
            pool.antennas = tuple(Antenna(host, a) for host in pool.hosts for a in range(8))
            # An earlier all-RX fit spans both boards; the second board is
            # now TX. Replacing only the current RX keys used to fail here.
            pool.calibrations = Calibration((CalibrationGroup(tuple(AntennaCorrection(CalibrationState(a, "rx", 2400e6, 80e6, 1, 100), 0, 0) for a in pool.antennas)),))
            keys = [(a, "rx" if a.host == pool.hosts[0] else "tx", 2400e6) for a in pool.antennas]
            with self.assertRaisesRegex(ValueError, "complete calibration group"):
                pool.calibrations._check_replacement(keys)
            pool.calibrate.side_effect = lambda **kw: pool.calibrations._check_replacement(keys)
            backend = SimpleNamespace(
                txActive=active,
                pool=pool,
                _tx_settings=settings,
                PING_TX_DEFAULTS=Analyzer.PING_TX_DEFAULTS,
                _stop_ping_tx=Mock(),
                _build_ping_waveform=Mock(return_value=np.ones(256, complex)),
                _tx_setup=Analyzer._tx_setup,
                radarCalibrationDone=Mock(),
                logger=Mock(),
            )
            candidate = Mock(antennas=tx.antennas)
            with patch.object(module, "RadarTransmitter", return_value=candidate):
                Analyzer._synchronize_radar(backend)
            pool.calibrate.assert_called_with(tx_method="reciprocity", path="reference", per_board=False)
            pool.calibrate_tx_rx_delay.assert_called_with(path="reference", rx_gain=24)
            self.assertIs(backend._radar_timing, pool.calibrate_tx_rx_delay.return_value)
            self.assertEqual(pool.calibrations.groups, ())
            self.assertIsNone(pool.iq._calibration)
            candidate.prepare.assert_called_once()
            candidate.start.assert_not_called()
            candidate.prepare_calibration_helpers.assert_called_once()
            self.assertEqual(candidate.session.mute.call_count, int(active))
            self.assertTrue(backend._tx_calibration_valid)

    def test_extended_hardware_settings_are_available(self):
        tx, pool = self.setup_tx(["espargostx"])
        tx.args.tx_gain_a, tx.args.tx_gain_b = 7, 7
        for bandwidth in (0, 13, 20, 40, 60):
            tx.args.tx_bandwidth = bandwidth
            tx.configure()
        tx.start(np.ones(256, complex), 0)
        self.assertEqual(pool.tx.signals.add.call_args.kwargs["amplitude"], 1)
        payload = pool.boards[1].control.post_json.call_args.args[1]
        self.assertEqual(payload["transmitters"][0]["rf_gain"], dict(a=7, b=7))

    def test_central_button_reacquires_rejected_pilots_before_publishing(self):
        tx, pool = self.setup_tx(["espargostx"])
        settings = dict(vars(tx.args), chirp_frames=16, bandwidth_hz=40e6, pings_per_256=2, tx_gain_db=-20)
        backend = SimpleNamespace(
            txActive=False,
            pool=pool,
            _tx_settings=settings,
            PING_TX_DEFAULTS=Analyzer.PING_TX_DEFAULTS,
            _stop_ping_tx=Mock(),
            _build_ping_waveform=Mock(return_value=np.ones(256, complex)),
            _tx_setup=Analyzer._tx_setup,
            radarCalibrationDone=Mock(),
            logger=Mock(),
        )
        pool.calibrate.side_effect = [module.CalibrationMeasurementError("Independent validation failed"), None]
        candidate = Mock(antennas=tx.antennas)
        with patch.object(module, "RadarTransmitter", return_value=candidate):
            Analyzer._synchronize_radar(backend)
        self.assertEqual(candidate.prepare.call_count, 2)
        self.assertTrue(backend._tx_calibration_valid)

    def test_failed_central_calibration_cannot_publish_valid_state(self):
        tx, pool = self.setup_tx(["espargostx"])
        settings = dict(vars(tx.args), chirp_frames=16, bandwidth_hz=40e6, pings_per_256=2, tx_gain_db=-20)
        backend = SimpleNamespace(
            txActive=False,
            pool=pool,
            _tx_settings=settings,
            PING_TX_DEFAULTS=Analyzer.PING_TX_DEFAULTS,
            _stop_ping_tx=Mock(),
            _build_ping_waveform=Mock(return_value=np.ones(256, complex)),
            _tx_setup=Analyzer._tx_setup,
            radarCalibrationDone=Mock(),
            logger=Mock(),
        )
        pool.calibrate.side_effect = module.DisconnectedLinks([[0], [1]])
        with patch.object(module, "RadarTransmitter", return_value=Mock(antennas=tx.antennas)):
            with self.assertRaises(module.DisconnectedLinks):
                Analyzer._synchronize_radar(backend)
        self.assertIsNone(backend._calibrated_tx_setup)
        self.assertFalse(backend._tx_calibration_valid)
        self.assertEqual(pool.calibrate.call_count, 3)

    def test_unsupported_rf_filter_is_rejected_before_hardware_changes(self):
        tx, pool = self.setup_tx(["espargostx"])
        tx.args.tx_bandwidth = 80
        with self.assertRaisesRegex(ValueError, "Unsupported TX RF filter 80"):
            tx.configure()
        pool.configure.assert_not_called()
        pool.tx.modes.assert_not_called()
        pool.tx.settings.assert_not_called()
        waveform = Analyzer._build_ping_waveform(None, dict(chirp_frames=16, bandwidth_hz=80e6, pings_per_256=2))
        self.assertEqual(waveform.size, 256)

    def test_rf_rejection_reports_selected_settings(self):
        tx, pool = self.setup_tx(["espargostx"])
        tx.args.tx_gain_a = 1
        pool.boards[1].control.post_json.side_effect = module.espargos.board.EspargosHTTPStatusError(400, "set_iq_tx", "invalid TX settings")
        with self.assertRaisesRegex(RuntimeError, "RF filter=40 MHz, gains A/B=1/0, output=antenna_r"):
            tx.configure()

    def test_phase_waterfall_corrects_its_reference_channel(self):
        phase = 0.6 + 0.2 * np.arange(8)
        tone = np.exp(2j * np.pi * 7 * np.arange(256) / 256)
        backend = SimpleNamespace(
            fft_size=256,
            _display_config={},
            _display_schedule=None,
            _clutter_calibrating=False,
            _win=np.ones(256),
            complete_only=False,
            last_display_block=-1,
            block_time={0: time.monotonic() - 1},
            block_acc={0: {a: {0: tone * np.exp(1j * phase[a])} for a in range(8)}},
            _rx_sensor_ids=lambda: tuple(range(8)),
            display_width=256,
            iqcontrol=SimpleNamespace(display_correction=lambda board, a, n: np.full(n, np.exp(-1j * phase[a]))),
            active_board=0,
            _placeholder_row=lambda: np.zeros((256, 4), np.uint8),
            providers=[Mock() for _ in range(8)],
            rows_rendered=[0] * 8,
            waterfallFrameReady=Mock(),
        )
        Analyzer._render_block_rows(backend, [], "phase")
        zero_phase = module.PHASE_MAPPER.to_rgba(0, bytes=True)
        for provider in backend.providers:
            # fftshift maps the single bright bin 7 to pixel 135.
            pixel = provider.add_rows.call_args.args[0][0, 135]
            np.testing.assert_allclose(pixel, zero_phase, atol=3)

    def test_nondividing_ping_count_rejected(self):
        with self.assertRaises(ValueError):
            Analyzer._build_ping_waveform(None, dict(chirp_frames=16, bandwidth_hz=40e6, pings_per_256=3))

    def test_only_hardware_setup_edits_require_new_calibration(self):
        tx, _ = self.setup_tx(["espargostx"])
        settings = dict(vars(tx.args), chirp_frames=16, bandwidth_hz=40e6, pings_per_256=2, tx_gain_db=-20)
        original = Analyzer._tx_setup(settings)
        for key, value in dict(chirp_frames=32, bandwidth_hz=20e6, pings_per_256=1, tx_gain_db=-30, tx_azimuth=0, tx_elevation=0, tx_squint=False, tx_calibration="monitor", calibration_path="antenna_l").items():
            with self.subTest(key=key):
                self.assertEqual(Analyzer._tx_setup(dict(settings, **{key: value})), original)
        for key, value in dict(frequency_mhz=2400, tx_gain_a=1, tx_gain_b=1, tx_bandwidth=20, tx_output="antenna_l", tx_sensors=["espargosrx.1"], timing_path="antenna_l").items():
            with self.subTest(key=key):
                self.assertNotEqual(Analyzer._tx_setup(dict(settings, **{key: value})), original)

    def test_matched_filter_uses_actual_rx_rate_and_reports_pulse_start(self):
        cfg = dict(chirp_frames=64, bandwidth_hz=8e6, pings_per_256=1)
        waveform = Analyzer._build_ping_waveform(None, cfg)
        for rate in (80e6, 40e6, 20e6, 10e6, 8e6, 4e6):
            with self.subTest(rate=rate):
                reference, _ = module.receive_chirp_reference(waveform, cfg, rate)
                backend = SimpleNamespace(_tx_waveform=waveform, _reference_ping_config=lambda: cfg, _spectrum_sample_rate_hz=rate)
                received = np.zeros(256, np.complex64)
                received[40 : 40 + reference.size] = reference * np.exp(0.7j)
                spatial = Analyzer._angle_matched_trace(backend, received)
                chart, _, peaks = Analyzer._matched_filter(backend, received)
                self.assertEqual(np.argmax(abs(spatial)), 40)
                self.assertIn(40, peaks)
                np.testing.assert_allclose(chart, spatial)

    def test_empty_matched_filter_retains_return_shape(self):
        backend = SimpleNamespace(_tx_waveform=None)
        complex_values, magnitudes, peaks = Analyzer._matched_filter(backend, np.zeros(10))
        self.assertEqual((len(complex_values), len(magnitudes), peaks), (10, 10, []))

    def test_uploaded_chirp_recovers_arrival_angle_independently_of_tx_steering(self):
        tx, _ = self.setup_tx(["espargostx"])  # TX points at 25 degrees.
        tx.configure()
        cfg = dict(chirp_frames=32, bandwidth_hz=8e6, pings_per_256=2)
        waveform = tx.start(Analyzer._build_ping_waveform(None, cfg), -20)
        row, column = np.indices((2, 4))
        azimuth, elevation = np.deg2rad([-30, 20])
        frequency = 2.38e9
        kd = 2 * np.pi * frequency * module.espargos.constants.ANTENNA_SEPARATION / 299792458
        phase = kd * (column * np.cos(elevation) * np.sin(azimuth) + row * np.sin(elevation))
        received = np.zeros((8, 256), np.complex64)
        received[:, 40:72] = np.exp(1j * phase.ravel())[:, None] * waveform[None, :32]
        backend = SimpleNamespace(
            _tx_waveform=waveform,
            _reference_ping_config=lambda: cfg,
            _spectrum_sample_rate_hz=80e6,
            _spectrum_center_hz=frequency,
            _rx_sensor_ids=lambda: tuple(range(8)),
            _angle_geometry=lambda: np.arange(8).reshape(2, 4),
            fft_size=256,
            angleChanged=Mock(),
        )
        traces = {a: Analyzer._angle_matched_trace(backend, received[a]) for a in range(8)}
        backend._angle_corrected_traces = lambda traces: (np.array(list(traces.values())), True)
        Analyzer._update_angle_estimate(backend, traces)
        self.assertEqual(backend._angle_estimate_deg, -30)
        self.assertEqual(backend._elevation_estimate_deg, 20)


if __name__ == "__main__":
    unittest.main()
