"""Built-in protocol decoders for the packet-decoding pipeline."""

from __future__ import annotations

import math

import numpy as np

from ..wifi_legacy import decode_legacy_ofdm_all, decode_wifi_ofdm_headers_all
from .base import ProtocolDecoder
from .detection import extract_features
from .model import ProtocolMatch, SignalRegion

WIFI_FRAME_TYPES = {0: "Management", 1: "Control", 2: "Data", 3: "Extension"}
WIFI_SUBTYPES = {
    (0, 0): "Association request",
    (0, 1): "Association response",
    (0, 4): "Probe request",
    (0, 5): "Probe response",
    (0, 8): "Beacon",
    (0, 10): "Disassociation",
    (0, 11): "Authentication",
    (0, 12): "Deauthentication",
    (1, 8): "Block ACK request",
    (1, 9): "Block ACK",
    (1, 11): "RTS",
    (1, 12): "CTS",
    (1, 13): "ACK",
}
WIFI_N_DBPS = {6: 24, 9: 36, 12: 48, 18: 72, 24: 96, 36: 144, 48: 192, 54: 216}
BARKER = np.array((1, -1, 1, 1, -1, 1, 1, 1, -1, -1, -1), dtype=np.complex64)


class LegacyOFDMDecoder(ProtocolDecoder):
    """IEEE 802.11a/g legacy OFDM PLCP, PSDU, MAC, and FCS decoder."""

    name = "802.11 legacy OFDM"

    @staticmethod
    def _channel_candidates(samples, sample_rate_hz, center_frequency_hz, regions):
        if 2_300_000_000 <= center_frequency_hz <= 2_600_000_000:
            channel_centers = [2_412_000_000 + 5_000_000 * index for index in range(13)]
            channel_centers.append(2_484_000_000)
        else:
            first = math.ceil((center_frequency_hz - sample_rate_hz / 2) / 5_000_000) * 5_000_000
            channel_centers = list(range(int(first), int(center_frequency_hz + sample_rate_hz / 2) + 1, 5_000_000))
        usable = [frequency for frequency in channel_centers if abs(frequency - center_frequency_hz) <= sample_rate_hz / 2 - 8_000_000]
        candidates = set()
        for region in regions:
            features = extract_features(samples, sample_rate_hz, region)
            estimate = center_frequency_hz + features.center_offset_hz
            if usable:
                # A spectral centroid is only a coarse channel hint.  Partial
                # captures, overlapping traffic, clipping, and the asymmetric
                # spectrum of an individual OFDM payload can bias it by one
                # 5 MHz channel.  Header validation is an excellent rejector,
                # so try the nearest center and its two immediate neighbours
                # instead of turning that approximate measurement into a hard
                # decision.
                candidates.update(sorted(usable, key=lambda frequency: abs(frequency - estimate))[:3])
        if not candidates:
            candidates.add(center_frequency_hz)
        return sorted(candidates)

    def decode(self, samples, sample_rate_hz, center_frequency_hz, regions):
        if sample_rate_hz not in (20_000_000, 40_000_000, 80_000_000):
            return []
        results = []
        sample_index = np.arange(len(samples))
        frames_with_channels = []
        headers_with_channels = []
        for channel_frequency in self._channel_candidates(samples, sample_rate_hz, center_frequency_hz, regions):
            physical_offset = channel_frequency - center_frequency_hz
            for orientation, oriented_offset in (
                (samples, physical_offset),
                (np.conj(samples), -physical_offset),
            ):
                shifted = orientation * np.exp(-2j * np.pi * oriented_offset * sample_index / sample_rate_hz)
                for frame in decode_legacy_ofdm_all(shifted, sample_rate_hz):
                    # A failed MPDU FCS means the SIGNAL header was decoded,
                    # not that the random-looking MAC fields are trustworthy.
                    # The header path below reports that weaker fact without
                    # leaking fabricated addresses/types into the packet list.
                    if frame.fcs_ok:
                        frames_with_channels.append((frame, channel_frequency, physical_offset))
                for header in decode_wifi_ofdm_headers_all(shifted, sample_rate_hz):
                    headers_with_channels.append((header, channel_frequency, physical_offset))
        for frame, channel_frequency, physical_offset in frames_with_channels:
            n_symbols = math.ceil((16 + 8 * frame.psdu_length + 6) / WIFI_N_DBPS[frame.rate_mbps])
            duration_samples = round(sample_rate_hz * (20 + 4 * n_symbols) * 1e-6)
            frame_type = WIFI_FRAME_TYPES.get(frame.frame_type, "Unknown")
            subtype = WIFI_SUBTYPES.get((frame.frame_type, frame.frame_subtype), str(frame.frame_subtype))
            addresses = list(frame.addresses)
            address_summary = ""
            if addresses:
                address_summary = f" · {addresses[1] if len(addresses) > 1 else '?'} → {addresses[0]}"
            summary = f"{frame_type} / {subtype} · {frame.psdu_length} B{address_summary}"
            results.append(
                ProtocolMatch(
                    protocol="IEEE 802.11 legacy OFDM",
                    decoder=self.name,
                    status="decoded",
                    confidence=1.0,
                    start_sample=frame.sample_offset,
                    end_sample=min(len(samples), frame.sample_offset + duration_samples),
                    summary=summary,
                    bitrate=f"{frame.rate_mbps} Mbit/s",
                    center_offset_hz=float(physical_offset),
                    bandwidth_hz=20_000_000.0,
                    fields={
                        "FCS": "valid",
                        "frame type": frame_type,
                        "subtype": subtype,
                        "length": frame.psdu_length,
                        "sequence": frame.sequence_number,
                        "source": addresses[1] if len(addresses) > 1 else None,
                        "destination": addresses[0] if addresses else None,
                        "center frequency MHz": channel_frequency / 1e6,
                        "CFO Hz": round(frame.coarse_cfo_hz + frame.fine_cfo_hz),
                        "SIGNAL SNR dB": round(frame.signal_snr_db, 1),
                    },
                    payload=frame.psdu,
                )
            )
        for header, channel_frequency, physical_offset in headers_with_channels:
            if any(frame_channel == channel_frequency and abs(frame.sample_offset - header.sample_offset) <= round(sample_rate_hz * 0.5e-6) for frame, frame_channel, _ in frames_with_channels):
                continue
            is_ht = header.phy_format == "HT mixed"
            is_he_eht = header.phy_format == "HE/EHT family"
            if is_he_eht:
                protocol = "IEEE 802.11ax/be HE/EHT"
                bitrate = "HE/EHT"
                summary = (
                    f"validated repeated L-SIG · compatibility length "
                    f"{header.legacy_length}"
                )
            elif is_ht:
                protocol = "IEEE 802.11n HT"
                bitrate = f"MCS {header.mcs_index}"
                summary = f"HT{header.channel_width_mhz} · MCS {header.mcs_index} · " f"{header.psdu_length} B · " f"{'short' if header.short_guard_interval else 'long'} GI"
            else:
                protocol = "IEEE 802.11 OFDM"
                bitrate = f"{header.legacy_rate_mbps} Mbit/s"
                summary = f"validated L-SIG · {header.legacy_length} B; later PHY header not decoded"
            minimum_duration = 36e-6 if is_ht else 24e-6 if is_he_eht else 20e-6
            results.append(
                ProtocolMatch(
                    protocol=protocol,
                    decoder=self.name,
                    status="PHY header decoded",
                    confidence=0.99 if is_ht or is_he_eht else 0.9,
                    start_sample=header.sample_offset,
                    end_sample=min(
                        len(samples),
                        header.sample_offset + round(sample_rate_hz * minimum_duration),
                    ),
                    summary=summary,
                    bitrate=bitrate,
                    center_offset_hz=float(physical_offset),
                    bandwidth_hz=float((header.channel_width_mhz or 20) * 1_000_000),
                    fields={
                        "PHY format": header.phy_format,
                        "MCS": header.mcs_index,
                        "channel width MHz": header.channel_width_mhz,
                        "length": header.psdu_length or header.legacy_length,
                        "STBC streams": header.stbc_streams,
                        "short GI": header.short_guard_interval,
                        "center frequency MHz": channel_frequency / 1e6,
                        "CFO Hz": round(header.coarse_cfo_hz + header.fine_cfo_hz),
                        "SIGNAL SNR dB": round(header.signal_snr_db, 1),
                    },
                )
            )
        return results


