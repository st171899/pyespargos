"""Verify receive phase and timing after restoring mixed IQ TX/RX roles."""

import numpy as np

from ._iq_calibration import reference as iq_reference
from .iq_tx_control import _rpc
from .iq_packet import DECIM_TO_FS
from .sensor_calibration import SensorCalibration, compute_reference_path_delays, ClockReferenceScope


def calibrate_receivers(pool, center, timeout=10.0, rx_gain=None, cable_lengths=None, cable_velocity_factors=None, scope=None):
    """Measure current RX sensors without re-firing calibrated TX engines.

    A TX-to-RX role transition can change the receive PLL phase. The earlier
    all-RX calibration used to fit reciprocal pilots therefore cannot be used
    as the final receive calibration. Keep TX roles, RF settings and replay
    buffers fixed, measure RX against REF, then bind the fit to live epochs
    after restoring the user's capture settings.
    """
    from ._iq_calibration.scope import CalibrationScope

    scope = scope or CalibrationScope(pool)
    boards = pool.boards
    saved = iq_reference._map(lambda b: b.iq.get_config(), boards)
    shape = (len(boards), 2, 4)
    mask = np.zeros(shape, bool)
    for bi, (board, config) in enumerate(zip(boards, saved)):
        for a, mode in enumerate(config["modes"]):
            if mode == "iq_rx" and a in scope.ids[bi]:
                mask[bi, *board.revision.antenna_id_to_row_col(a)] = True
    if not mask.any():
        return
    if not pool._reference_lock.acquire(blocking=False):
        raise RuntimeError("Synchronization or calibration is already running")
    states = None
    success = False
    restored = False
    pool._calibrating = True
    pool._calibration = None

    def restore(pair, *, trigger=True):
        bi, board = pair
        if mask[bi].any():
            config = saved[bi]
            # Never send modes or a frequency write to TX sensors here.
            board.iq.set_config(
                {
                    "receivers": [dict({k: v for k, v in r.items() if k != "rf_freq_hz"}, antid=a) for a, r in enumerate(config["receivers"]) if config["modes"][a] == "iq_rx" and a in scope.ids[bi]],
                    **{k: config[k] for k in ("rf_switch", "adc_decimation")},
                    **({"trigger": config["trigger"]} if trigger else {}),
                }
            )

    try:
        states = iq_reference._map(lambda b: _rpc(b, "get_iq_tx"), boards)
        iq_reference._map(lambda b: _rpc(b, "set_iq_tx", {"muted": True}), [boards[bi] for bi in scope.active])
        external = boards[0].wifi_rx.get_config()["calib-source"] != 0
        gain = rx_gain if rx_gain is not None else (iq_reference.REFERENCE_EXTERNAL_RX_GAIN if external else iq_reference.REFERENCE_RX_GAIN)

        def prepare(pair):
            bi, board = pair
            config = saved[bi]
            if mask[bi].any():
                board.iq.set_config(
                    {
                        "receivers": [dict(antid=a, **({"gain_mode": "manual", "rx_gain": gain} if getattr(pool, "gain_phase_compensation", True) else {})) for a, mode in enumerate(config["modes"]) if mode == "iq_rx" and a in scope.ids[bi]],
                        "rf_switch": 1,
                        "adc_decimation": 1,
                        "trigger": {"mode": "interval", "interval_chunks": 8192, "burst_chunks": 4},
                    }
                )

        iq_reference._map(prepare, enumerate(boards))
        boards[0].iq.set_reference("iq", center, iq_reference.REFERENCE_BACKOFF)
        pool.clear_chunks()
        training = iq_reference.collect(pool, count=32, timeout=timeout, mask=mask)
        ref = int(np.flatnonzero(mask)[0])
        tau, phi, quality = (values[mask.ravel()] for values in iq_reference.fit(training, ref, mask=mask))
        validation = iq_reference.collect(pool, count=32, timeout=timeout, mask=mask)
        tau2, phi2, _ = (values[mask.ravel()] for values in iq_reference.fit(validation, ref, mask=mask))
        if not np.array_equal(training[0].fire_time_ns[mask], validation[0].fire_time_ns[mask]):
            raise RuntimeError("RX capture epoch changed during final calibration")
        residual = np.abs(np.angle(np.exp(1j * (phi2 - phi)))) + np.pi * np.abs(tau2 - tau) * (36e6 / 80e6)
        if residual.max() > 0.30:
            raise RuntimeError(f"Final RX phase/time validation residual {residual.max():.3f} rad")
        # Obtain the measured full-rate grid before restoring capture rate and
        # trigger; both can re-fire the RX engines independently of TX.
        training_grid = training[0].grid_origin_ns[mask]
        # Keep deterministic interval packets until timing has been read.
        # Accumulation emits no ordinary chunks; Signal may never fire.
        # Trigger-only changes do not restart the RX sample engines.
        iq_reference._map(lambda pair: restore(pair, trigger=False), enumerate(boards))
        boards[0].iq.set_reference()
        pool.clear_chunks()
        epoch, grid = iq_reference.collect_origins(pool, mask=mask, timeout=timeout)

        def restore_trigger(pair):
            bi, board = pair
            if mask[bi].any():
                board.iq.set_config({"trigger": saved[bi]["trigger"]})

        iq_reference._map(restore_trigger, enumerate(boards))
        restored = True
        timing = -iq_reference.transfer_delay(tau / 80e6, training_grid, grid[mask], 0)
        phases, delays = np.zeros(shape), np.zeros(shape)
        phases[mask], delays[mask] = phi, timing
        model = SensorCalibration(
            sensor_shape=shape,
            timing_offsets=delays,
            phase_offsets=phases,
            phase_reference_frequency=center,
            clock_scope=ClockReferenceScope.POOL,
            reference_path_delays=compute_reference_path_delays(boards, board_cable_lengths=cable_lengths, board_cable_vfs=cable_velocity_factors),
        )
        # RX-only work must leave the selected TX replay epochs unchanged.
        current = iq_reference._map(lambda b: _rpc(b, "get_iq_tx"), boards)
        for bi, config in enumerate(saved):
            if current[bi]["modes"] != config["modes"]:
                raise RuntimeError("Sensor roles changed during final RX calibration")
            for a, mode in enumerate(config["modes"]):
                if mode == "iq_tx" and any(current[bi]["sensors"][a][key] != states[bi]["sensors"][a][key] for key in ("boot_id", "epoch", "origin_ns")):
                    raise RuntimeError("A calibrated TX replay restarted during RX calibration")
        pool._calibration = model
        pool._calibration_epoch, pool._calibration_grid_origin = epoch.tobytes(), grid
        pool._calibration_center_frequency = center
        rates = {DECIM_TO_FS[c["adc_decimation"]] for bi, c in enumerate(saved) if mask[bi].any()}
        if len(rates) != 1:
            raise RuntimeError("Final RX calibration requires one sample rate")
        pool._calibration_sample_rate = rates.pop()
        pool._logger.info("Final RX calibration verified for %d sensors: coherence %.3f, residual %.3f rad", mask.sum(), quality.min(), residual.max())
        success = True
    finally:
        try:
            if not restored:
                iq_reference._map(restore, enumerate(boards))
            boards[0].iq.set_reference()
            if success:
                iq_reference._map(lambda pair: _rpc(pair[1], "set_iq_tx", {"muted": states[pair[0]]["muted"]}), [(bi, boards[bi]) for bi in scope.active])
        except Exception:
            success = False
            raise
        finally:
            try:
                if not success:
                    pool._calibration = None
                    iq_reference._map(lambda b: _rpc(b, "set_iq_tx", {"muted": True}), [boards[bi] for bi in scope.active])
            finally:
                pool._calibrating = False
                pool._reference_lock.release()
