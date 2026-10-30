"""Sample-timestamp delay/Doppler matched filtering for sparse IQ captures.

All acquired samples are used. Gaps are neither interpolated nor time-compressed.
For accumulated vectors the trial waveform is averaged at the same repetition
spacing as the firmware. This models phase AND amplitude loss before reception;
nulls do not become recoverable by changing host processing.
"""
from collections import OrderedDict
from dataclasses import dataclass
import numpy as np
import time

from doppler import C, strongest_horizontal_power


@dataclass
class RadarResult:
    velocities: np.ndarray
    power: np.ndarray  # velocity x requested delay, beamformed voltage squared
    aperture_s: float
    coherent_s: float
    samples: int
    groups: int
    period_samples: int
    minimum_response: float
    partial_accumulations: int


def accumulation_response(frequency_hz, count, step_seconds):
    """Complex response of an M-term normalized boxcar, stable at comb peaks."""
    cycles = np.remainder(np.asarray(frequency_hz) * step_seconds + 0.5, 1.0) - 0.5
    return np.exp(1j*np.pi*(count-1)*cycles) * np.sinc(count*cycles) / np.sinc(cycles)


def minimal_replay(reference):
    """Find an exactly repeating divisor of the submitted RX-grid replay."""
    reference = np.asarray(reference, np.complex64).reshape(-1)
    if not reference.size or not np.isfinite(reference).all() or not np.any(abs(reference) > 0):
        raise ValueError("Doppler processing needs a nonzero finite transmit reference")
    for period in range(1, reference.size+1):
        if reference.size % period == 0 and np.allclose(reference, np.resize(reference[:period], reference.size), rtol=1e-5, atol=1e-7):
            return reference[:period].copy()
    return reference.copy()


def migration_terms(data, weights, times, fd, velocity, sample_rate, offsets, reference_time):
    """Align pulse profiles to the common aperture midpoint for each velocity.

    Delay changes by 2*v*dt/c. The shift is constant over each replay period
    (evaluated at its center); Doppler phase within every sample is still exact.
    For long replays this is a narrowband, stop-and-hop range-walk approximation.
    """
    period = data.shape[-1]
    numerator_fft = np.zeros((len(fd), 8, period), np.complex128)
    coverage_fft = np.zeros_like(numerator_fft)
    phase = np.exp(-2j*np.pi*fd[:, None]*np.arange(period)[None]/sample_rate)
    for begin in range(0, len(times), 16):
        t = times[begin:begin+16]
        dt = t+(period-1)/(2*sample_rate)-reference_time
        shift = np.exp(2j*np.pi*(2*velocity/C)[:, None, None]*dt[None, :, None]*offsets[None, None])
        carrier = np.exp(-2j*np.pi*fd[:, None]*t[None])
        values = data[begin:begin+16]*weights[begin:begin+16]
        spectra = np.fft.fft(values[None]*phase[:, None, None, :])
        numerator_fft += np.einsum('vrm,vram->vam', shift*carrier[:, :, None], spectra, optimize=False)
        coverage_fft += np.einsum('vrm,ram->vam', shift, np.fft.fft(weights[begin:begin+16]), optimize=False)
    return numerator_fft, coverage_fft


