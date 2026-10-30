"""Uniform slow-time pulse radar processing (same FFT/sign convention as MIMO).

Times are unwrapped source chunk indices, never host arrival times. Accumulated
vectors contribute lane zero once per window; other lanes are boxcar averages,
not additional consecutive slow-time measurements.
"""
from collections import deque
import numpy as np

C = 299792458.0


class DopplerHistory:
    def __init__(self, size=64, *, uniform=True):
        self.size = int(size)
        self.uniform = bool(uniform)
        self.frames = deque(maxlen=self.size)
        self.indices = deque(maxlen=self.size)
        self.stride = None

    def clear(self):
        self.frames.clear()
        self.indices.clear()
        self.stride = None

    def append(self, index, frame, stride):
        frame = np.asarray(frame, dtype=np.complex64)
        finite = np.isfinite(frame)
        if not finite.any() or (self.uniform and not finite.all()):
            return False
        if self.indices and index <= self.indices[-1]:
            if not self.uniform and index == self.indices[-1] and frame.shape == self.frames[-1].shape:
                added = finite & ~np.isfinite(self.frames[-1])
                if added.any():
                    self.frames[-1] = np.where(added, frame, self.frames[-1])
                    return True
            return False
        if self.indices and (frame.shape != self.frames[-1].shape or
                             (self.uniform and (stride != self.stride or index - self.indices[-1] != stride))):
            self.clear()  # A gap must never masquerade as contiguous slow time.
        self.stride = int(stride)
        self.indices.append(int(index))
        self.frames.append(frame.copy())
        return True

    @property
    def ready(self):
        return len(self.frames) == self.size

    def residual(self):
        """Remove the complex DC component over the current coherent interval."""
        data = np.asarray(self.frames)
        return data - data.mean(axis=0, keepdims=True)

    def mean_frame(self):
        """Mean raw-IQ period, excluding missing antenna/sample observations."""
        data = np.asarray(self.frames)
        valid = np.isfinite(data)
        count = valid.sum(axis=0)
        mean = np.divide(np.where(valid, data, 0).sum(axis=0), count,
                         out=np.zeros(data.shape[1:], np.complex64), where=count > 0)
        return mean

    def latest_residual(self):
        return self.frames[-1] - self.mean_frame()

    def spectrum(self, sample_rate, frequency, suppress=False, allow_partial=False):
        """Return ascending equivalent radial velocities and complex [velocity, RX, delay].

        Hann weighting and 2x FFT padding follow the MIMO radar. Padding only
        interpolates the spectrum; independent bin spacing is 1/(N*dt).
        With allow_partial, show a coarse estimate after eight measurements
        and improve resolution as the requested history fills.
        """
        if not self.uniform:
            raise ValueError("A gapped clutter history is not a uniform Doppler FFT")
        count = len(self.frames)
        if count < 8 or (not allow_partial and not self.ready):
            return None
        data = self.residual() if suppress else np.asarray(self.frames)
        weight = np.hanning(count)
        spectrum = np.fft.fftshift(np.fft.fft(data * weight[:, None, None], n=2*count, axis=0), axes=0) / weight.sum()
        if suppress:
            spectrum[count] = 0  # Explicitly blank DC even with window leakage.
        dt = self.stride * 256 / sample_rate
        velocity = -np.fft.fftshift(np.fft.fftfreq(2*count, dt)) * C / (2*frequency)
        return velocity[::-1], spectrum[::-1]


def strongest_horizontal_power(samples, frequency, spacing, bins=256):
    """Maximum uniform horizontal beam power for [velocity, row, column, delay].

    Sum spatial correlation lags first to avoid materializing the large
    velocity × row × beam × delay array. This is exactly the same steering
    polynomial as beamforming.horizontal_power, including row power addition.
    """
    samples = np.asarray(samples)
    columns = samples.shape[2]
    directions = np.linspace(-1, 1, bins)
    phase = 2*np.pi*frequency*spacing/C
    weights = np.exp(-1j*phase*directions[:, None]*np.arange(1, columns))
    real_dtype = samples.real.dtype
    weights_real = np.ascontiguousarray(weights.real, dtype=real_dtype)
    weights_imag = np.ascontiguousarray(weights.imag, dtype=real_dtype)
    result = np.empty((len(samples), samples.shape[-1]), np.float64)
    for start in range(0, len(samples), 16):
        block = samples[start:start+16]
        diagonal = np.sum(abs(block)**2, axis=(1, 2))
        if columns == 1:
            result[start:start+16] = diagonal
            continue
        correlation = np.asarray([np.sum(block[:, :, lag:] * block[:, :, :-lag].conj(), axis=(1, 2)) for lag in range(1, columns)])
        power = diagonal[None] + 2*(
            np.einsum('bl,lvt->bvt', weights_real, correlation.real, optimize=False) -
            np.einsum('bl,lvt->bvt', weights_imag, correlation.imag, optimize=False))
        result[start:start+16] = np.maximum(power.max(axis=0), 0)
    return result
