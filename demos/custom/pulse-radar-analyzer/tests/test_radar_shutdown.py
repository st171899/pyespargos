"""Board replay cleanup and shutdown ordering without connected hardware."""

import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
import radar_tx

spec = importlib.util.spec_from_file_location("radar_shutdown", DEMO / "pulse-radar-analyzer-espargi.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Analyzer = module.EspargosPulseRadarAnalyzer


class BoardResetTest(unittest.TestCase):
    def setUp(self):
        self.boards = [Mock(host="rx"), Mock(host="tx")]
        self.callbacks = {}
        for board in self.boards:
            board.iq.get_config.return_value = dict(trigger=dict(mode="signal"), config_generation=42)
            board.iq.subscribe_chunks.side_effect = lambda callback, b=board: self.callbacks.setdefault(b, callback)
        self.pool = SimpleNamespace(boards=self.boards)
        # Event waits deliver wire messages deterministically; board operations
        # remain sequential so patching Event cannot affect executor internals.
        self.event = Mock()
        self.event.wait.side_effect = self.deliver
        self.addCleanup(patch.stopall)
        patch("radar_tx._map", side_effect=lambda f, items: list(map(f, items))).start()
        patch("radar_tx.threading.Event", return_value=self.event).start()
        self.rpc = patch("radar_tx._rpc").start()
        self.waveform = patch("radar_tx._waveform").start()
        self.wait = patch("radar_tx._wait").start()

    def deliver(self, timeout):
        for board, callback in self.callbacks.items():
            for a in range(8):
                callback(SimpleNamespace(antenna_id=a, payload=SimpleNamespace(config_generation=42)))
        return True

    def test_replaces_all_replays_only_after_rx_readiness_then_confirms_wifi(self):
        def waveform(board, recipe, timeout):
            self.assertEqual(board.iq.set_config.call_args.args[0]["modes"], ["iq_rx"] * 8)
            self.assertEqual(recipe, dict(type="cw", frequency_hz=0, samples=1024, amplitude=0, antennas=list(range(8))))
            self.assertTrue(self.event.wait.called)

        self.waveform.side_effect = waveform
        radar_tx.reset_radar_boards(self.pool)
        self.assertEqual(self.waveform.call_count, 2)
        for board in self.boards:
            self.rpc.assert_any_call(board, "set_iq_tx", {"muted": True})
            board.iq.set_reference.assert_called_once_with()
            board.iq.unsubscribe.assert_called_once_with(self.callbacks[board])
            self.assertEqual(board.iq.set_config.call_args.args[0], dict(modes=["wifi"] * 8, trigger=dict(mode="signal")))
        predicate = self.wait.call_args.args[1]
        self.assertTrue(predicate(dict(sensors=[dict(applied_mode="wifi", enabled=False)] * 8)))
        self.assertFalse(predicate(dict(sensors=[dict(applied_mode="iq_rx", enabled=False)] * 8)))
        self.assertFalse(predicate(dict(sensors=[dict(applied_mode="wifi", enabled=True)] * 8)))

    def test_stale_chunks_cannot_allow_replay_reset(self):
        def stale(timeout):
            for callback in self.callbacks.values():
                for a in range(8):
                    callback(SimpleNamespace(antenna_id=a, payload=SimpleNamespace(config_generation=41)))
            return False

        self.event.wait.side_effect = stale
        with self.assertRaises(ExceptionGroup) as caught:
            radar_tx.reset_radar_boards(self.pool, timeout=0)
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.waveform.assert_not_called()
        for board in self.boards:
            self.assertEqual(board.iq.set_config.call_args.args[0]["modes"], ["wifi"] * 8)
            board.iq.unsubscribe.assert_called_once()

    def test_one_failed_waveform_does_not_skip_other_boards_or_wifi(self):
        self.waveform.side_effect = [RuntimeError("waveform rejected"), None]
        with self.assertRaises(ExceptionGroup) as caught:
            radar_tx.reset_radar_boards(self.pool)
        self.assertEqual(len(caught.exception.exceptions), 1)
        self.assertEqual(self.waveform.call_count, 2)
        self.assertEqual(self.wait.call_count, 2)
        for board in self.boards:
            self.assertEqual(board.iq.set_config.call_args.args[0]["modes"], ["wifi"] * 8)

    def test_failed_unsubscribe_still_restores_wifi(self):
        self.boards[0].iq.unsubscribe.side_effect = ConnectionError("lost connection")
        with self.assertRaises(ExceptionGroup):
            radar_tx.reset_radar_boards(self.pool)
        self.assertEqual(self.wait.call_count, 2)


class AnalyzerShutdownTest(unittest.TestCase):
    def test_workers_finish_before_board_reset_and_transport_close_even_after_mute_failure(self):
        events = []
        controller = SimpleNamespace(
            _reference_thread=Mock(), _reference_io_lock=threading.Lock(),
            _tone_state_lock=threading.Lock(), _tone_pending=("cw", 2380, 0),
            _reference_busy=False,
        )
        controller._reference_thread.join.side_effect = lambda: events.append("calibration restored old replay")
        backend = SimpleNamespace(
            _live_tx_timer=Mock(), txcontrol=Mock(), render_running=True,
            _tx_worker=Mock(), _render_worker=Mock(), iqcontrol=controller,
            _stop_ping_tx=Mock(side_effect=RuntimeError("mute failed")), pool=Mock(), logger=Mock(),
        )
        backend._tx_worker.join.side_effect = lambda: events.append("tx stopped")
        backend._render_worker.join.side_effect = lambda: events.append("render stopped")

        def reset(pool):
            self.assertFalse(backend.render_running)
            self.assertTrue(controller._reference_busy)
            self.assertIsNone(controller._tone_pending)
            events.append("reset replay")
            raise RuntimeError("one board unreachable")

        base = Mock()
        base.onAboutToQuit.side_effect = lambda: events.append("transport closed")
        with patch.object(module, "reset_radar_boards", side_effect=reset) as cleanup, patch.object(module, "super", return_value=base, create=True):
            Analyzer.onAboutToQuit(backend)
            Analyzer.onAboutToQuit(backend)
        self.assertEqual(events, ["tx stopped", "calibration restored old replay", "render stopped", "reset replay", "transport closed"])
        cleanup.assert_called_once_with(backend.pool)
        base.onAboutToQuit.assert_called_once()
        self.assertEqual(backend.logger.exception.call_count, 2)


if __name__ == "__main__":
    unittest.main()
