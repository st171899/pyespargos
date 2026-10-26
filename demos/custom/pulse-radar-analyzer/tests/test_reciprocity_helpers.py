"""Reciprocal calibration orchestration with a TX board and RX helper board."""

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from espargos._iq_calibration import reciprocity as calibration
from espargos._iq_calibration.reciprocity_fit import DisconnectedLinks, fit_links


class ReciprocityHelpersTest(unittest.TestCase):
    def test_cross_board_links_connect_isolated_tx_sensors(self):
        frequencies = np.arange(-180, 181) * 80e6 / 1024
        n = 16
        phase = np.linspace(0, 0.8, n)
        delay = np.linspace(0, 1.5, n)
        response = np.exp(1j * (phase[None, None, :] + 2 * np.pi * frequencies[:, None, None] / 80e6 * delay[None, None, :]))
        response = np.broadcast_to(response, (len(frequencies), n, n)).copy()
        valid = np.zeros(response.shape, bool)
        valid[:, :8, 8:] = True
        valid[:, 8:, :8] = True
        # Same-board REF paths alone provide no usable reciprocal links.
        with self.assertRaises(DisconnectedLinks):
            fit_links(frequencies, response[:, :8, :8], valid[:, :8, :8])
        fitted_phase, fitted_delay, quality = fit_links(frequencies, response, valid)
        np.testing.assert_allclose(fitted_phase, -np.rad2deg(phase), atol=1e-7)
        np.testing.assert_allclose(fitted_delay, delay, atol=1e-7)
        self.assertEqual(quality["links"], 64)

    def simulate(self, selected, fail=False, participating=None):
        participating = participating or {(bi, a) for bi in range(2) for a in range(8)}
        count = len(participating)
        configs, states, boards = [], [], []
        zero = dict(phase_deg=[0.0] * 8, delay_samples=[0.0] * 8)
        for bi in range(2):
            modes = ["iq_tx" if (bi, a) in selected else "iq_rx" for a in range(8)]
            configs.append(dict(modes=modes, receivers=[dict(antid=a, rf_freq_hz=2380e6, gain_mode="manual", rx_gain=12) for a in range(8)], adc_decimation=1, rf_switch=2, trigger=dict(mode="continuous"), config_generation=1))
            states.append(
                dict(
                    muted=True,
                    modes=modes[:],
                    calibration=copy.deepcopy(zero),
                    steering=copy.deepcopy(zero),
                    sensors=[
                        dict(
                            antid=a,
                            frequency_hz=2380e6 if (bi, a) in selected else 2500e6,
                            rf_gain=dict(a=0, b=0),
                            bandwidth_mhz=40,
                            output="antenna_r",
                            amplitude=1,
                            samples=256 if (bi, a) in selected else (512 if a == 1 else 0),
                            boot_id=1,
                            epoch=1,
                            origin_ns=100000,
                            enabled=(bi, a) in selected,
                            applied_mode=modes[a],
                            phase_deg=0,
                            delay_samples=0,
                        )
                        for a in range(8)
                    ],
                )
            )
            board = SimpleNamespace(host=f"board{bi}", revision=SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4)))
            board.iq = Mock()
            board.iq.get_config.side_effect = lambda bi=bi: copy.deepcopy(configs[bi])

            def set_config(payload, bi=bi):
                payload = copy.deepcopy(payload)
                for receiver in payload.pop("receivers", []):
                    configs[bi]["receivers"][receiver["antid"]].update(receiver)
                configs[bi].update(payload)
                if "modes" in payload:
                    states[bi]["modes"] = payload["modes"][:]
                    for a, mode in enumerate(payload["modes"]):
                        states[bi]["sensors"][a].update(applied_mode=mode, enabled=mode == "iq_tx")

            board.iq.set_config.side_effect = set_config
            boards.append(board)
        originals = copy.deepcopy(configs)
        original_states = copy.deepcopy(states)

        def rpc(board, path, payload=None):
            bi = boards.index(board)
            if path == "get_iq_tx":
                return copy.deepcopy(states[bi])
            if path == "get_iq_monitor":
                return dict(enabled=False, gain_mode="manual", frequency_hz=2380e6, sample_rate_hz=80e6, gain=0, bandwidth_mhz=40, samples=1024, interval_us=1, offset_us=0)
            if path == "set_iq_tx":
                if "frequency_hz" in payload:
                    for sensor in states[bi]["sensors"]:
                        sensor["frequency_hz"] = payload["frequency_hz"]
                for key in ("muted", "calibration", "steering"):
                    if key in payload:
                        states[bi][key] = copy.deepcopy(payload[key])
                for sensor in payload.get("transmitters", []):
                    states[bi]["sensors"][sensor["antid"]].update(sensor)
                if "modes" in payload:
                    board.iq.set_config({"modes": payload["modes"]})
                if "calibration" in payload:
                    for a, sensor in enumerate(states[bi]["sensors"]):
                        sensor["phase_deg"] = payload["calibration"]["phase_deg"][a]
                        sensor["delay_samples"] = payload["calibration"]["delay_samples"][a]
            return None

        capture = Mock()

        def wait(board, predicate, timeout):
            state = states[boards.index(board)]
            self.assertTrue(predicate(state))
            return copy.deepcopy(state)

        capture.wait.side_effect = wait
        ready_boards = set()

        def wait_rx(board, predicate, timeout):
            result = wait(board, predicate, timeout)
            ready_boards.add(boards.index(board))
            return result

        def load_waveform(board, recipe, *args, **kwargs):
            if recipe["type"] == "calibration":
                self.assertTrue(capture.collect.called)
                self.assertEqual(capture.collect.call_args_list[0].args[1:3], (256, 1))
                self.assertEqual(recipe["antennas"], sorted(recipe["antennas"]))
                self.assertIn(boards.index(board), ready_boards)
                self.assertTrue(all(s["applied_mode"] == "iq_rx" and not s["enabled"] for a, s in enumerate(states[boards.index(board)]["sensors"]) if (boards.index(board), a) in participating))
                self.assertTrue(all(states[boards.index(board)]["sensors"][a]["frequency_hz"] == 2380e6 for a in recipe["antennas"]))

        packet = SimpleNamespace(fire_time_ns=100000, source_chunk_index=0, chunk_counter=0, sample_rate_hz=80e6, decode_iq=lambda: np.zeros(256, complex))
        capture.collect.side_effect = lambda ids, samples, count, *args, **kwargs: {i: [[packet] * (samples // 256) for _ in range(count)] for i in ids}
        model = SimpleNamespace(phase_offsets=np.zeros((2, 2, 4)), timing_offsets=np.zeros((2, 2, 4)), reference_path_delays=np.zeros((2, 2, 4)), sensor_shape=(2, 2, 4))
        pool = SimpleNamespace(boards=boards, master_board=boards[0], _calibrating=False, _calibration_grid_origin=np.full((2, 2, 4), 100000), _sync=Mock(return_value=model), apply_config=Mock(), _logger=Mock())
        from espargos.calibration import Antenna
        from espargos._iq_calibration.scope import CalibrationScope

        scope = CalibrationScope(pool, [Antenna(boards[bi].host, a) for bi, a in participating])

        def fit(f, response, valid):
            self.assertEqual(response.shape[1:], (count, count))
            if fail:
                raise RuntimeError("fit failed")
            return np.arange(count, dtype=float), np.zeros(count), dict(phase_residual_deg=0, delay_residual_samples=0)

        with patch.object(calibration, "_rpc", side_effect=rpc), patch.object(calibration, "_Captures", return_value=capture), patch.object(calibration, "_waveform", side_effect=load_waveform) as waveform, patch.object(
            calibration, "_wait", side_effect=wait_rx
        ), patch.object(calibration, "fit_links", side_effect=fit), patch.object(calibration, "validate_links", return_value=dict(phase_residual_deg=0, delay_residual_samples=0)):
            if fail:
                with self.assertRaisesRegex(RuntimeError, "fit failed"):
                    calibration.calibrate(pool, rx_gain=30, scope=scope)
                self.assertTrue(all(s["muted"] for s in states))
                self.assertTrue(all(mode == "iq_rx" for c in configs for mode in c["modes"]))
            else:
                result = calibration.calibrate(pool, rx_gain=30, scope=scope)
                self.assertEqual(set(result["_tx_states"]), selected)
                self.assertIs(pool._calibration, model if len(selected) < 16 else None)
                for bi in range(2):
                    self.assertEqual(configs[bi], originals[bi])
                    for a in range(8):
                        if (bi, a) not in selected:
                            self.assertEqual(result["calibration"][bi]["phase_deg"][a], 0)
            pilots = {(boards.index(call.args[0]), a) for call in waveform.call_args_list if call.args[1]["type"] == "calibration" for a in call.args[1]["antennas"]}
            self.assertEqual(pilots, participating)
            for call in waveform.call_args_list:
                recipe = call.args[1]
                if recipe["type"] == "restore":
                    bi = boards.index(call.args[0])
                    for a in recipe["antennas"]:
                        self.assertEqual(recipe["samples"], original_states[bi]["sensors"][a]["samples"])
            capture.close.assert_called_once()
            self.assertFalse(pool._calibrating)

    def test_whole_tx_board_uses_rx_board_as_helpers_and_restores_roles(self):
        self.simulate({(1, a) for a in range(8)})

    def test_partial_tx_selection_only_publishes_selected_corrections(self):
        self.simulate({(1, 2), (1, 5)})

    def test_calibration_helpers_stay_within_selected_scope(self):
        self.simulate({(1, 2), (1, 5)}, participating={(0, 0), (0, 1), (1, 2), (1, 5)})

    def test_all_tx_beamforming_aperture_keeps_existing_behavior(self):
        self.simulate({(bi, a) for bi in range(2) for a in range(8)})

    def test_failed_measurement_returns_helpers_and_targets_to_muted_rx(self):
        self.simulate({(1, a) for a in range(8)}, fail=True)


if __name__ == "__main__":
    unittest.main()
