"""Spectral selection and rectangular-array beamspace helpers for IQ demos."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "BinSelection",
    "ExponentialArraySpectrum",
    "music_beamspace_power",
    "rectangular_fft_beamspace_power",
    "rectangular_steering_vectors",
    "select_frequency_bins",
]


@dataclass(frozen=True)
class BinSelection:
    indices: np.ndarray
    allowed: np.ndarray
    selected: np.ndarray
    dc_mask: np.ndarray
    noise_floor_db: float
    peak_index: int | None


class ExponentialArraySpectrum:
    """Track power and cross-antenna coherence for every frequency bin.

    A single FFT snapshot cannot distinguish a coherent plane wave from
    independent receiver noise: both produce one complex array vector.  The
    exponentially averaged cross-spectrum makes that distinction over time.
    It is deliberately independent of array geometry, so other IQ demos can
    reuse it for stable active-bin detection.
    """

    def __init__(self, time_constant_s: float = 0.75, minimum_updates: int = 8):
        self.time_constant_s = max(float(time_constant_s), 1e-3)
        self.minimum_updates = max(2, int(minimum_updates))
        self.reset()

    def reset(self):
        self.covariance = None
        self.update_count = 0

    def update(self, spectra: np.ndarray, elapsed_s: float | None = None):
        spectra = np.asarray(spectra)
        if spectra.ndim < 2:
            raise ValueError("spectra must have antenna dimensions followed by frequency bins")
        flat = spectra.reshape(-1, spectra.shape[-1])
        sample = np.einsum("ak,bk->abk", flat, np.conj(flat), optimize=True)
        if self.covariance is None or self.covariance.shape != sample.shape:
            self.covariance = sample.astype(np.complex64, copy=True)
            self.update_count = 1
            return
        elapsed_s = 0.05 if elapsed_s is None else max(0.0, float(elapsed_s))
        alpha = 1.0 - np.exp(-elapsed_s / self.time_constant_s)
        self.covariance *= 1.0 - alpha
        self.covariance += alpha * sample
        self.update_count += 1

    @property
    def ready(self) -> bool:
        return self.covariance is not None and self.update_count >= self.minimum_updates

    def mean_power(self) -> np.ndarray | None:
        if self.covariance is None:
            return None
        diagonal = np.real(np.einsum("aak->ak", self.covariance))
        return np.maximum(np.mean(diagonal, axis=0), 0.0)

    def coherence(self) -> np.ndarray | None:
        """Mean magnitude coherence across all distinct antenna pairs."""

        if self.covariance is None:
            return None
        antenna_count = self.covariance.shape[0]
        if antenna_count < 2:
            return np.ones(self.covariance.shape[-1], dtype=np.float32)
        diagonal = np.maximum(np.real(np.einsum("aak->ak", self.covariance)), 1e-24)
        denominator = np.sqrt(diagonal[:, np.newaxis, :] * diagonal[np.newaxis, :, :])
        normalized = np.abs(self.covariance) / denominator
        off_diagonal = ~np.eye(antenna_count, dtype=bool)
        return np.asarray(np.mean(normalized[off_diagonal], axis=0), dtype=np.float32)


def select_frequency_bins(
    power_db: np.ndarray,
    mode: str = "active",
    *,
    reject_dc: bool = True,
    dc_half_width: int = 2,
    threshold_db: float = 8.0,
    max_bins: int = 64,
    frequencies_hz: np.ndarray | None = None,
    band_low_hz: float | None = None,
    band_high_hz: float | None = None,
    coherence: np.ndarray | None = None,
    min_coherence: float = 0.0,
) -> BinSelection:
    """Select beamforming bins from one combined-power spectrum.

    Modes are ``peak`` (one strongest allowed bin), ``active`` (bins above a
    robust median noise estimate and, when supplied, a coherence threshold;
    capped by power), ``band`` (manual absolute frequency interval), and
    ``all``.  DC rejection removes the exact centre and a configurable number
    of neighbours from every mode.  Active mode intentionally returns no bins
    when no signal passes its gates rather than beamforming arbitrary noise.
    """

    power_db = np.asarray(power_db, dtype=np.float64).reshape(-1)
    count = power_db.size
    finite = np.isfinite(power_db)
    dc_mask = np.zeros(count, dtype=bool)
    if reject_dc and count:
        center = count // 2
        half_width = max(0, int(dc_half_width))
        dc_mask[max(0, center - half_width) : min(count, center + half_width + 1)] = True
    allowed = finite & ~dc_mask

    mode = str(mode).lower()
    if mode == "band":
        if frequencies_hz is None:
            raise ValueError("band selection requires frequencies_hz")
        frequencies_hz = np.asarray(frequencies_hz).reshape(-1)
        if frequencies_hz.size != count:
            raise ValueError("frequencies_hz and power_db lengths differ")
        low = -np.inf if band_low_hz is None else min(float(band_low_hz), float(band_high_hz if band_high_hz is not None else band_low_hz))
        high = np.inf if band_high_hz is None else max(float(band_high_hz), float(band_low_hz if band_low_hz is not None else band_high_hz))
        allowed &= (frequencies_hz >= low) & (frequencies_hz <= high)
    elif mode not in ("peak", "active", "all"):
        raise ValueError(f"unknown bin-selection mode {mode!r}")

    if coherence is not None:
        coherence = np.asarray(coherence, dtype=np.float64).reshape(-1)
        if coherence.size != count:
            raise ValueError("coherence and power_db lengths differ")

    candidate_indices = np.flatnonzero(allowed)
    noise_floor = float(np.median(power_db[candidate_indices])) if candidate_indices.size else float("-inf")
    peak_index = int(candidate_indices[np.argmax(power_db[candidate_indices])]) if candidate_indices.size else None
    selected = np.zeros(count, dtype=bool)

    if mode == "peak":
        if peak_index is not None:
            selected[peak_index] = True
    elif mode == "active":
        active = candidate_indices[power_db[candidate_indices] >= noise_floor + float(threshold_db)]
        if coherence is not None:
            active = active[coherence[active] >= float(min_coherence)]
        limit = max(1, int(max_bins))
        if active.size > limit:
            active = active[np.argpartition(power_db[active], -limit)[-limit:]]
        selected[active] = True
    else:  # band/all: every allowed bin is intentionally used
        selected[candidate_indices] = True

    return BinSelection(
        indices=np.flatnonzero(selected),
        allowed=allowed,
        selected=selected,
        dc_mask=dc_mask,
        noise_floor_db=noise_floor,
        peak_index=peak_index,
    )


def rectangular_fft_beamspace_power(
    spectra: np.ndarray,
    selected_indices: np.ndarray,
    resolution_azimuth: int = 64,
    resolution_elevation: int = 32,
    batch_size: int = 64,
) -> np.ndarray:
    """Beamform selected bins from ``(rows, columns, bins)`` spectra.

    The aperture is centred in a zero-padded spatial grid exactly like the
    WiFi camera demo's FFT beamformer.  Frequency bins are combined
    incoherently, so unrelated emitters cannot cancel each other.  Batching
    bounds temporary memory when ``all`` selects a large FFT.
    """

    spectra = np.asarray(spectra)
    if spectra.ndim != 3:
        raise ValueError("spectra must have shape (rows, columns, bins)")
    selected_indices = np.asarray(selected_indices, dtype=np.intp).reshape(-1)
    resolution_azimuth = max(int(resolution_azimuth), spectra.shape[1])
    resolution_elevation = max(int(resolution_elevation), spectra.shape[0])
    output = np.zeros((resolution_azimuth, resolution_elevation), dtype=np.float64)
    if selected_indices.size == 0:
        return output

    rows, columns, _ = spectra.shape
    az0 = resolution_azimuth // 2 - columns // 2
    el0 = resolution_elevation // 2 - rows // 2
    batch_size = max(1, int(batch_size))
    for start in range(0, selected_indices.size, batch_size):
        indices = selected_indices[start : start + batch_size]
        aperture = np.zeros((resolution_azimuth, resolution_elevation, indices.size), dtype=np.complex64)
        aperture[az0 : az0 + columns, el0 : el0 + rows, :] = np.swapaxes(spectra[..., indices], 0, 1)
        beamspace = np.fft.fftshift(
            np.fft.fft2(np.fft.ifftshift(aperture, axes=(0, 1)), axes=(0, 1)),
            axes=(0, 1),
        )
        output += np.sum(np.abs(beamspace) ** 2, axis=-1)

    antenna_count = rows * columns
    return output / max(float(antenna_count * antenna_count * selected_indices.size), 1.0)


def rectangular_steering_vectors(
    rows: int,
    columns: int,
    resolution_azimuth: int = 64,
    resolution_elevation: int = 32,
    frequency_ratio: float = 1.0,
) -> np.ndarray:
    """Return half-wavelength rectangular-array steering vectors.

    The output has shape ``(rows * columns, azimuth, elevation)`` and uses the
    same beamspace coordinates and axis orientation as
    :func:`rectangular_fft_beamspace_power`. ``frequency_ratio`` is the RF
    frequency divided by the array's reference frequency, allowing wideband
    MUSIC bins to focus onto one common direction grid.
    """

    rows = max(1, int(rows))
    columns = max(1, int(columns))
    resolution_azimuth = max(int(resolution_azimuth), columns)
    resolution_elevation = max(int(resolution_elevation), rows)
    azimuth_sine = 2.0 * (np.arange(resolution_azimuth) - resolution_azimuth // 2) / resolution_azimuth
    elevation_sine = 2.0 * (np.arange(resolution_elevation) - resolution_elevation // 2) / resolution_elevation
    phase = (
        np.pi
        * float(frequency_ratio)
        * (np.arange(columns)[np.newaxis, :, np.newaxis, np.newaxis] * azimuth_sine[np.newaxis, np.newaxis, :, np.newaxis] + np.arange(rows)[:, np.newaxis, np.newaxis, np.newaxis] * elevation_sine[np.newaxis, np.newaxis, np.newaxis, :])
    )
    return np.exp(1j * phase).reshape(rows * columns, resolution_azimuth, resolution_elevation)


def music_beamspace_power(
    covariance: np.ndarray,
    selected_indices: np.ndarray,
    *,
    rows: int,
    columns: int,
    resolution_azimuth: int = 64,
    resolution_elevation: int = 32,
    source_count: int = 1,
    frequencies_hz: np.ndarray | None = None,
    reference_frequency_hz: float | None = None,
) -> np.ndarray:
    """Compute a wideband covariance-averaged MUSIC pseudo-spectrum.

    ``covariance`` has shape ``(antennas, antennas, bins)`` and is normally
    supplied by :class:`ExponentialArraySpectrum`. Selected-bin covariances are
    summed before the eigendecomposition, matching the established pyespargos
    CSI-camera MUSIC path. Strong bins contribute naturally, uncorrelated noise
    averages toward a diagonal matrix, and ``source_count`` can represent
    independent emitters occupying different selected bins. One
    power-weighted mean RF frequency focuses the steering grid; the IQ capture
    fractional bandwidth is small enough that this approximation keeps All
    mode real-time.
    """

    covariance = np.asarray(covariance)
    selected_indices = np.asarray(selected_indices, dtype=np.intp).reshape(-1)
    antenna_count = int(rows) * int(columns)
    resolution_azimuth = max(int(resolution_azimuth), int(columns))
    resolution_elevation = max(int(resolution_elevation), int(rows))
    output = np.zeros((resolution_azimuth, resolution_elevation), dtype=np.float64)
    if covariance.ndim != 3 or covariance.shape[:2] != (antenna_count, antenna_count):
        raise ValueError("covariance must have shape (rows*columns, rows*columns, bins)")
    if antenna_count < 2:
        raise ValueError("MUSIC requires at least two antennas")
    if selected_indices.size == 0:
        return output
    if np.any(selected_indices < 0) or np.any(selected_indices >= covariance.shape[-1]):
        raise ValueError("selected bin index out of range")

    selected_covariance = np.sum(covariance[..., selected_indices], axis=-1, dtype=np.complex128)
    selected_covariance = (selected_covariance + np.conj(selected_covariance.T)) * 0.5

    frequency_ratio = 1.0
    if frequencies_hz is not None:
        frequencies_hz = np.asarray(frequencies_hz, dtype=np.float64).reshape(-1)
        if frequencies_hz.size != covariance.shape[-1]:
            raise ValueError("frequencies_hz and covariance bin counts differ")
        traces = np.maximum(np.real(np.trace(covariance[..., selected_indices], axis1=0, axis2=1)), 0.0)
        selected_frequency = float(np.average(frequencies_hz[selected_indices], weights=traces)) if np.any(traces) else float(np.mean(frequencies_hz[selected_indices]))
        if reference_frequency_hz is None:
            reference_frequency_hz = selected_frequency
        if not np.isfinite(reference_frequency_hz) or reference_frequency_hz <= 0:
            raise ValueError("reference_frequency_hz must be positive")
        frequency_ratio = selected_frequency / float(reference_frequency_hz)
    elif reference_frequency_hz is not None and (not np.isfinite(reference_frequency_hz) or reference_frequency_hz <= 0):
        raise ValueError("reference_frequency_hz must be positive")

    source_count = min(max(1, int(source_count)), antenna_count - 1)
    eigenvalues, eigenvectors = np.linalg.eigh(selected_covariance)
    eigenvalues = np.maximum(np.real(eigenvalues), 0.0)
    signal_level = float(np.mean(eigenvalues[-source_count:]))
    noise_level = float(np.mean(eigenvalues[: antenna_count - source_count]))
    if signal_level <= noise_level + 1e-24:
        return output

    noise_subspace = eigenvectors[:, : antenna_count - source_count]
    steering = rectangular_steering_vectors(
        rows,
        columns,
        resolution_azimuth,
        resolution_elevation,
        frequency_ratio=frequency_ratio,
    )
    projection = np.einsum("an,aij->nij", np.conj(noise_subspace), steering, optimize=True)
    output = 1.0 / np.maximum(np.sum(np.abs(projection) ** 2, axis=0), 1e-12)
    output -= np.min(output)
    peak = float(np.max(output))
    if peak > 0:
        output /= peak
    return output
