"""Small dependency-free 802.11 OFDM receiver primitives for IQ captures.

It is intentionally a packet-inspection receiver, not a replacement for the
hardware modem: integer-rate anti-aliasing, STF/LTF synchronization, L-SIG and
HT-SIG decoding, all eight legacy OFDM rates, descrambling, and MPDU FCS
validation are included so signal-triggered array captures can be compared
with the normal ESPARGOS Wi-Fi receive path.
"""

from __future__ import annotations

import binascii
import math
from dataclasses import dataclass

import numpy as np

__all__ = [
    "LegacyOFDMFrame",
    "WiFiOFDMHeader",
    "decode_legacy_ofdm",
    "decode_legacy_ofdm_all",
    "decode_wifi_ofdm_headers_all",
]

POLARITY = np.array(
    [
        1, 1, 1, 1, -1, -1, -1, 1, -1, -1, -1, -1, 1, 1, -1, 1,
        -1, -1, 1, 1, -1, 1, 1, -1, 1, 1, 1, 1, 1, 1, -1, 1,
        1, 1, -1, 1, 1, -1, -1, 1, 1, 1, -1, 1, -1, -1, -1, 1,
        -1, 1, -1, -1, 1, -1, -1, 1, 1, 1, 1, 1, -1, -1, 1, 1,
        -1, -1, 1, -1, 1, -1, 1, 1, -1, -1, -1, 1, 1, -1, -1, -1,
        -1, 1, -1, -1, 1, -1, 1, 1, 1, 1, -1, 1, -1, 1, -1, 1,
        -1, -1, -1, -1, -1, 1, -1, 1, 1, -1, 1, -1, 1, 1, 1, -1,
        -1, 1, -1, -1, -1, 1, 1, 1, -1, -1, -1, -1, -1, -1, -1,
    ],
    dtype=np.float32,
)

LONG = np.array(
    [
        0, 0, 0, 0, 0, 0, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1,
        1, 1, 1, 1, 1, -1, -1, 1, 1, -1, 1, -1, 1, 1, 1, 1,
        0, 1, -1, -1, 1, 1, -1, 1, -1, 1, -1, -1, -1, -1, -1, 1,
        1, -1, -1, 1, -1, 1, -1, 1, 1, 1, 1, 0, 0, 0, 0, 0,
    ],
    dtype=np.complex64,
)

DATA_SUBCARRIERS = np.array(
    [k for k in range(-26, 27) if k not in (0, -21, -7, 7, 21)], dtype=int
)
PILOT_SUBCARRIERS = np.array((-21, -7, 7, 21), dtype=int)
PILOT_VALUES = np.array((1, 1, 1, -1), dtype=np.complex64)

# rate field -> (Mbps, coded bits/subcarrier, coded bits/symbol,
# data bits/symbol, puncturing rate)
RATE_PARAMETERS = {
    0xD: (6, 1, 48, 24, "1/2"),
    0xF: (9, 1, 48, 36, "3/4"),
    0x5: (12, 2, 96, 48, "1/2"),
    0x7: (18, 2, 96, 72, "3/4"),
    0x9: (24, 4, 192, 96, "1/2"),
    0xB: (36, 4, 192, 144, "3/4"),
    0x1: (48, 6, 288, 192, "2/3"),
    0x3: (54, 6, 288, 216, "3/4"),
}


@dataclass(frozen=True)
class LegacyOFDMFrame:
    sample_offset: int
    rate_mbps: int
    psdu_length: int
    psdu: bytes
    fcs_ok: bool
    coarse_cfo_hz: float
    fine_cfo_hz: float
    signal_snr_db: float
    frame_type: int | None
    frame_subtype: int | None
    retry: bool
    sequence_number: int | None
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class WiFiOFDMHeader:
    """A validated legacy-compatible PHY header, even without a full PSDU."""

    sample_offset: int
    phy_format: str
    legacy_rate_mbps: int
    legacy_length: int
    coarse_cfo_hz: float
    fine_cfo_hz: float
    signal_snr_db: float
    mcs_index: int | None = None
    channel_width_mhz: int | None = None
    psdu_length: int | None = None
    stbc_streams: int | None = None
    short_guard_interval: bool | None = None


