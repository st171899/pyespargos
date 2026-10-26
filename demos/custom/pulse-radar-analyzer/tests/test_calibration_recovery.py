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
