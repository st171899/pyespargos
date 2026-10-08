"""Protocol-independent burst detection and feature extraction."""

from __future__ import annotations

import math

import numpy as np

from .model import SignalFeatures, SignalRegion

ADC_FULL_SCALE = 512.0


def _moving_mean(values: np.ndarray, length: int) -> np.ndarray:
    length = max(1, min(int(length), values.size))
    sums = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
    means = (sums[length:] - sums[:-length]) / length
    if length == 1:
        return means
    left = length // 2
    return np.pad(means, (left, values.size - means.size - left), mode="edge")


class EnergyBurstDetector:
    """Detect bursts from diversity-combined, noise-normalized array power.

    Thresholds are relative to each sensor's own quiet-level estimate. This is
    intentionally independent of the firmware's absolute trigger threshold:
    that threshold decides which banks to retain, while this detector locates
    packet boundaries inside an already retained array capture.
    """

    def __init__(
        self,
        threshold_db: float = 7.0,
        minimum_duration_us: float = 1.5,
        merge_gap_us: float = 1.0,
        required_silence_us: float = 2.0,
    ):
        self.threshold_db = float(threshold_db)
        self.minimum_duration_us = float(minimum_duration_us)
        self.merge_gap_us = float(merge_gap_us)
        self.required_silence_us = float(required_silence_us)

    def detect(self, signals: np.ndarray, sample_rate_hz: float) -> list[SignalRegion]:
        signals = np.asarray(signals)
        if signals.ndim == 1:
            signals = signals[np.newaxis, :]
        if signals.ndim != 2 or signals.shape[1] < 8:
            return []

        powers = np.abs(signals) ** 2
        noise = np.quantile(powers, 0.2, axis=1)
        noise = np.maximum(noise, 1.0)
        relative = powers / noise[:, None]
        diversity_power = np.max(relative, axis=0)
        # Taking the maximum provides spatial diversity, but it also raises the
        # quiet distribution as sensor count grows. Normalize that combined
        # statistic once more so the dB threshold retains its intended meaning.
        diversity_noise = max(float(np.quantile(diversity_power, 0.3)), 1e-12)
        diversity_power /= diversity_noise
        smooth = _moving_mean(diversity_power, round(sample_rate_hz * 0.50e-6))
        active = smooth >= 10.0 ** (self.threshold_db / 10.0)
        indices = np.flatnonzero(active)
        if indices.size == 0:
            return []

        merge_gap = max(1, round(sample_rate_hz * self.merge_gap_us * 1e-6))
        groups = np.split(indices, np.flatnonzero(np.diff(indices) > merge_gap) + 1)
        minimum = max(1, round(sample_rate_hz * self.minimum_duration_us * 1e-6))
        silence = max(1, round(sample_rate_hz * self.required_silence_us * 1e-6))
        regions = []
        absolute_power = np.max(powers, axis=0)
        for group in groups:
            start = max(0, int(group[0]) - merge_gap // 2)
            end = min(signals.shape[1], int(group[-1]) + 1 + merge_gap // 2)
            if end - start < minimum:
                continue
            quiet_start = max(0, start - silence)
            clean_onset = start >= silence and not np.any(active[quiet_start:start])
            peak = float(np.max(absolute_power[start:end]))
            local_noise = float(np.median(absolute_power[quiet_start:start])) if start > quiet_start else float(np.median(absolute_power))
            regions.append(
                SignalRegion(
                    start_sample=start,
                    end_sample=end,
                    peak_dbfs=10.0 * math.log10(max(peak, 1e-12) / ADC_FULL_SCALE**2),
                    noise_dbfs=10.0 * math.log10(max(local_noise, 1e-12) / ADC_FULL_SCALE**2),
                    clean_onset=clean_onset,
                )
            )
        return regions


def _normalized_lag_correlation(samples: np.ndarray, lag: int) -> float:
    if lag <= 0 or samples.size < 3 * lag:
        return 0.0
    first = samples[:-lag]
    second = samples[lag:]
    numerator = abs(np.vdot(first, second))
    denominator = math.sqrt(float(np.vdot(first, first).real * np.vdot(second, second).real))
    return float(numerator / max(denominator, 1e-20))


def extract_features(samples: np.ndarray, sample_rate_hz: float, region: SignalRegion) -> SignalFeatures:
    """Measure bandwidth, center, envelope, OFDM repetition, and FM shape."""

    samples = np.asarray(samples, dtype=np.complex64).reshape(-1)
    segment = samples[region.start_sample : region.end_sample]
    segment = segment - np.mean(segment)
    if segment.size < 8:
        return SignalFeatures(0.0, sample_rate_hz, 1.0, 1.0, 0.0, 0.0)

    nfft = 1 << min(14, max(8, int(math.ceil(math.log2(segment.size)))))
    if segment.size > nfft:
        offset = (segment.size - nfft) // 2
        spectrum_input = segment[offset : offset + nfft]
    else:
        spectrum_input = np.pad(segment, (0, nfft - segment.size))
    window = np.hanning(spectrum_input.size).astype(np.float32)
    power = np.abs(np.fft.fftshift(np.fft.fft(spectrum_input * window))) ** 2
    floor = float(np.quantile(power, 0.15))
    power = np.maximum(power - floor, 0.0)
    frequencies = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / sample_rate_hz))
    total = float(power.sum())
    if total <= 0:
        center = 0.0
        bandwidth = sample_rate_hz
        flatness = 1.0
    else:
        cumulative = np.cumsum(power) / total
        lo = int(np.searchsorted(cumulative, 0.025))
        hi = min(nfft - 1, int(np.searchsorted(cumulative, 0.975)))
        bandwidth = float(max(sample_rate_hz / nfft, frequencies[hi] - frequencies[lo]))
        center = float(np.sum(frequencies * power) / total)
        positive = power[power > 0]
        flatness = float(np.exp(np.mean(np.log(positive + 1e-30))) / max(np.mean(positive), 1e-30)) if positive.size else 1.0

    envelope = np.abs(segment)
    envelope_variation = float(np.std(envelope) / max(np.mean(envelope), 1e-12))
    repeat_lag = max(1, round(sample_rate_hz * 0.8e-6))
    repeat_window = segment[: min(segment.size, round(sample_rate_hz * 10e-6))]
    ofdm_score = _normalized_lag_correlation(repeat_window, repeat_lag)

    phase_steps = np.angle(segment[1:] * np.conj(segment[:-1]))
    phase_steps -= np.median(phase_steps)
    fm_scale = float(np.sqrt(np.mean(np.square(phase_steps))))
    binary_fm_score = float(np.mean(np.abs(phase_steps)) / max(fm_scale, 1e-12))
    return SignalFeatures(center, bandwidth, flatness, envelope_variation, ofdm_score, binary_fm_score)
