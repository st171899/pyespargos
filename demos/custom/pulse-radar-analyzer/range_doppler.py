"""Pulse-matched range profiles and timestamped slow-time Doppler processing.

All acquired chunks are used within the bounded observation history. Range
matching runs once per pulse; velocity uses only pulse-to-pulse complex phase.
Capture gaps stay on the source time grid. Firmware averaging losses remain.
"""
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
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
    pulses: int = 0
    averaging_aligned: bool = True


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


@lru_cache(maxsize=128)
def accumulation_layout(source_start, source_end, vector, nominal, lane, coverage_hash):
    """Recover a regular source grid using the firmware coverage fingerprint.

    Some firmware accumulates a decimated source stream: nominal is then
    smaller than window/vector even for a complete window. Verify candidate
    source sequences with the wire FNV-1a word hash before assigning timestamps.
    Unknown/missing coverage must never be presented as consecutive samples.
    Returns (first offset in chunks, repetition step in chunks), or None.
    """
    span = (int(source_end)-int(source_start)) & 0xffffffff
    vector, nominal, lane = int(vector), int(nominal), int(lane)
    if span <= 0 or nominal <= 0 or not 0 <= lane < vector:
        return None
    if nominal*vector == span:
        return lane, vector  # Full coverage has no missing source positions.
    steps = {vector}
    if span % nominal == 0:
        steps.add(span//nominal)
    for step in sorted(steps):
        # Current firmware fingerprints vector starts, so every section
        # shares the hash although its samples start lane chunks later.
        # Also accept verified section-specific fingerprints from older data.
        candidates = [(0,lane),(lane,lane)]
        if step % vector == 0:
            offset = lane*(step//vector)
            candidates.append((offset,offset))
        for hash_offset,source_offset in dict.fromkeys(candidates):
            if source_offset+(nominal-1)*step >= span:
                continue
            fingerprint = 2166136261
            for q in range(nominal):
                index = (int(source_start)+hash_offset+q*step) & 0xffffffff
                fingerprint = ((fingerprint ^ index)*16777619) & 0xffffffff
            if fingerprint == int(coverage_hash):
                return source_offset, step
    return None


def slow_time_spectrum(profiles, times, frequencies, sample_rate, period, cancelled=None):
    """Timestamped pulse-phase transform, using short regular burst lattices.

    A CZT evaluates the requested uniform frequency grid without allocating
    the huge zero-filled pulse lattice across inter-burst gaps. Missing slots
    in each small burst-phase lattice have zero weight, not synthetic data.
    Irregular schedules/nonuniform frequency grids use an exact tiled DFT.
    """
    from scipy.signal import CZT

    profiles = np.asarray(profiles, np.complex64)
    frequencies = np.asarray(frequencies, np.float64)
    times = np.asarray(times, np.float64)
    result = np.zeros((len(frequencies), *profiles.shape[1:]), np.complex128)
    if not len(frequencies):
        return result
    ticks = np.rint(times*sample_rate*2).astype(np.int64)
    unique = np.unique(ticks)
    lattice = None
    uniform = len(frequencies) > 1 and np.allclose(np.diff(frequencies), frequencies[1]-frequencies[0], rtol=1e-8, atol=1e-10)
    exact_ticks = np.allclose(times*sample_rate*2,ticks,rtol=0,atol=1e-4)
    if len(unique) >= 8 and len(frequencies) >= 16 and uniform and exact_ticks:
        gaps = np.diff(unique)
        starts = unique[np.r_[True, gaps > 3*period]]
        cadence = int(np.gcd.reduce(np.diff(starts if len(starts) > 1 else unique)))
        if cadence > 0:
            phases = np.unique(ticks % cadence)
            if len(phases) <= 32:
                candidates = []
                for phase in phases:
                    ids = np.flatnonzero(ticks % cadence == phase)
                    first = ticks[ids].min()
                    slots = (ticks[ids]-first)//cadence
                    candidates.append((ids,slots,int(slots.max())+1,first/(2*sample_rate)))
                if max(c[2] for c in candidates) <= 4096 and sum(c[2] for c in candidates) <= 4*len(times):
                    lattice = candidates, cadence/(2*sample_rate)
    if lattice is not None:
        groups, dt = lattice
        step = frequencies[1]-frequencies[0]
        transforms = {}
        for ids, slots, size, first in groups:
            if size not in transforms:
                transforms[size] = CZT(size,m=len(frequencies),
                    w=np.exp(-2j*np.pi*step*dt),a=np.exp(2j*np.pi*frequencies[0]*dt))
            transform = transforms[size]
            rotation = np.exp(-2j*np.pi*frequencies*first)
            for begin in range(0,profiles.shape[-1],64):
                if cancelled is not None and cancelled():
                    return None
                time.sleep(0)
                end = min(begin+64,profiles.shape[-1])
                grid = np.zeros((size,profiles.shape[1],end-begin),np.complex64)
                np.add.at(grid,slots,profiles[ids,:,begin:end])
                result[:,:,begin:end] += transform(grid,axis=0)*rotation[:,None,None]
        return result
    for begin in range(0,len(frequencies),16):
        if cancelled is not None and cancelled():
            return None
        time.sleep(0)
        phase = np.exp(-2j*np.pi*frequencies[begin:begin+16,None]*times[None]).astype(np.complex64)
        result[begin:begin+16] = np.einsum('vp,pad->vad',phase,profiles,optimize=False)
    return result


class SampleRadar:
    """Bounded raw/averaged chunk history with exact source sample positions."""
    def __init__(self, size=64):
        self.size = int(size)
        self.records = OrderedDict()

    def clear(self):
        self.records.clear()

    def append(self, index, traces, *, count=1, step=0, capture=None, partial=False):
        if self.records and index < next(iter(self.records)) and len(self.records) >= self.size:
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
        reorder = bool(self.records) and index < next(reversed(self.records))
        self.records[int(index)] = (data, valid, max(1, int(count)), int(step), capture, bool(partial))
        if reorder:
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

    def _rows(self, reference, sample_rate, mode, max_age):
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
        centers = times+np.asarray([(k[1]-1)*k[2]/2 for k in keys])
        weight = valid.astype(np.float32)
        coherent = 0.0
        for group in sorted(set(k[0] for k in keys)):
            members = np.asarray([k[0] == group for k in keys])
            row,phase = np.nonzero(valid[members].any(axis=1))
            observed = centers[members][row]+phase
            coherent = max(coherent,(observed.max()-observed.min()+1)/sample_rate)
        partial = sum(int(r[-1]) for _, r in records)
        return keys, data, weight, times/sample_rate, (end-first)/sample_rate, coherent, int(sum(r[1].sum() for _, r in records)), partial

    def maps(self, reference, sample_rate, frequency, velocities, delays, geometry,
             *, mode='coherent', window='hann', suppress=False, max_age=0,
             spacing_m=0.06, cancelled=None):
        """Range match once per pulse, then combine matched-filter slow-time phases.

        Every acquired chunk/verified averaged lane contributes. Pulses split
        across chunks are joined; multiple pulses per chunk stay separate.
        No trial-velocity phase correction is applied within a pulse.
        """
        if mode not in ('coherent','bursts') or window not in ('hann','rectangular'):
            raise ValueError('Unknown Doppler strategy or time window')
        if sample_rate <= 0 or frequency <= 0:
            raise ValueError('Sample rate and RF frequency must be positive')
        reference = minimal_replay(reference)
        # Slow-time taper/suppression operate on range-matched pulses below.
        prepared = self._rows(reference,sample_rate,mode,max_age)
        if prepared is None:
            return None
        keys,data,valid,times,aperture,coherent,samples,partial = prepared
        if cancelled is not None and cancelled():
            return None
        period = len(reference)
        geometry = np.asarray(geometry,int)
        velocities = np.asarray(velocities,np.float64)
        frequencies = -2*frequency*velocities/C
        delays,inverse = np.unique(np.asarray(delays,int)%period,return_inverse=True)
        reference_fft = np.fft.fft(reference)
        # These range FFTs are independent of velocity and run once per pulse.
        correlations = np.fft.ifft(np.fft.fft(data*valid)*reference_fft.conj())[:,:,delays]
        energy = np.maximum(np.fft.ifft(np.fft.fft(valid)*np.fft.fft(abs(reference)**2).conj()).real,0)[:,:,delays]
        energy[energy < max(float(np.vdot(reference,reference).real)*1e-7,1e-12)] = 0
        correlations = np.where(energy > 0,correlations,0)
        centers = times+(period-1)/(2*sample_rate)+np.asarray([(k[1]-1)*k[2]/(2*sample_rate) for k in keys])
        output = np.zeros((len(velocities),len(delays)),np.float64)
        total_weight = np.zeros(len(delays),np.float64)
        response_min,aligned = 1.,True
        groups = sorted(set(k[0] for k in keys))
        for group in groups:
            members = np.asarray([k[0]==group for k in keys])
            ids = np.flatnonzero(members)
            t = centers[ids]
            taper = np.ones(len(ids))
            if window == 'hann' and len(ids)>1:
                span = t.max()-t.min()+period/sample_rate
                taper = np.sin(np.pi*(t-t.min()+period/(2*sample_rate))/span)**2
            coverage = energy[ids]*taper[:,None,None]
            denominator = coverage.sum(axis=0)
            values = correlations[ids].astype(np.complex64)
            if suppress:
                mean = np.divide((values*taper[:,None,None]).sum(axis=0),denominator,
                    out=np.zeros_like(denominator,dtype=np.complex128),where=denominator>1e-12)
                values = values-mean[None]*energy[ids]
            weighted = (values*taper[:,None,None]).astype(np.complex64)
            numerator = np.zeros((len(velocities),8,len(delays)),np.complex128)
            integrations = sorted(set((keys[i][1],keys[i][2]) for i in ids))
            for count,step in integrations:
                local = np.asarray([keys[i][1:3]==(count,step) for i in ids])
                spectrum = slow_time_spectrum(weighted[local],t[local],frequencies,
                    sample_rate,period,cancelled)
                if spectrum is None:
                    return None
                if count>1 and step%period==0:
                    response = accumulation_response(frequencies,count,step/sample_rate)
                    response_min = min(response_min,float(np.min(abs(response)**2)))
                    # Align only the boxcar sign about its centroid, without
                    # amplifying attenuated/noisy samples or filling nulls.
                    centered = (response*np.exp(-1j*np.pi*frequencies*(count-1)*step/sample_rate)).real
                    spectrum *= np.where(abs(response)>1e-6,np.sign(centered),0)[:,None,None]
                elif count>1:
                    aligned = False  # Averaging mixes different waveform phases.
                numerator += spectrum
            amplitude = np.divide(numerator,denominator[None],out=np.zeros_like(numerator),where=denominator[None]>1e-12)
            if suppress:
                amplitude[np.isclose(frequencies,0,atol=1e-12)] = 0
            spatial = np.zeros((len(velocities),*geometry.shape,len(delays)),np.complex64)
            for row,col in np.ndindex(geometry.shape):
                antenna = geometry[row,col]
                if antenna>=0:
                    spatial[:,row,col] = amplitude[:,antenna]
            power = strongest_horizontal_power(spatial,frequency,spacing_m)
            group_weight = denominator.sum(axis=0)
            output += power*group_weight[None]
            total_weight += group_weight
        output = np.divide(output,total_weight[None],out=np.zeros_like(output),where=total_weight[None]>0)
        return RadarResult(velocities,output[:,inverse],aperture,coherent,samples,len(groups),
                           period,response_min,partial,pulses=len(keys),averaging_aligned=aligned)
