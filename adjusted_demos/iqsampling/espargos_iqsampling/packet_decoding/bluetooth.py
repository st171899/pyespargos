"""Bluetooth packet classifiers and decoders."""

from __future__ import annotations

import numpy as np

from .base import ProtocolDecoder
from .model import ProtocolMatch


def _ble_channel_for_frequency(frequency_hz: float) -> tuple[int, float]:
    candidates = [(37, 2_402_000_000.0), (38, 2_426_000_000.0), (39, 2_480_000_000.0)]
    candidates.extend((channel, 2_404_000_000.0 + 2_000_000.0 * channel) for channel in range(11))
    candidates.extend((channel, 2_428_000_000.0 + 2_000_000.0 * (channel - 11)) for channel in range(11, 37))
    return min(candidates, key=lambda item: abs(item[1] - frequency_hz))


def _ble_expected_bits(preamble_bits: int) -> np.ndarray:
    access_address = 0x8E89BED6
    aa = np.array([(access_address >> bit) & 1 for bit in range(32)], dtype=np.int8)
    first = int(aa[0])
    preamble = np.array([(first + bit) & 1 for bit in range(preamble_bits)], dtype=np.int8)
    return np.concatenate((preamble, aa))


def _spectral_center(samples: np.ndarray, sample_rate_hz: float) -> tuple[float, float]:
    count = min(8192, samples.size)
    if count < 32:
        return 0.0, sample_rate_hz
    offset = (samples.size - count) // 2
    windowed = samples[offset : offset + count] * np.hanning(count)
    power = np.abs(np.fft.fftshift(np.fft.fft(windowed))) ** 2
    frequencies = np.fft.fftshift(np.fft.fftfreq(count, 1.0 / sample_rate_hz))
    peak = int(np.argmax(power))
    half_width = max(2, round(1_500_000 * count / sample_rate_hz))
    lo, hi = max(0, peak - half_width), min(count, peak + half_width + 1)
    local = power[lo:hi]
    center = float(np.sum(frequencies[lo:hi] * local) / max(float(local.sum()), 1e-20))
    cumulative = np.cumsum(local) / max(float(local.sum()), 1e-20)
    blo = int(np.searchsorted(cumulative, 0.025))
    bhi = min(local.size - 1, int(np.searchsorted(cumulative, 0.975)))
    return center, float(max(sample_rate_hz / count, frequencies[lo + bhi] - frequencies[lo + blo]))


class BluetoothLEAdvertisingDecoder(ProtocolDecoder):
    """Recognize the fixed BLE advertising access address on 1M/2M PHYs."""

    name = "Bluetooth LE access-address detector"
    target_sample_rate_hz = 10_000_000

    @staticmethod
    def _channelize(samples, sample_rate_hz, center_offset_hz):
        ratio = int(round(sample_rate_hz / BluetoothLEAdvertisingDecoder.target_sample_rate_hz))
        if ratio not in (2, 4, 8) or abs(sample_rate_hz / ratio - BluetoothLEAdvertisingDecoder.target_sample_rate_hz) > 1:
            return None
        index = np.arange(samples.size)
        shifted = samples * np.exp(-2j * np.pi * center_offset_hz * index / sample_rate_hz)
        taps_index = np.arange(-32, 33, dtype=np.float64)
        cutoff = 1_350_000.0 / sample_rate_hz
        taps = 2 * cutoff * np.sinc(2 * cutoff * taps_index) * np.hamming(taps_index.size)
        taps /= taps.sum()
        return np.convolve(shifted, taps, mode="same")[::ratio].astype(np.complex64)

    @staticmethod
    def _find_access(samples, bitrate):
        sample_rate = BluetoothLEAdvertisingDecoder.target_sample_rate_hz
        samples_per_bit = round(sample_rate / bitrate)
        expected = _ble_expected_bits(16 if bitrate == 2_000_000 else 8)
        expected_pm = 2.0 * expected - 1.0
        discriminator = np.angle(samples[1:] * np.conj(samples[:-1]))
        discriminator -= np.median(discriminator)
        best = None
        for phase in range(samples_per_bit):
            usable = (discriminator.size - phase) // samples_per_bit
            if usable < expected.size:
                continue
            soft = discriminator[phase : phase + usable * samples_per_bit].reshape(usable, samples_per_bit).mean(axis=1)
            norms = np.sqrt(np.convolve(soft**2, np.ones(expected.size), mode="valid") * float(np.sum(expected_pm**2)))
            correlation = np.correlate(soft, expected_pm, mode="valid") / np.maximum(norms, 1e-12)
            for polarity in (1.0, -1.0):
                index = int(np.argmax(polarity * correlation))
                score = float(polarity * correlation[index])
                if best is None or score > best[0]:
                    best = (score, phase + index * samples_per_bit, polarity)
        return best

    def decode(self, samples, sample_rate_hz, center_frequency_hz, regions):
        if sample_rate_hz not in (20_000_000, 40_000_000, 80_000_000):
            return []
        results = []
        pad = round(sample_rate_hz * 8e-6)
        for region in regions:
            section_start = max(0, region.start_sample - pad)
            section_end = min(len(samples), region.end_sample + pad)
            section = samples[section_start:section_end]
            center_offset, bandwidth = _spectral_center(section, sample_rate_hz)
            if not 250_000 <= bandwidth <= 4_000_000:
                continue
            channelized = self._channelize(section, sample_rate_hz, center_offset)
            if channelized is None:
                continue
            best = None
            for bitrate in (1_000_000, 2_000_000):
                found = self._find_access(channelized, bitrate)
                if found is not None and (best is None or found[0] > best[0]):
                    best = (*found, bitrate)
            if best is None or best[0] < 0.68:
                continue
            score, access_start_10m, _polarity, bitrate = best
            original_start = section_start + round(access_start_10m * sample_rate_hz / self.target_sample_rate_hz)
            absolute_frequency = center_frequency_hz + center_offset
            channel, nominal_frequency = _ble_channel_for_frequency(absolute_frequency)
            if abs(nominal_frequency - absolute_frequency) > 1_250_000:
                continue
            advertising = channel >= 37
            results.append(
                ProtocolMatch(
                    protocol=f"Bluetooth LE {'2M' if bitrate == 2_000_000 else '1M'}",
                    decoder=self.name,
                    status="header detected",
                    confidence=min(0.99, score),
                    start_sample=max(0, original_start),
                    end_sample=region.end_sample,
                    summary=f"advertising access address · channel {channel}" if advertising else f"advertising access address near data channel {channel}",
                    bitrate=f"{bitrate / 1e6:g} Mbit/s",
                    center_offset_hz=center_offset,
                    bandwidth_hz=bandwidth,
                    fields={
                        "access address": "8e89bed6",
                        "channel": channel,
                        "frequency MHz": round(absolute_frequency / 1e6, 3),
                        "correlation": round(score, 3),
                        "clean onset": region.clean_onset,
                    },
                )
            )
        return results
