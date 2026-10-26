"""Final receive calibration must retain TX epochs and bind to live RX epochs."""

import copy
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from espargos._iq_calibration import reference as iq_reference
from espargos.iq_receiver import IQReceiver
from espargos.iq_rx_refinement import calibrate_receivers


class RXRefinementTest(unittest.TestCase):
    def setup_pool(self, *, fail=False, restart_tx=False, rx_board=0):
        shape = (2, 2, 4)
        mask = np.zeros(shape, bool)
        mask[rx_board] = True
        configs, states, boards = [], [], []
        for bi in range(2):
            configs.append(
                dict(modes=["iq_rx" if bi == rx_board else "iq_tx"] * 8, receivers=[dict(rf_freq_hz=2400e6, rx_gain=12, gain_mode="manual") for _ in range(8)], rf_switch=2, adc_decimation=2, trigger=dict(mode="continuous"), config_generation=1)
            )
            states.append(dict(modes=configs[bi]["modes"][:], muted=True, sensors=[dict(boot_id=1, epoch=99, origin_ns=70000) for _ in range(8)]))
            board = SimpleNamespace(host=f"board{bi}", revision=SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4), calib_trace_delays=np.zeros((2, 4))))
            board.iq = Mock()
            board.wifi_rx = Mock()
            board.wifi_rx.get_config.return_value = {"calib-source": 0}
            board.iq.get_config.side_effect = lambda bi=bi: copy.deepcopy(configs[bi])

            def configure(payload, bi=bi):
                self.assertNotIn("modes", payload)
                self.assertNotIn("transmitters", payload)
                self.assertEqual(bi, rx_board)  # No configuration writes to the TX board.
                for receiver in payload.get("receivers", []):
                    self.assertNotIn("frequency_hz", receiver)
                    self.assertNotIn("rf_freq_hz", receiver)
                    a = receiver["antid"]
                    configs[bi]["receivers"][a].update({k: v for k, v in receiver.items() if k != "antid"})
                configs[bi].update({k: copy.deepcopy(v) for k, v in payload.items() if k != "receivers"})

            board.iq.set_config.side_effect = configure
            boards.append(board)
        original = copy.deepcopy(configs)
        pool = SimpleNamespace(boards=boards, _reference_lock=threading.Lock(), _calibrating=False, _calibration=object(), clear_chunks=Mock(), _logger=Mock())
        rng = np.random.default_rng(17)
        # Distinct post-TX receive PLL phases, including a nonzero reference.
        phase = np.arange(8) * 0.18 + 0.4
        training = []
        for _ in range(64):
            source = rng.normal(size=256) + 1j * rng.normal(size=256)
            iq = np.full(shape + (256,), np.nan, complex)
            iq[mask] = source * np.exp(1j * phase[:, None])
            training.append(SimpleNamespace(iq=iq, fire_time_ns=np.full(shape, 100000, np.uint64), grid_origin_ns=np.full(shape, 100000, np.int64)))
        live_epoch, live_grid = np.zeros(shape, np.uint64), np.zeros(shape, np.int64)
        live_epoch[mask] = 1000000 + np.arange(8) * 50
        live_grid[mask] = 120000 + np.arange(8) * 25
        live = SimpleNamespace(fire_time_ns=live_epoch, grid_origin_ns=live_grid, completion=mask.copy())

        def collect(receiver, count, timeout, mask, reference=True):
            if fail:
                raise RuntimeError("RX reference failed")
            if reference:
                return training[:32] if not collected else training[32:]
            return [live]

        collected = []

        def collected_samples(*args, **kwargs):
            result = collect(*args, **kwargs)
            collected.append(True)
            return result

        def collect_origins(*args, **kwargs):
            self.assertEqual(configs[rx_board]["trigger"]["mode"], "interval")
            self.assertEqual(configs[rx_board]["adc_decimation"], original[rx_board]["adc_decimation"])
            collected.append(True)
            return live_epoch, live_grid

        def rpc(board, path, payload=None):
            bi = boards.index(board)
            if path == "get_iq_tx":
                result = copy.deepcopy(states[bi])
                if restart_tx and len(collected) >= 3 and bi != rx_board:
                    result["sensors"][0]["epoch"] += 1
                return result
            self.assertEqual(payload.keys(), {"muted"})
            states[bi]["muted"] = payload["muted"]

        with patch("espargos.iq_rx_refinement._rpc", side_effect=rpc), patch.object(iq_reference, "collect", side_effect=collected_samples), patch.object(iq_reference, "collect_origins", side_effect=collect_origins):
            if fail or restart_tx:
                with self.assertRaisesRegex(RuntimeError, "RX reference failed|TX replay restarted"):
                    calibrate_receivers(pool, 2400e6)
                self.assertIsNone(pool._calibration)
                self.assertTrue(all(s["muted"] for s in states))
            else:
                calibrate_receivers(pool, 2400e6)
                model = pool._calibration
                np.testing.assert_allclose(model.phase_offsets[mask], phase - phase[0], atol=1e-10)
                np.testing.assert_allclose(model.timing_offsets[mask], -np.arange(8) * 25e-9, atol=1e-15)
                self.assertEqual(pool._calibration_sample_rate, 40e6)
                self.assertEqual(pool._calibration_center_frequency, 2400e6)
                self.assertEqual(pool._calibration_epoch, live_epoch.tobytes())
                self.assertTrue(IQReceiver.calibration_applies_to(pool, live))
                # Epochs are checked only for present, calibrated receivers.
                partial = SimpleNamespace(fire_time_ns=live_epoch.copy(), completion=np.zeros(shape, bool))
                partial.completion[rx_board, 0, 2] = True
                self.assertTrue(IQReceiver.calibration_applies_to(pool, partial))
                partial.fire_time_ns[rx_board, 0, 2] += 1
                self.assertFalse(IQReceiver.calibration_applies_to(pool, partial))
            self.assertEqual(configs, original)
            self.assertFalse(pool._calibrating)
            self.assertFalse(pool._reference_lock.locked())

    def test_final_rx_phase_timing_and_epoch_are_live_without_restarting_tx(self):
        self.setup_pool()

    def test_final_rx_reference_can_be_on_second_board(self):
        self.setup_pool(rx_board=1)

    def test_failed_rx_calibration_invalidates_model_and_mutes(self):
        self.setup_pool(fail=True)

    def test_rx_calibration_rejects_a_restarted_tx(self):
        self.setup_pool(restart_tx=True)

    def test_tx_entry_point_refines_rx_before_publishing_calibration(self):
        from espargos._iq_calibration import orchestration as iq_tx_calibration, reciprocity as iq_tx_reciprocity_calibration

        calls = []
        result = {"_center_hz": 2400e6}
        with patch.object(iq_tx_reciprocity_calibration, "calibrate", return_value=result), patch("espargos.iq_rx_refinement.calibrate_receivers", side_effect=lambda *args, **kwargs: calls.append("RX")), patch.object(
            iq_tx_calibration, "snapshot", side_effect=lambda *args: calls.append("publish")
        ):
            iq_tx_calibration.calibrate(Mock(), method="reciprocity")
        self.assertEqual(calls, ["RX", "publish"])

    def test_reference_collector_accepts_only_complete_selected_receivers(self):
        shape = (2, 2, 4)
        mask = np.zeros(shape, bool)
        mask[0] = True
        sample = SimpleNamespace(iq=np.full(shape + (256,), np.nan, complex), completion=mask, config_generation=np.zeros(shape, int), fire_time_ns=np.zeros(shape, np.uint64), sync_info=np.zeros(shape, np.uint32), adc_peak=np.full(shape, 100))
        sample.iq[mask] = np.random.default_rng(1).normal(size=(8, 256)) * 20
        sample.config_generation[mask] = 4
        sample.sync_info[mask] = 1 << 16
        sample.fire_time_ns[mask] = 123
        board = SimpleNamespace(iq=Mock())
        board.iq.get_config.return_value = {"config_generation": 4}
        pool = SimpleNamespace(boards=[board, board], remove_callback=Mock(), processing=True)

        def subscribe(callback, callback_predicate):
            self.assertTrue(callback_predicate(sample))
            callback(sample)
            return 1

        pool.add_callback = subscribe
        self.assertEqual(iq_reference.collect(pool, count=1, mask=mask), [sample])

    def test_origins_accept_disjoint_partial_clusters_and_reject_stale_data(self):
        shape = (2, 2, 4)
        selected = np.ones(shape, bool)
        selected[1, 0, 0] = False  # A transmitter never supplies RX metadata.
        boards = [SimpleNamespace(iq=Mock()) for _ in range(2)]
        for generation, board in enumerate(boards, 4):
            board.iq.get_config.return_value = {"config_generation": generation}
        pool = SimpleNamespace(boards=boards, processing=True, remove_callback=Mock())
        samples = []
        for bi, generation in ((0, 3), (0, 4), (1, 5)):
            present = np.zeros(shape, bool)
            present[bi] = selected[bi]
            samples.append(
                SimpleNamespace(completion=present, config_generation=np.full(shape, generation), sync_info=np.full(shape, 1 << 16), fire_time_ns=np.full(shape, generation * 100, np.uint64), grid_origin_ns=np.full(shape, generation * 10, np.int64))
            )

        def subscribe(callback, callback_predicate):
            for sample in samples:
                self.assertTrue(callback_predicate(sample))
                callback(sample)
            return 7

        pool.add_callback = subscribe
        epoch, grid = iq_reference.collect_origins(pool, selected)
        np.testing.assert_array_equal(epoch[0], 400)
        np.testing.assert_array_equal(grid[0], 40)
        np.testing.assert_array_equal(epoch[1][selected[1]], 500)
        self.assertEqual(epoch[1, 0, 0], 0)
        pool.remove_callback.assert_called_once_with(7)

    def test_origins_timeout_removes_callback(self):
        board = SimpleNamespace(iq=Mock())
        board.iq.get_config.return_value = {"config_generation": 4}
        pool = SimpleNamespace(boards=[board], processing=True, add_callback=Mock(return_value=8), remove_callback=Mock())
        with self.assertRaisesRegex(RuntimeError, "0/8 receivers"):
            iq_reference.collect_origins(pool, np.ones((1, 2, 4), bool), timeout=0)
        pool.remove_callback.assert_called_once_with(8)

    def test_missing_and_tx_positions_have_zero_grid_origin_without_warnings(self):
        from espargos.iq_cluster import IQCluster

        revision = SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4))
        cluster = IQCluster(0, [revision])
        cluster._completion[0, 0, 0] = True
        cluster._sample_rate_hz[0, 0, 0] = 80_000_000
        cluster._fire_time_ns[0, 0, 0] = 10000
        cluster._source_chunk_index[0, 0, 0] = 5
        cluster._chunk_counter[0, 0, 0] = 3
        with np.errstate(all="raise"):
            origins = cluster.grid_origin_ns
        self.assertEqual(origins[0, 0, 0], 3600)
        np.testing.assert_array_equal(origins[~cluster.completion], 0)


if __name__ == "__main__":
    unittest.main()
