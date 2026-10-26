"""Receive references and board-local geometry shared by both radar demos."""

import numpy as np


def matched_filter_peaks(received, reference, correlation, threshold=0.6):
    """Validate chirp-only matches against both match quality and noise power.

    Neither the template nor the energy denominator includes surrounding TX
    silence. Another reflection may overlap the chirp or its silent interval.
    """
    magnitude = np.abs(correlation)
    reference_energy = float(np.vdot(reference, reference).real)
    energy_start = reference.size - 1
    received_energy = np.convolve(np.abs(received) ** 2, np.ones(reference.size), mode="full")[energy_start : energy_start + received.size]
    score = magnitude / np.sqrt(np.maximum(reference_energy * received_energy, 1e-12))

    # Complex Gaussian noise gives exponential correlation power. Estimate
    # its mean robustly from the median, and allow 0.0001 expected noise
    # exceedances per displayed window. This also rejects noise with very
    # short references, for which normalized correlation alone is unselective.
    power = magnitude.astype(np.float64) ** 2
    noise_mean = float(np.median(power)) / np.log(2.0)
    noise_limit = noise_mean * np.log(max(1, received.size) / 0.0001)
    local_maximum = np.ones(magnitude.size, dtype=bool)
    if magnitude.size > 1:
        local_maximum[1:] &= magnitude[1:] > magnitude[:-1]
        local_maximum[:-1] &= magnitude[:-1] >= magnitude[1:]
    candidates = np.flatnonzero(local_maximum & (score >= threshold) & (power > max(noise_limit, 1e-12)))
    # Local maxima are already distinct. Do not impose a fraction-of-chirp
    # exclusion zone: resolvable echoes can overlap in uncompressed IQ.
    return candidates.tolist()


def chirp_reference(waveform, config, receive_rate_hz, transmit_rate_hz):
    """Sample the submitted replay, rather than regenerate an ideal chirp.

    ADC decimation directly subsamples the replay. For a faster RX clock
    (e.g. 40 MSa/s Pluto into 80 MSa/s RX), interpolate the cyclic DAC
    sequence with its Fourier series. This models an ideal bandlimited DAC;
    analog TX/RX filtering and fractional propagation delay remain in the data.
    """
    period = 256 // max(1, min(int(config["pings_per_256"]), 256))
    length = max(1, min(int(config["chirp_frames"]), period))
    ratio = float(transmit_rate_hz) / float(receive_rate_hz)
    if not np.isfinite(ratio) or ratio <= 0:
        raise ValueError("TX and RX sample rates must be positive and finite")
    waveform = np.asarray(waveform, dtype=np.complex64)
    if ratio >= 1 and np.isclose(ratio, round(ratio)):
        reference = waveform[: length : round(ratio)]
    else:
        positions = np.arange(int(np.ceil(length / ratio))) * ratio
        bins = np.fft.fftfreq(waveform.size)
        reference = np.exp(2j * np.pi * positions[:, None] * bins) @ np.fft.fft(waveform) / waveform.size
    return np.asarray(reference, dtype=np.complex64), max(1, int(np.ceil(period / ratio)))


def local_geometry(revision, receiver_ids=range(8)):
    """Use physical antenna positions on just the selected board."""
    geometry = np.full((2, 4), -1, dtype=int)
    for antenna_id in receiver_ids:
        row, column = revision.antenna_id_to_row_col(antenna_id)
        geometry[row, column] = antenna_id
    return geometry