def _dsss_known_scrambled_preamble():
    history = [1] * 7
    output = []
    for _ in range(128):
        coded = 1 ^ history[-4] ^ history[-7]
        output.append(coded)
        history.append(coded)
    return np.asarray(output, dtype=np.uint8)


def _dsss_descramble(coded):
    history = [1] * 7
    output = np.empty(len(coded), dtype=np.uint8)
    for index, bit in enumerate(coded):
        output[index] = int(bit) ^ history[-4] ^ history[-7]
        history.append(int(bit))
    return output


def _dsss_plcp_crc(bits):
    state = [1] * 16
    for bit in bits:
        feedback = state[15] ^ int(bit)
        old = state
        state = [
            feedback,
            old[0],
            old[1],
            old[2],
            old[3],
            old[4] ^ feedback,
            old[5],
            old[6],
            old[7],
            old[8],
            old[9],
            old[10],
            old[11] ^ feedback,
            old[12],
            old[13],
            old[14],
        ]
    complemented = [bit ^ 1 for bit in state]
    return np.asarray(
        [complemented[15 - index] for index in range(8)] + [complemented[7 - index] for index in range(8)],
        dtype=np.uint8,
    )


class WiFiDSSSDecoder(ProtocolDecoder):
    """Recognize the long 802.11b preamble and decode its PLCP header."""

    name = "802.11b long-preamble DSSS"
    _target_sample_rate = 22_000_000
    _known_preamble = _dsss_known_scrambled_preamble()

    @classmethod
    def _resample(cls, samples, sample_rate_hz):
        if sample_rate_hz > cls._target_sample_rate:
            taps_index = np.arange(-32, 33, dtype=np.float64)
            cutoff = 9_500_000.0 / sample_rate_hz
            taps = 2 * cutoff * np.sinc(2 * cutoff * taps_index) * np.hamming(taps_index.size)
            taps /= taps.sum()
            samples = np.convolve(samples, taps, mode="same")
        output_count = int(len(samples) * cls._target_sample_rate / sample_rate_hz)
        positions = np.arange(output_count, dtype=np.float64) * sample_rate_hz / cls._target_sample_rate
        source = np.arange(len(samples), dtype=np.float64)
        return (np.interp(positions, source, samples.real) + 1j * np.interp(positions, source, samples.imag)).astype(np.complex64)

    @classmethod
    def _find_long_preamble(cls, samples, approximate_start, despread=None):
        lo = max(0, approximate_start - 44)
        hi = min(len(samples) - 22 * 32, approximate_start + 66)
        best = None
        if despread is None:
            matched_filter = np.zeros(21, dtype=np.complex64)
            matched_filter[::2] = BARKER[::-1]
            despread = np.convolve(samples, matched_filter, mode="valid")
        for start in range(lo, max(lo, hi)):
            bit_count = min(4096, (len(despread) - start + 21) // 22)
            if bit_count < 32:
                continue
            symbols = despread[start + 22 * np.arange(bit_count)]
            differential = symbols[1 : min(128, bit_count)] * np.conj(symbols[: min(128, bit_count) - 1])
            magnitude = np.abs(differential)
            valid = magnitude >= np.quantile(magnitude, 0.2)
            if np.count_nonzero(valid) < 24:
                continue
            expected_sign = np.where(cls._known_preamble[1 : min(128, bit_count)] != 0, -1.0, 1.0)
            aligned = differential[valid] * expected_sign[valid]
            score = float(abs(np.sum(aligned)) / max(float(np.sum(np.abs(aligned))), 1e-20))
            if best is None or score > best[0] + 0.01 or (score >= best[0] - 0.01 and abs(start - approximate_start) < abs(best[1] - approximate_start)):
                best = (score, start, symbols, float(np.angle(np.sum(aligned))))
        return best

    def decode(self, samples, sample_rate_hz, center_frequency_hz, regions):
        if sample_rate_hz not in (20_000_000, 40_000_000, 80_000_000):
            return []
        results = []
        sample_index = np.arange(len(samples))
        for channel_frequency in LegacyOFDMDecoder._channel_candidates(samples, sample_rate_hz, center_frequency_hz, regions):
            physical_offset = channel_frequency - center_frequency_hz
            shifted = samples * np.exp(-2j * np.pi * physical_offset * sample_index / sample_rate_hz)
            resampled = self._resample(shifted, sample_rate_hz)
            matched_filter = np.zeros(21, dtype=np.complex64)
            matched_filter[::2] = BARKER[::-1]
            despread = np.convolve(resampled, matched_filter, mode="valid")
            for region in regions:
                approximate = round(region.start_sample * self._target_sample_rate / sample_rate_hz)
                found = self._find_long_preamble(resampled, approximate, despread)
                if found is None or found[0] < 0.78:
                    continue
                score, start_22m, symbols, differential_cfo = found
                coded = np.empty(len(symbols), dtype=np.uint8)
                coded[0] = self._known_preamble[0]
                corrected_differential = symbols[1:] * np.conj(symbols[:-1]) * np.exp(-1j * differential_cfo)
                coded[1:] = (corrected_differential.real < 0).astype(np.uint8)
                serial = _dsss_descramble(coded)
                preamble_errors = int(np.count_nonzero(serial[: min(128, len(serial))] != 1))
                if preamble_errors > max(2, len(serial[:128]) // 20):
                    continue
                start_sample = round(start_22m * sample_rate_hz / self._target_sample_rate)
                status = "long preamble detected"
                summary = f"validated long DSSS preamble · correlation {score:.2f}"
                bitrate = "1 Mchip header"
                fields = {
                    "preamble correlation": round(score, 3),
                    "preamble bit errors": preamble_errors,
                    "CFO Hz": round(differential_cfo / (2 * np.pi * 1e-6)),
                }
                if len(serial) >= 192:
                    expected_sfd = np.unpackbits(
                        np.frombuffer((0xF3A0).to_bytes(2, "little"), dtype=np.uint8),
                        bitorder="little",
                    )
                    header_bits = serial[144:176]
                    crc_bits = serial[176:192]
                    if np.array_equal(serial[128:144], expected_sfd) and np.array_equal(crc_bits, _dsss_plcp_crc(header_bits)):
                        header = np.packbits(header_bits, bitorder="little").tobytes()
                        rate_code = header[0]
                        rate = {0x0A: 1, 0x14: 2, 0x37: 5.5, 0x6E: 11}.get(rate_code)
                        length_us = int.from_bytes(header[2:4], "little")
                        status = "PLCP header decoded"
                        bitrate = f"{rate:g} Mbit/s" if rate is not None else f"rate code 0x{rate_code:02x}"
                        summary = f"long preamble · {bitrate} · payload duration {length_us} µs"
                        fields.update({"rate code": rate_code, "rate Mbit/s": rate, "payload duration us": length_us})
                results.append(
                    ProtocolMatch(
                        protocol="IEEE 802.11b DSSS/CCK",
                        decoder=self.name,
                        status=status,
                        confidence=min(0.99, score),
                        start_sample=start_sample,
                        end_sample=region.end_sample,
                        summary=summary,
                        bitrate=bitrate,
                        center_offset_hz=float(physical_offset),
                        bandwidth_hz=22_000_000.0,
                        fields=fields,
                    )
                )
        return results
