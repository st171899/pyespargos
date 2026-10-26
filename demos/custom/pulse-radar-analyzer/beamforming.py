"""Conventional narrowband planar-array beamforming.

ATWC SS25 Lecture 4, sections 4.1 and 4.4: evaluate the spatial DTFT at
psi_x = k dx cos(elevation) sin(azimuth), psi_y = k dy sin(elevation).
This is the continuous-direction counterpart of the spatial FFT. No visible
region mask or projection of rectangular FFT bins is needed.
"""

from functools import lru_cache

import numpy as np

SPEED_OF_LIGHT = 299792458.0
AZIMUTH_DEG = np.linspace(-90.0, 90.0, 361)
ELEVATION_DEG = np.linspace(-90.0, 90.0, 181)


def horizontal_power(samples, frequency_hz, spacing_m, bins=256):
    """Per-delay horizontal scan on the shader's uniform sin(angle) grid.

    Steer columns coherently, then combine row powers. This measures the
    horizontal direction cosine cos(elevation)*sin(azimuth); a true azimuth
    needs the elevation estimate from planar_power. Use the actual k*d,
    rather than assuming half-wavelength spacing as a bare FFT would.
    """
    columns = samples.shape[1]
    u = np.linspace(-1.0, 1.0, bins)
    phase = 2 * np.pi * frequency_hz * spacing_m / SPEED_OF_LIGHT
    weights = np.exp(-1j * phase * u[:, None] * np.arange(columns))
    beams = np.einsum("bc,rct->rbt", weights, samples, optimize=True)
    return np.sum(np.abs(beams) ** 2, axis=0)


@lru_cache(maxsize=2)
def _steering(rows, columns, frequency_hz, spacing_m):
    azimuth = np.deg2rad(AZIMUTH_DEG)[None, :]
    elevation = np.deg2rad(ELEVATION_DEG)[:, None]
    k_d = 2 * np.pi * frequency_hz * spacing_m / SPEED_OF_LIGHT
    psi_x = k_d * np.cos(elevation) * np.sin(azimuth)
    psi_y = k_d * np.sin(elevation) * np.ones_like(azimuth)
    row, column = np.indices((rows, columns))
    phase = psi_x[..., None] * column.ravel() + psi_y[..., None] * row.ravel()
    return np.exp(-1j * phase).reshape(-1, rows * columns)


def planar_power(samples, frequency_hz, spacing_m):
    """Return elevation-by-azimuth power of uniformly weighted delay-and-sum beams.

    Input shape is (rows, columns, snapshots), with positive spatial phase
    progression corresponding to positive direction cosines. Antenna signals
    are summed coherently, then power is averaged over snapshots. The sample
    covariance gives exactly that result without forming every beam's time trace.
    """
    samples = np.asarray(samples)
    if samples.ndim != 3 or min(samples.shape) < 1:
        raise ValueError("samples must have nonempty rows, columns and snapshots")
    if not np.isfinite(frequency_hz) or frequency_hz <= 0 or not np.isfinite(spacing_m) or spacing_m <= 0:
        raise ValueError("frequency and antenna spacing must be positive and finite")
    rows, columns, snapshots = samples.shape
    signals = samples.reshape(rows * columns, snapshots)
    covariance = signals @ signals.conj().T / snapshots
    weights = _steering(rows, columns, float(frequency_hz), float(spacing_m))
    power = np.einsum("bi,ij,bj->b", weights, covariance, weights.conj(), optimize=True).real
    return np.maximum(power, 0).reshape(len(ELEVATION_DEG), len(AZIMUTH_DEG)) / (rows * columns) ** 2