class SampleRadar:
    """Bounded raw/averaged chunk history with exact source sample positions."""
    def __init__(self, size=64):
        self.size = int(size)
        self.records = OrderedDict()

    def clear(self):
        self.records.clear()

    def append(self, index, traces, *, count=1, step=0, capture=None, partial=False):
        if self.records and index < min(self.records) and len(self.records) >= self.size:
            return False
        data = np.zeros((8, 256), np.complex64)
        valid = np.zeros((8, 256), bool)
        for antenna, trace in traces.items():
            values = np.asarray(trace, np.complex64).reshape(-1)[:256]
            finite = np.isfinite(values)
            data[int(antenna), :len(values)] = np.where(finite, values, 0)
            valid[int(antenna), :len(values)] = finite
        if not valid.any():
            return False
        if index in self.records:
            old_data, old_valid, old_count, old_step, old_capture, old_partial = self.records[index]
            if (count, step, capture) != (old_count, old_step, old_capture):
                return False
            added = valid & ~old_valid
            if not added.any():
                return False
            data = np.where(added, data, old_data)
            valid = valid | old_valid
            partial = partial or old_partial
        self.records[int(index)] = (data, valid, max(1, int(count)), int(step), capture, bool(partial))
        self.records = OrderedDict(sorted(self.records.items()))
        while len(self.records) > self.size:
            self.records.popitem(last=False)
        return True

    def aperture_seconds(self, sample_rate, max_age=0):
        """Span between the actual centers of retained capture observations."""
        records = list(self.records.items())
        if not records:
            return 0.0
        latest = records[-1][0]*256
        spans = [(i*256+(r[2]-1)*r[3]/2, i*256+(r[2]-1)*r[3]/2+256)
                 for i, r in records if max_age <= 0 or latest-i*256 <= max_age*sample_rate]
        return (max(b for a, b in spans)-min(a for a, b in spans))/sample_rate

    def alias_spacing(self, sample_rate, frequency, max_age=0):
        """Observed burst/window cadence, not a guaranteed unambiguous band.

        Infer it from source indices rather than requested firmware settings;
        adjacent chunks inside a burst do not define its repetition interval.
        """
        records = list(self.records.items())
        if max_age > 0 and records:
            latest = records[-1][0]
            records = [(i, r) for i, r in records if (latest-i)*256 <= max_age*sample_rate]
        if len(records) < 2:
            return None
        if any(r[2] > 1 for i, r in records):
            starts = sorted(set(r[4] if r[4] is not None else i for i, r in records))
        else:
            indices = np.asarray([i for i, r in records], dtype=np.int64)
            gaps = np.diff(indices)
            if not np.any(gaps > 1):
                return None
            starts = indices[np.r_[True, gaps > 1]]
        if len(starts) < 2:
            return None
        # Missed complete windows can be integer multiples of the cadence.
        cadence = int(np.gcd.reduce(np.diff(starts)))
        return C*sample_rate/(2*frequency*cadence*256) if cadence > 1 else None

    def _rows(self, reference, sample_rate, mode, window, suppress, max_age):
        records = list(self.records.items())
        if not records:
            return None
        if max_age > 0:
            latest = records[-1][0]*256
            records = [(i, r) for i, r in records if latest-i*256 <= max_age*sample_rate]
        period = len(reference)
        # Merge partial pieces of the same pulse, retaining validity per sample.
        rows = {}
        run, previous, previous_capture = 0, None, None
        first = records[0][0]*256
        end = first
        for index, (data, valid, count, step, capture, partial) in records:
            if previous is not None and (index != previous+1 or capture != previous_capture):
                run += 1
            group = run if mode == 'bursts' else 0
            previous, previous_capture = index, capture
            start = index*256
            end = max(end, start+256+(count-1)*step)
            offset = 0
            while offset < 256:
                pulse, phase = divmod(start+offset, period)
                length = min(period-phase, 256-offset)
                key = (group, count, step, pulse)
                if key not in rows:
                    if (len(rows)+1)*8*period > 8_388_608:
                        raise ValueError("Replay/history combination is too large; reduce history chunks/lanes or replay length.")
                    rows[key] = (np.zeros((8, period), np.complex64), np.zeros((8, period), bool))
                values, mask = rows[key]
                values[:, phase:phase+length] = data[:, offset:offset+length]
                mask[:, phase:phase+length] = valid[:, offset:offset+length]
                offset += length
        keys = list(rows)
        data = np.asarray([rows[k][0] for k in keys])
        valid = np.asarray([rows[k][1] for k in keys])
        times = np.asarray([k[3]*period-first for k in keys], np.float64)
        centers = times[:, None] + np.arange(period)[None] + np.asarray([(k[1]-1)*k[2]/2 for k in keys])[:, None]
        weight = valid.astype(np.float32)
        coherent = 0.0
        for group in sorted(set(k[0] for k in keys)):
            members = np.asarray([k[0] == group for k in keys])
            good = valid[members].any(axis=1)
            a, b = centers[members][good].min(), centers[members][good].max()+1
            coherent = max(coherent, (b-a)/sample_rate)
            if window == 'hann' and b-a > 1:
                # A raised cosine on actual time, not ordinal record number.
                taper = np.sin(np.pi*(centers[members]-a+0.5)/(b-a))**2
                weight[members] *= taper[:, None, :]
        if suppress:
            total = weight.sum(axis=0)
            mean = np.divide((weight*data).sum(axis=0), total, out=np.zeros_like(total, dtype=np.complex128), where=total > 0)
            data = (data-mean[None]).astype(np.complex64)
        partial = sum(int(r[-1]) for _, r in records)
        return keys, data, weight, times/sample_rate, (end-first)/sample_rate, coherent, int(sum(r[1].sum() for _, r in records)), partial

    def maps(self, reference, sample_rate, frequency, velocities, delays, geometry,
             *, mode='coherent', window='hann', suppress=False, compensate=False,
             response_floor_db=-20, max_age=0, spacing_m=0.06, migration=False, cancelled=None):
        """Matched filter over the requested velocity grid, tiled for bounded memory.

        Demodulate each raw IQ sample for each trial Doppler BEFORE range
        correlation. This retains intra-pulse Doppler information and avoids
        range/Doppler coupling introduced by a zero-Doppler-only correlator.
        'bursts' combines contiguous runs in power; 'coherent' combines all
        records using their true timestamps, including gaps.
        """
        if mode not in ('coherent', 'bursts') or window not in ('hann', 'rectangular'):
            raise ValueError('Unknown Doppler strategy or time window')
        if sample_rate <= 0 or frequency <= 0:
            raise ValueError('Sample rate and RF frequency must be positive')
        geometry = np.asarray(geometry, int)
        reference = minimal_replay(reference)
        prepared = self._rows(reference, sample_rate, mode, window, suppress, max_age)
        if prepared is None:
            return None
        keys, data, weights, times, aperture, coherent, samples, partial = prepared
        period = len(reference)
        velocities = np.asarray(velocities, np.float64)
        # The requested range can span repeated waveform periods. Calculate
        # each physical lag once, then restore the full requested range axis.
        delays, delay_inverse = np.unique(np.asarray(delays, int) % period, return_inverse=True)
        reference_fft = np.fft.fft(reference)
        offsets = np.fft.fftfreq(period, 1/sample_rate)
        reference_energy_fft = np.fft.fft(abs(reference)**2)
        groups = sorted(set(k[0] for k in keys))
        output = np.zeros((len(velocities), len(delays)), np.float64)
        total_group_weight = np.zeros_like(output)
        response_min = 1.0
        floor = 10**(response_floor_db/10)
        group_weights = {}
        for group in groups:
            ids = np.asarray([k[0] == group for k in keys])
            coverage = weights[ids].sum(axis=0)
            base = np.maximum(np.fft.ifft(np.fft.fft(coverage)*reference_energy_fft.conj()).real, 0)
            group_weights[group] = (base, base[:, delays].sum(axis=0))
        # Each integration's samples/coverage are invariant across velocity
        # tiles. Previously these large masked copies were made every tile.
        integrations_by_group = {}
        for group in groups:
            integrations_by_group[group] = []
            for count, step in sorted(set((k[1], k[2]) for k in keys if k[0] == group)):
                ids = np.asarray([k[:3] == (group, count, step) for k in keys])
                values, coverage = data[ids], weights[ids]
                integrations_by_group[group].append((count, step, values, coverage,
                    (values*coverage).astype(np.complex64), times[ids], np.fft.fft(coverage.sum(axis=0))))
        for begin in range(0, len(velocities), 16):
            if cancelled is not None and cancelled():
                return None
            time.sleep(0)  # Let Python acquisition/UI callbacks run between tiles.
            velocity = velocities[begin:begin+16]
            fd = -2*frequency*velocity/C
            phase = np.exp(-2j*np.pi*fd[:, None]*np.arange(period)[None]/sample_rate)
            for group in groups:
                numerator = np.zeros((len(fd), 8, period), np.complex128)
                energy = np.zeros((len(fd), 8, period), np.float64)
                moving_base = np.zeros_like(energy) if migration else None
                for count, step, values, coverage, weighted, row_times, coverage_fft in integrations_by_group[group]:
                    fast_frequency = offsets[None]*(1-2*velocity[:, None]/C) if migration else offsets[None]
                    response = accumulation_response(fd[:, None]+fast_frequency, count, step/sample_rate) if count > 1 else np.ones((len(fd), period))
                    effective_fft = reference_fft[None]*response
                    effective = np.fft.ifft(effective_fft)
                    if migration:
                        folded_fft, coverage_fft = migration_terms(values, coverage, row_times, fd, velocity,
                            sample_rate, offsets, aperture/2)
                        numerator += np.fft.ifft(folded_fft*effective_fft[:, None].conj())
                        energy += np.fft.ifft(coverage_fft*np.fft.fft(abs(effective)**2)[:, None].conj()).real
                        moving_base += np.fft.ifft(coverage_fft*reference_energy_fft.conj()).real
                    else:
                        kernel = np.exp(-2j*np.pi*fd[:, None]*row_times[None]).astype(np.complex64)
                        # A direct contraction avoids BLAS spawning many threads
                        # for each small tile, which can starve the GUI process.
                        folded = np.einsum('vr,rap->vap', kernel, weighted, optimize=False)
                        folded *= phase[:, None, :]
                        numerator += np.fft.ifft(np.fft.fft(folded)*effective_fft[:, None].conj())
                        energy += np.fft.ifft(coverage_fft[None]*np.fft.fft(abs(effective)**2)[:, None].conj()).real
                base, group_weight = group_weights[group]
                base = np.maximum(moving_base, 0) if migration else base[None]
                group_weight = base[:, :, delays].sum(axis=1)
                energy = np.maximum(energy, 0)
                relative = np.divide(energy, base, out=np.zeros_like(energy), where=base > 1e-12)
                occupied = np.broadcast_to(base, energy.shape) > 1e-12
                if occupied.any():
                    response_min = min(response_min, float(relative[occupied].min()))
                denominator = energy if compensate else np.sqrt(energy*base)
                threshold = floor if compensate else 1e-10
                amplitude = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=(denominator > 1e-12) & (relative >= threshold))
                if suppress:
                    amplitude[np.isclose(fd, 0, atol=1e-12)] = 0
                spatial = np.zeros((len(fd), *geometry.shape, len(delays)), np.complex64)
                for row, col in np.ndindex(geometry.shape):
                    antenna = geometry[row, col]
                    if antenna >= 0:
                        spatial[:, row, col] = amplitude[:, antenna, delays]
                power = strongest_horizontal_power(spatial, frequency, spacing_m)
                output[begin:begin+len(fd)] += power*group_weight
                total_group_weight[begin:begin+len(fd)] += group_weight
        output = np.divide(output, total_group_weight, out=np.zeros_like(output), where=total_group_weight > 0)
        return RadarResult(velocities, output[:, delay_inverse], aperture, coherent, samples, len(groups), period, response_min, partial)
