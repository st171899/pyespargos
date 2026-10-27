"""Recovery must retain calibration quality checks and command semantics."""

from contextlib import nullcontext
from pathlib import Path
import sys
import threading
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from espargos import iq_tx_control
from espargos._iq_calibration import reference as iq_reference, monitor as iq_tx_monitor_calibration
from espargos.board import Board, EspargosHTTPStatusError
from espargos.iq_receiver import IQReceiver
from espargos.iq_transmitter import IQTransmitter


class CalibrationRecoveryTest(unittest.TestCase):
    def test_timing_gain_validation_and_forwarding(self):
        from espargos.pool import AntennaSelection
        from espargos._iq_calibration import delay

        owner = SimpleNamespace(_lock=nullcontext(), open=Mock())
        selection = SimpleNamespace(_pool=owner, antennas=["rx", "tx"])
        for gain in (-1, 77, 24.5, True, float("nan"), float("inf"), "42"):
            with self.subTest(gain=gain), self.assertRaisesRegex(ValueError, "rx_gain"):
                AntennaSelection.calibrate_tx_rx_delay(selection, rx_gain=gain)
        owner.open.assert_not_called()
        with patch.object(delay, "measure") as measure:
            self.assertIs(AntennaSelection.calibrate_tx_rx_delay(selection, rx_gain=42), measure.return_value)
            measure.assert_called_once_with(owner, selection.antennas, "reference", None, 5.0, rx_gain=42)

    def test_noise_only_timing_probes_still_fail_the_quality_gate(self):
        from espargos._iq_calibration.delay import _delay
        from espargos.iq_tx_rx_delay import TxRxDelayMeasurementError

        rng = np.random.default_rng(274)
        noise = rng.normal(size=(8, 256)) + 1j * rng.normal(size=(8, 256))
        with self.assertRaises(TxRxDelayMeasurementError) as raised:
            _delay(noise, 80e6)
        self.assertEqual(raised.exception.reason, "weak")

    def test_over_air_delay_recovers_hardware_delay_from_noisy_fractional_captures(self):
        from espargos._iq_calibration import delay
        from espargos.calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState
        from espargos.constants import SPEED_OF_LIGHT

        rate, period = 80e6, 256
        boards = [Mock(host=host) for host in ("rx", "tx")]
        groups, states = [], []
        for bi, board in enumerate(boards):
            role = "rx" if bi == 0 else "tx"
            board.iq.get_config.return_value = dict(modes=["iq_" + role] * 2 + ["wifi"] * 6, adc_decimation=1, config_generation=1, rf_switch=1, trigger=dict(mode="interval"), receivers=[dict(antid=a, rf_freq_hz=2400e6) for a in range(8)])
            states.append(dict(muted=True, steering=dict(phase_deg=[0] * 8, delay_samples=[0] * 8), sensors=[dict(antid=a, samples=period, boot_id=1, origin_ns=100, output="antenna_r", amplitude=1, rf_gain=dict(a=0, b=0)) for a in range(8)]))
            groups.append(CalibrationGroup(tuple(AntennaCorrection(CalibrationState(Antenna(board.host, a), role, 2400e6, rate, 1, 100, sample_origin_ns=100), 0.2 - 0.9 * a, (0.3 - 0.8 * a) / rate) for a in range(2))))
        calibration = Calibration(tuple(groups))
        owner = SimpleNamespace(boards=boards, refresh_calibration=Mock(return_value=calibration))
        tx = [c.state.antenna for c in groups[1].corrections]
        hardware_samples = {tx[0]: 17.35, tx[1]: 22.8}
        frequencies = np.fft.fftfreq(period, 1 / rate)
        _, spectrum, _ = delay.pilots(period, peak=128, mask=255)[0]
        current = [None]

        def waveform(board, recipe, *args, **kwargs):
            if recipe["type"] == "calibration":
                current[0] = Antenna(board.host, recipe["antennas"][0])

        def rpc(board, command, payload=None):
            return states[boards.index(board)] if command == "get_iq_tx" else {}

        for distance in (0.4, 3.0):
            paths = {(t, c.state.antenna): distance / SPEED_OF_LIGHT for t in tx for c in groups[0].corrections}
            rng = np.random.default_rng(73)
            capture = Mock()

            def collect(*args):
                frames = {}
                transmitter = current[0]
                for c in groups[0].corrections:
                    received_delay = hardware_samples[transmitter] / rate + paths[transmitter, c.state.antenna]
                    channel = np.exp(-2j * np.pi * frequencies * received_delay) / c.response(2400e6 + frequencies)
                    trace = np.fft.ifft(spectrum * channel) * 0.5
                    chunks = []
                    for _ in range(16):
                        noisy = trace + 0.02 * (rng.normal(size=period) + 1j * rng.normal(size=period))
                        chunks.append([SimpleNamespace(decode_iq=lambda z=noisy: z)])
                    frames[c.state.antenna.antid] = chunks
                return frames

            capture.collect.side_effect = collect
            with patch.object(delay, "_rpc", side_effect=rpc), patch.object(delay, "_waveform", side_effect=waveform), patch.object(delay, "_Captures", return_value=capture), patch.object(delay, "_grid_origin", return_value=100):
                result = delay.measure(owner, {c.state.antenna for g in groups for c in g.corrections}, "antenna_r", paths, 5)
            for transmitter in tx:
                self.assertAlmostEqual(result.delays_s[transmitter] * rate, hardware_samples[transmitter], delta=0.03)
            probe = boards[0].iq.set_config.call_args_list[-2].args[0]
            self.assertEqual(probe["rf_switch"], 2)
            capture.close.assert_called_once()

    def test_delay_probe_leaves_tx_only_capture_config_untouched(self):
        from espargos._iq_calibration import delay
        from espargos.calibration import Antenna, AntennaCorrection, Calibration, CalibrationGroup, CalibrationState

        boards = [Mock(host=host) for host in ("rx", "tx")]
        configs, states, corrections = [], [], []
        for bi, board in enumerate(boards):
            board.revision.antenna_id_to_row_col.side_effect = lambda a: divmod(a, 4)
            config = dict(modes=(["iq_rx"] + ["wifi"] * 7 if bi == 0 else ["iq_tx"] * 8), adc_decimation=4, rf_switch=2, trigger=dict(mode="continuous"), receivers=[dict(antid=a, rf_freq_hz=2400e6) for a in range(8)], config_generation=1)
            board.iq.get_config.return_value = config
            configs.append(config)
            states.append(dict(muted=True, steering=dict(phase_deg=[0] * 8, delay_samples=[0] * 8), sensors=[dict(antid=a, samples=256, boot_id=1, origin_ns=100, output="antenna_r", amplitude=1, rf_gain=dict(a=0, b=0)) for a in range(8)]))
            state = CalibrationState(Antenna(board.host, 0), "rx" if bi == 0 else "tx", 2400e6, 20e6 if bi == 0 else 80e6, 1, 100, sample_origin_ns=100)
            corrections.append(AntennaCorrection(state, 0, 0))
        calibration = Calibration(tuple(CalibrationGroup((c,)) for c in corrections))
        owner = SimpleNamespace(boards=boards, _geometry=SimpleNamespace(cables={}, velocity_factors={}), refresh_calibration=Mock(return_value=calibration))
        capture = Mock()
        packet = SimpleNamespace(decode_iq=lambda: np.ones(256, complex))
        capture.collect.return_value = {0: [[packet] for _ in range(16)]}

        def rpc(board, command, payload=None):
            return states[boards.index(board)] if command == "get_iq_tx" else {}

        with patch.object(delay, "_rpc", side_effect=rpc), patch.object(delay, "_Captures", return_value=capture), patch.object(delay, "_waveform"), patch.object(delay, "_grid_origin", return_value=100), patch.object(delay, "_delay", return_value=10e-9), patch.object(delay, "compute_reference_path_delays", return_value={(0, 0, 0): 0, (1, 0, 0): 0}):
            result = delay.measure(owner, {c.state.antenna for c in corrections}, "reference", None, 5, rx_gain=42)
        self.assertEqual(result.delays_s, {Antenna("tx", 0): 10e-9})
        boards[1].iq.set_config.assert_not_called()
        probe, restored = [call.args[0] for call in boards[0].iq.set_config.call_args_list]
        self.assertEqual(probe["adc_decimation"], 1)
        self.assertEqual(probe["receivers"], [dict(antid=0, rx_gain=42, gain_mode="manual")])
        self.assertEqual(restored, {k: configs[0][k] for k in ("rf_switch", "receivers", "trigger", "adc_decimation")})
        capture.close.assert_called_once()

    def test_mute_after_memory_error_requires_fresh_streamed_silence(self):
        board = Mock()
        capture = SimpleNamespace(changed=threading.Condition(), status={board: [{"sequence": 1}] * 8}, wait=Mock())
        tx = IQTransmitter.__new__(IQTransmitter)
        tx.boards = [board]
        tx.authorization, tx.capture = [True], capture
        tx._parallel = lambda fn: [fn(0, board)]
        with patch.object(iq_tx_control, "_rpc"), patch("espargos.iq_transmitter._rpc", side_effect=EspargosHTTPStatusError(503, "set_iq_tx", "memory")):
            tx.mute(True)
        predicate = capture.wait.call_args_list[0].args[1]
        silent = [dict(sequence=2, enabled=True, applied_amplitude=0)] * 8
        self.assertTrue(predicate({"sensors": silent}))
        self.assertFalse(predicate({"sensors": [dict(s, sequence=1) for s in silent]}))
        self.assertFalse(predicate({"sensors": [dict(s, applied_amplitude=1) for s in silent]}))

    def test_rx_snapshot_uses_streamed_boot_ids_without_full_tx_json(self):
        from espargos._iq_calibration import orchestration as iq_tx_calibration
        from espargos.calibration import Calibration

        board = Mock(host="board")
        board.revision.antenna_id_to_row_col.side_effect = lambda a: divmod(a, 4)
        board.iq.get_config.return_value = dict(modes=["iq_rx"] * 8, adc_decimation=1, receivers=[dict(rf_freq_hz=2400e6) for _ in range(8)])
        capture = iq_tx_control.TXStatus.__new__(iq_tx_control.TXStatus)
        capture.changed = threading.Condition()
        capture.status = {board: [dict(boot_id=7, applied_mode="iq_rx")] * 8}
        shape = (1, 2, 4)
        owner = SimpleNamespace(_tx=SimpleNamespace(capture=capture), calibrations=Calibration())
        receiver = SimpleNamespace(
            boards=[board],
            _owner=owner,
            _calibration_epoch=np.full(shape, 123, np.uint64).tobytes(),
            calibration_sample_rate=80e6,
            _calibration_grid_origin=None,
            calibration=SimpleNamespace(phase_reference_frequency=2400e6, phase_offsets=np.zeros(shape), reference_path_delays=np.zeros(shape), timing_offsets=np.zeros(shape)),
        )
        with patch.object(iq_tx_calibration, "_rpc", side_effect=AssertionError("Full TX JSON requested")):
            result = iq_tx_calibration.snapshot(receiver)
        self.assertEqual(len(result.groups[0].corrections), 8)
        self.assertTrue(all(c.state.boot_id == 7 and c.state.epoch == 123 for c in result.groups[0].corrections))

    def test_quiet_udp_stream_remains_available_for_next_calibration(self):
        sock = Mock()
        sock.recvfrom.side_effect = [socket.timeout()] * 30 + [(b"next capture", ("board", 1))]
        board = SimpleNamespace(_udp_sock=sock, _stream_connected=True, _stream_timeout=5, _logger=Mock(), _enqueue_stream_message=Mock(return_value=False))
        Board._stream_loop_udp(board)
        board._enqueue_stream_message.assert_called_once_with(b"next capture")
        self.assertTrue(board._stream_connected)

    def test_http_status_failure_keeps_drained_connection_for_recovery(self):
        connection = Mock()
        connection.getresponse.side_effect = [SimpleNamespace(status=503, read=lambda: b"not enough memory"), SimpleNamespace(status=200, read=lambda: b"ok")]
        board = SimpleNamespace(_uart_client=None, _http_lock=threading.Lock(), _http_connection=connection, host="board")
        with self.assertRaises(EspargosHTTPStatusError):
            Board._fetch(board, "set_iq_tx", "{}")
        self.assertEqual(Board._fetch(board, "set_iq_tx", "{}"), "ok")
        connection.close.assert_not_called()
        self.assertIs(board._http_connection, connection)

    def test_absolute_settings_retry_transient_controller_memory_failure(self):
        board = Mock(host="board")
        board.control.post_json.side_effect = [EspargosHTTPStatusError(503, "set_iq_tx", "not enough memory"), "ok"]
        with patch.object(iq_tx_control.time, "sleep"):
            iq_tx_control._rpc(board, "set_iq_tx", {"muted": True})
        self.assertEqual(board.control.post_json.call_count, 2)

    def test_applied_write_memory_error_retries_status_only(self):
        board = Mock(host="board")
        board.control.post_json.side_effect = EspargosHTTPStatusError(503, "set_iq_tx", "not enough memory for TX status")
        board.control.get_json.side_effect = [EspargosHTTPStatusError(503, "get_iq_tx", "busy"), {"muted": True}]
        with patch.object(iq_tx_control.time, "sleep"):
            self.assertEqual(iq_tx_control._rpc(board, "set_iq_tx", {"muted": True}), {"muted": True})
        board.control.post_json.assert_called_once()
        self.assertEqual(board.control.get_json.call_count, 2)

    def test_scheduled_waveforms_are_not_retried_by_rpc(self):
        board = Mock(host="board")
        board.control.post_json.side_effect = EspargosHTTPStatusError(503, "set_iq_tx_waveform", "busy")
        with self.assertRaises(EspargosHTTPStatusError):
            iq_tx_control._rpc(board, "set_iq_tx_waveform", {"start_sample": 100})
        self.assertEqual(board.control.post_json.call_count, 1)

    def test_invalid_settings_are_not_retried(self):
        board = Mock(host="board")
        board.control.post_json.side_effect = EspargosHTTPStatusError(400, "set_iq_tx", "invalid")
        with self.assertRaises(EspargosHTTPStatusError):
            iq_tx_control._rpc(board, "set_iq_tx", {"muted": True})
        self.assertEqual(board.control.post_json.call_count, 1)

    def test_monitor_gain_recovers_weak_and_clipped_captures(self):
        for peak, delta in ((30, 12), (510, -7)):
            monitor = Mock()
            monitor.collect.side_effect = [[np.full(256, peak, complex)], [np.full(256, 240, complex)]]
            board = Mock()
            with patch.object(iq_tx_monitor_calibration, "_rpc", return_value={"gain": 12, "gain_max": 72}) as rpc:
                iq_tx_monitor_calibration._adjust_monitor_gain(monitor, board, 3)
            self.assertIn(unittest.mock.call(board, "set_iq_monitor", {"gain": 12 + delta}), rpc.call_args_list)

    def test_false_reference_onset_rearms_at_higher_threshold(self):
        shape = (1, 2, 4)
        revision = SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4), calib_trace_delays=np.zeros((2, 4)))
        board = SimpleNamespace(host="board", revision=revision, iq=Mock())
        board.iq.get_config.return_value = dict(modes=["iq_rx"] * 8, adc_decimation=1)
        receiver = SimpleNamespace(boards=[board], _logger=Mock())
        capture = SimpleNamespace(fire_time_ns=np.ones(shape, np.uint64), grid_origin_ns=np.ones(shape, np.int64))
        fit = (np.zeros(8), np.zeros(8), np.ones(8))
        with patch.object(iq_reference, "reference_session", return_value=nullcontext((2400e6, 80e6))), patch.object(iq_reference, "acquire_grid", side_effect=[iq_reference.ReferenceOnsetError("false onset"), [1]]) as acquire, patch.object(
            iq_reference, "collect", return_value=[capture]
        ), patch.object(iq_reference, "fit", return_value=fit), patch("espargos._iq_calibration.orchestration.snapshot"):
            IQReceiver._sync(receiver)
        self.assertEqual([c.kwargs["threshold"] for c in acquire.call_args_list], [48, 96])
        self.assertIsNotNone(receiver._calibration)

    def test_rx_capture_does_not_mark_pending_tx_settings_calibrated(self):
        from demos.custom.common.iq_application import IQController

        receiver = Mock()
        receiver.calibration_applies_to.return_value = True
        controller = SimpleNamespace(_reference_busy=False, receiver=receiver, calibration_guard=lambda: False, _set_sync_needed=Mock())
        IQController._observe_capture(controller, Mock())
        controller._set_sync_needed.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