def _sliding_sum(values, length):
    cumulative = np.concatenate((np.zeros(1, dtype=values.dtype), np.cumsum(values)))
    return cumulative[length:] - cumulative[:-length]


def _to_20_msps(samples, sample_rate_hz):
    samples = np.asarray(samples, dtype=np.complex64).reshape(-1)
    ratio = int(round(float(sample_rate_hz) / 20_000_000.0))
    if ratio not in (1, 2, 4) or abs(float(sample_rate_hz) - ratio * 20_000_000) > 1:
        raise ValueError("legacy OFDM decoder supports 20, 40, or 80 MSa/s")
    if ratio == 1:
        return samples
    half = 20 * ratio
    n = np.arange(-half, half + 1, dtype=np.float64)
    cutoff = 0.45 / ratio
    taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(n.size)
    taps /= taps.sum()
    filtered = np.convolve(samples, taps.astype(np.float32), mode="same")
    return filtered[::ratio].astype(np.complex64)


def _stf_runs(samples, threshold=0.62):
    if samples.size < 96:
        return []
    lag, window = 16, 64
    first = samples[:-lag]
    second = samples[lag:]
    cross = _sliding_sum(np.conj(first) * second, window)
    power_a = _sliding_sum(np.abs(first) ** 2, window)
    power_b = _sliding_sum(np.abs(second) ** 2, window)
    metric = np.abs(cross) / np.sqrt(power_a * power_b + 1e-20)
    indices = np.flatnonzero(metric >= threshold)
    if not indices.size:
        return []
    groups = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
    runs = []
    for group in groups:
        if group.size < 32:
            continue
        # Pick the most energetic valid 64-sample repeated window for CFO.
        local = group[np.argmax((power_a + power_b)[group])]
        runs.append((int(group[0]), int(group[-1]), int(local), cross[int(local)]))
    return runs


def _deinterleave(bits, n_cbps, n_bpsc):
    bits = np.asarray(bits)
    s = max(n_bpsc // 2, 1)
    first = np.array(
        [s * (j // s) + ((j + (16 * j) // n_cbps) % s) for j in range(n_cbps)]
    )
    second = np.array(
        [16 * i - (n_cbps - 1) * ((16 * i) // n_cbps) for i in range(n_cbps)]
    )
    permutation = second[first]
    output = np.empty_like(bits)
    output[permutation] = bits
    return output


def _branch_outputs(state, bit):
    register = ((state << 1) & 0x7E) | bit
    return (
        (register & 0o155).bit_count() & 1,
        (register & 0o117).bit_count() & 1,
    )


def _viterbi_decode(coded, force_final_zero=False):
    coded = np.asarray(coded, dtype=np.int8)
    if coded.size & 1:
        raise ValueError("convolutional stream must contain whole bit pairs")
    steps = coded.size // 2
    inf = 1 << 28
    metrics = np.full(64, inf, dtype=np.int32)
    metrics[0] = 0
    previous = np.zeros((steps, 64), dtype=np.uint8)
    chosen = np.zeros((steps, 64), dtype=np.uint8)
    for step in range(steps):
        received = coded[2 * step : 2 * step + 2]
        next_metrics = np.full(64, inf, dtype=np.int32)
        for state in range(64):
            if metrics[state] >= inf:
                continue
            for bit in (0, 1):
                expected = _branch_outputs(state, bit)
                branch = sum(
                    int(received[lane] >= 0 and received[lane] != expected[lane])
                    for lane in (0, 1)
                )
                next_state = ((state << 1) | bit) & 0x3F
                candidate = metrics[state] + branch
                if candidate < next_metrics[next_state]:
                    next_metrics[next_state] = candidate
                    previous[step, next_state] = state
                    chosen[step, next_state] = bit
        metrics = next_metrics
    state = 0 if force_final_zero else int(np.argmin(metrics))
    decoded = np.empty(steps, dtype=np.uint8)
    for step in range(steps - 1, -1, -1):
        decoded[step] = chosen[step, state]
        state = int(previous[step, state])
    return decoded, int(metrics[0 if force_final_zero else np.argmin(metrics)])


def _depuncture(bits, code_rate, output_length):
    if code_rate == "1/2":
        if len(bits) < output_length:
            raise ValueError("short coded payload")
        return np.asarray(bits[:output_length], dtype=np.int8)
    pattern = {
        "2/3": np.array((1, 1, 1, 0), dtype=bool),
        "3/4": np.array((1, 1, 1, 0, 0, 1), dtype=bool),
    }[code_rate]
    output = np.full(output_length, -1, dtype=np.int8)
    source = 0
    for index in range(output_length):
        if pattern[index % pattern.size]:
            if source >= len(bits):
                raise ValueError("short punctured payload")
            output[index] = bits[source]
            source += 1
    return output


def _constellation_table(n_bpsc):
    if n_bpsc == 1:
        return np.array((-1, 1), np.complex64)
    if n_bpsc == 2:
        return np.array((-1 - 1j, -1 + 1j, 1 - 1j, 1 + 1j), np.complex64) / math.sqrt(2)
    axis_bits = n_bpsc // 2
    levels = {2: (-3, -1, 3, 1), 3: (-7, -5, -1, -3, 7, 5, 1, 3)}[axis_bits]
    norm = math.sqrt(10 if n_bpsc == 4 else 42)
    return np.array(
        [
            levels[value & ((1 << axis_bits) - 1)]
            + 1j * levels[value >> axis_bits]
            for value in range(1 << n_bpsc)
        ],
        np.complex64,
    ) / norm


def _demap(values, n_bpsc):
    table = _constellation_table(n_bpsc)
    nearest = np.argmin(np.abs(np.asarray(values)[:, None] - table[None, :]) ** 2, axis=1)
    return ((nearest[:, None] >> np.arange(n_bpsc)) & 1).astype(np.uint8).reshape(-1)


def _equalized_symbol(samples, useful_start, channel, symbol_index):
    if useful_start < 0 or useful_start + 64 > samples.size:
        raise ValueError("capture ends inside OFDM symbol")
    frequency = np.fft.fftshift(np.fft.fft(samples[useful_start : useful_start + 64]))
    equalized = np.zeros(64, dtype=np.complex64)
    active = np.abs(LONG) > 0
    equalized[active] = frequency[active] / channel[active]
    pilot_indices = PILOT_SUBCARRIERS + 32
    expected = PILOT_VALUES * POLARITY[symbol_index % POLARITY.size]
    phase = np.angle(np.vdot(expected, equalized[pilot_indices]))
    equalized *= np.exp(-1j * phase)
    return equalized


def _descramble(bits):
    bits = np.asarray(bits, dtype=np.uint8)
    best = None
    for seed in range(1, 128):
        state = seed
        output = np.empty_like(bits)
        for index, bit in enumerate(bits):
            feedback = ((state >> 6) ^ (state >> 3)) & 1
            output[index] = int(bit) ^ feedback
            state = ((state << 1) & 0x7E) | feedback
        score = int(output[:16].sum())
        if best is None or score < best[0]:
            best = (score, output)
            if score == 0:
                break
    return best[1]


def _bits_to_bytes(bits):
    return np.packbits(np.asarray(bits, dtype=np.uint8), bitorder="little").tobytes()


def _mac_summary(psdu):
    if len(psdu) < 2:
        return None, None, False, None, ()
    frame_control = int.from_bytes(psdu[:2], "little")
    frame_type = (frame_control >> 2) & 3
    subtype = (frame_control >> 4) & 15
    retry = bool(frame_control & (1 << 11))
    address_count = 1 if frame_type == 1 else 3
    addresses = []
    for offset in (4, 10, 16, 24)[:address_count]:
        if offset + 6 <= len(psdu):
            addresses.append(":".join(f"{byte:02x}" for byte in psdu[offset : offset + 6]))
    sequence = int.from_bytes(psdu[22:24], "little") >> 4 if frame_type != 1 and len(psdu) >= 24 else None
    return frame_type, subtype, retry, sequence, tuple(addresses)


def _synchronize_and_decode_lsig(samples, run):
    run_start, run_end, cfo_index, cfo_cross = run
    coarse_omega = np.angle(cfo_cross) / 16.0
    corrected = samples * np.exp(-1j * coarse_omega * np.arange(samples.size))

    long_td = np.fft.ifft(np.fft.ifftshift(LONG)).astype(np.complex64)
    search_lo = max(0, run_start + 100)
    search_hi = min(corrected.size - 64, run_end + 220)
    if search_hi <= search_lo:
        raise ValueError("capture ends before LTF")
    section = corrected[search_lo : search_hi + 64]
    correlation = np.abs(np.correlate(section, long_td, mode="valid"))
    energy = _sliding_sum(np.abs(section) ** 2, 64)
    normalized = correlation / np.sqrt(energy * np.vdot(long_td, long_td).real + 1e-20)
    peak_local = int(np.argmax(normalized))
    if normalized[peak_local] < 0.58:
        raise ValueError("no LTF correlation peak")
    ltf_start = search_lo + peak_local
    earlier = peak_local - 64
    if earlier >= 0 and normalized[earlier] >= 0.82 * normalized[peak_local]:
        ltf_start -= 64
    if ltf_start + 128 > corrected.size:
        raise ValueError("capture ends inside LTF")

    first_ltf = corrected[ltf_start : ltf_start + 64]
    second_ltf = corrected[ltf_start + 64 : ltf_start + 128]
    fine_omega = np.angle(np.vdot(first_ltf, second_ltf)) / 64.0
    corrected *= np.exp(-1j * fine_omega * (np.arange(corrected.size) - ltf_start))
    first_ltf = corrected[ltf_start : ltf_start + 64]
    second_ltf = corrected[ltf_start + 64 : ltf_start + 128]
    channel = (np.fft.fftshift(np.fft.fft(first_ltf)) + np.fft.fftshift(np.fft.fft(second_ltf))) / 2
    active = np.abs(LONG) > 0
    channel[active] /= LONG[active]

    signal_equalized = _equalized_symbol(corrected, ltf_start + 144, channel, 0)
    signal_points = signal_equalized[DATA_SUBCARRIERS + 32]
    signal_interleaved = (signal_points.real >= 0).astype(np.uint8)
    signal_coded = _deinterleave(signal_interleaved, 48, 1)
    signal_bits, signal_errors = _viterbi_decode(signal_coded, force_final_zero=True)
    rate_field = sum(int(signal_bits[index]) << (3 - index) for index in range(4))
    if rate_field not in RATE_PARAMETERS:
        raise ValueError(f"unsupported SIGNAL rate field 0x{rate_field:x}")
    if signal_bits[4] != 0 or signal_bits[18:24].any():
        raise ValueError("invalid SIGNAL reserved/tail bits")
    if (int(signal_bits[:17].sum()) & 1) != int(signal_bits[17]):
        raise ValueError("SIGNAL parity failure")
    psdu_length = sum(int(signal_bits[index]) << (index - 5) for index in range(5, 17))
    if psdu_length < 4 or psdu_length > 4095:
        raise ValueError("invalid SIGNAL length")

    rate_mbps, n_bpsc, n_cbps, n_dbps, code_rate = RATE_PARAMETERS[rate_field]
    signal_power = float(np.mean(np.abs(signal_points) ** 2))
    signal_noise = float(
        np.mean(
            np.abs(signal_points - np.where(signal_points.real >= 0, 1, -1)) ** 2
        )
    )
    snr_db = 10 * math.log10(signal_power / max(signal_noise, 1e-12))
    return {
        "corrected": corrected,
        "ltf_start": ltf_start,
        "channel": channel,
        "coarse_omega": coarse_omega,
        "fine_omega": fine_omega,
        "signal_snr_db": snr_db,
        "rate_mbps": rate_mbps,
        "psdu_length": psdu_length,
        "n_bpsc": n_bpsc,
        "n_cbps": n_cbps,
        "n_dbps": n_dbps,
        "code_rate": code_rate,
    }


def _expected_lsig_bits(synchronized):
    rate_field = next(
        field
        for field, parameters in RATE_PARAMETERS.items()
        if parameters[0] == synchronized["rate_mbps"]
    )
    bits = np.zeros(24, dtype=np.uint8)
    for index in range(4):
        bits[index] = (rate_field >> (3 - index)) & 1
    for index in range(5, 17):
        bits[index] = (synchronized["psdu_length"] >> (index - 5)) & 1
    bits[17] = int(bits[:17].sum()) & 1
    return bits


def _decode_bpsk_signal_bits(synchronized, useful_start, symbol_index):
    equalized = _equalized_symbol(
        synchronized["corrected"],
        useful_start,
        synchronized["channel"],
        symbol_index,
    )
    points = equalized[DATA_SUBCARRIERS + 32]
    interleaved = (points.real >= 0).astype(np.uint8)
    coded = _deinterleave(interleaved, 48, 1)
    bits, _errors = _viterbi_decode(coded, force_final_zero=True)
    return bits


def _has_repeated_lsig(synchronized):
    """Validate the RL-SIG that identifies an HE/EHT-family preamble."""

    repeated = _decode_bpsk_signal_bits(
        synchronized,
        synchronized["ltf_start"] + 224,
        1,
    )
    return bool(np.array_equal(repeated, _expected_lsig_bits(synchronized)))


def _ht_sig_crc(bits):
    state = [1] * 8
    for bit in np.asarray(bits, dtype=np.uint8):
        feedback = state[7] ^ int(bit)
        old = state
        state = [
            feedback,
            old[0] ^ feedback,
            old[1] ^ feedback,
            old[2],
            old[3],
            old[4],
            old[5],
            old[6],
        ]
    return np.array([state[7 - index] ^ 1 for index in range(8)], dtype=np.uint8)


def _decode_ht_sig(synchronized):
    """Decode the two quadrature-BPSK HT-SIG symbols after L-SIG."""

    coded_parts = []
    for symbol in range(2):
        equalized = _equalized_symbol(
            synchronized["corrected"],
            synchronized["ltf_start"] + 224 + 80 * symbol,
            synchronized["channel"],
            symbol + 1,
        )
        points = equalized[DATA_SUBCARRIERS + 32]
        if float(np.mean(points.imag**2)) < 1.4 * float(np.mean(points.real**2)):
            raise ValueError("not quadrature-BPSK HT-SIG")
        interleaved = (points.imag >= 0).astype(np.uint8)
        coded_parts.append(_deinterleave(interleaved, 48, 1))
    bits, _errors = _viterbi_decode(np.concatenate(coded_parts), force_final_zero=True)
    if bits[42:48].any() or not np.array_equal(bits[34:42], _ht_sig_crc(bits[:34])):
        raise ValueError("HT-SIG CRC/tail failure")
    if bits[26] != 1:
        raise ValueError("HT-SIG reserved bit failure")
    return {
        "mcs_index": sum(int(bits[index]) << index for index in range(7)),
        "channel_width_mhz": 40 if bits[7] else 20,
        "psdu_length": sum(int(bits[index]) << (index - 8) for index in range(8, 24)),
        "stbc_streams": int(bits[28]) | (int(bits[29]) << 1),
        "short_guard_interval": bool(bits[31]),
    }


def _decode_at(samples, run, original_ratio):
    synchronized = _synchronize_and_decode_lsig(samples, run)
    # Mixed-format HT uses a valid 6-Mbit/s L-SIG as a compatibility duration
    # reservation. Treating its following HT-SIG/training/data as a legacy
    # PSDU produces plausible-looking random MAC headers with invalid FCS.
    try:
        _decode_ht_sig(synchronized)
    except (ValueError, IndexError, FloatingPointError):
        pass
    else:
        raise ValueError("HT mixed-format PPDU is not a legacy PSDU")
    if _has_repeated_lsig(synchronized):
        raise ValueError("HE/EHT-family PPDU is not a legacy PSDU")

    corrected = synchronized["corrected"]
    ltf_start = synchronized["ltf_start"]
    channel = synchronized["channel"]
    rate_mbps = synchronized["rate_mbps"]
    psdu_length = synchronized["psdu_length"]
    n_bpsc = synchronized["n_bpsc"]
    n_cbps = synchronized["n_cbps"]
    n_dbps = synchronized["n_dbps"]
    code_rate = synchronized["code_rate"]
    n_symbols = math.ceil((16 + 8 * psdu_length + 6) / n_dbps)
    received_coded = []
    first_data_useful = ltf_start + 224
    for symbol in range(n_symbols):
        equalized = _equalized_symbol(
            corrected, first_data_useful + 80 * symbol, channel, symbol + 1
        )
        mapped_bits = _demap(equalized[DATA_SUBCARRIERS + 32], n_bpsc)
        received_coded.append(_deinterleave(mapped_bits, n_cbps, n_bpsc))
    received_coded = np.concatenate(received_coded)
    n_data = n_symbols * n_dbps
    depunctured = _depuncture(received_coded, code_rate, 2 * n_data)
    scrambled, _data_errors = _viterbi_decode(depunctured)
    data = _descramble(scrambled)
    psdu = _bits_to_bytes(data[16 : 16 + 8 * psdu_length])
    fcs_ok = (
        len(psdu) >= 4
        and (binascii.crc32(psdu[:-4]) & 0xFFFFFFFF)
        == int.from_bytes(psdu[-4:], "little")
    )
    frame_type, subtype, retry, sequence, addresses = _mac_summary(psdu)
    return LegacyOFDMFrame(
        sample_offset=int(max(0, ltf_start - 192) * original_ratio),
        rate_mbps=rate_mbps,
        psdu_length=psdu_length,
        psdu=psdu,
        fcs_ok=fcs_ok,
        coarse_cfo_hz=float(synchronized["coarse_omega"] * 20_000_000 / (2 * math.pi)),
        fine_cfo_hz=float(synchronized["fine_omega"] * 20_000_000 / (2 * math.pi)),
        signal_snr_db=synchronized["signal_snr_db"],
        frame_type=frame_type,
        frame_subtype=subtype,
        retry=retry,
        sequence_number=sequence,
        addresses=addresses,
    )


def decode_wifi_ofdm_headers_all(samples, sample_rate_hz):
    """Decode L-SIG plus mixed-format HT-SIG or HE/EHT repeated L-SIG."""

    ratio = int(round(float(sample_rate_hz) / 20_000_000.0))
    baseband = _to_20_msps(samples, sample_rate_hz)
    headers = []
    occupied_until = -1
    for run in _stf_runs(baseband):
        if run[0] < occupied_until:
            continue
        try:
            synchronized = _synchronize_and_decode_lsig(baseband, run)
        except (ValueError, IndexError, FloatingPointError):
            continue
        ht = None
        try:
            ht = _decode_ht_sig(synchronized)
        except (ValueError, IndexError, FloatingPointError):
            pass
        repeated_lsig = False
        try:
            repeated_lsig = _has_repeated_lsig(synchronized)
        except (ValueError, IndexError, FloatingPointError):
            pass
        headers.append(
            WiFiOFDMHeader(
                sample_offset=int(max(0, synchronized["ltf_start"] - 192) * ratio),
                phy_format=(
                    "HE/EHT family"
                    if repeated_lsig
                    else "HT mixed"
                    if ht is not None
                    else "legacy-compatible OFDM"
                ),
                legacy_rate_mbps=synchronized["rate_mbps"],
                legacy_length=synchronized["psdu_length"],
                coarse_cfo_hz=float(synchronized["coarse_omega"] * 20_000_000 / (2 * math.pi)),
                fine_cfo_hz=float(synchronized["fine_omega"] * 20_000_000 / (2 * math.pi)),
                signal_snr_db=synchronized["signal_snr_db"],
                **(ht or {}),
            )
        )
        occupied_until = synchronized["ltf_start"] + (400 if ht is not None else 240)
    return headers


def decode_legacy_ofdm_all(samples, sample_rate_hz):
    """Decode every legacy OFDM PPDU found in one capture.

    Invalid candidates are skipped.  An empty list is therefore the normal
    result for captures containing noise, 11b, HT-only, or truncated frames.
    """

    ratio = int(round(float(sample_rate_hz) / 20_000_000.0))
    baseband = _to_20_msps(samples, sample_rate_hz)
    frames = []
    occupied_until = -1
    for run in _stf_runs(baseband):
        if run[0] < occupied_until:
            continue
        try:
            frame = _decode_at(baseband, run, ratio)
        except (ValueError, IndexError, FloatingPointError):
            continue
        frames.append(frame)
        data_samples = math.ceil((16 + 8 * frame.psdu_length + 6) / RATE_PARAMETERS[
            next(key for key, value in RATE_PARAMETERS.items() if value[0] == frame.rate_mbps)
        ][3]) * 80
        occupied_until = frame.sample_offset // ratio + 400 + data_samples
    return frames


def decode_legacy_ofdm(samples, sample_rate_hz):
    """Return the first decoded legacy OFDM frame, or ``None``."""

    frames = decode_legacy_ofdm_all(samples, sample_rate_hz)
    return frames[0] if frames else None
