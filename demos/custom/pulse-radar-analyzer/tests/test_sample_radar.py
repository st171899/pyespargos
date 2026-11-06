"""End-to-end synthetic delay/Doppler ambiguity checks on raw IQ samples."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from range_doppler import SampleRadar, accumulation_response, accumulation_layout, slow_time_spectrum
from doppler import C

FS, RF = 80e6, 2.4e9
GEOMETRY = np.arange(8).reshape(2, 4)
# The even 256-beam grid samples either side of exact boresight.
a = 2*np.pi*RF*0.06/C/255
PEAK_POWER = 2*(np.sin(4*a/2)/np.sin(a/2))**2


def replay(period=128):
    rng = np.random.default_rng(6)
    reference = np.zeros(period, np.complex64)
    reference[:16] = np.exp(1j*rng.uniform(-np.pi, np.pi, 16))
    return reference


def echo(index, reference, velocity, delay=23, count=1, step=0, amplitude=1):
    time = index*256+np.arange(256)
    fd = -2*RF*velocity/C
    values = np.zeros(256, np.complex128)
    for q in range(count):
        t = time+q*step
        values += reference[(t-delay) % len(reference)]*np.exp(2j*np.pi*fd*t/FS)/count
    return {a: amplitude*values.astype(np.complex64) for a in range(8)}


def matched_power(reference,velocity,delay=23,count=1,step=0):
    # Independent, direct matched-filter calculation for a single pulse.
    trace = echo(0,reference,velocity,delay,count,step)[0][:len(reference)]
    gain = np.vdot(np.roll(reference,delay),trace)/np.vdot(reference,reference)
    return PEAK_POWER*abs(gain)**2


class SampleRadarTest(unittest.TestCase):
    def test_sparse_firmware_accumulation_layout_matches_recorded_fingerprint(self):
        # Actual captured header: a complete 8192-chunk window contains 1024
        # contributions spaced 8 chunks apart, not 8192 consecutive inputs.
        self.assertEqual(accumulation_layout(2342912,2351104,1,1024,0,4125752773),(0,8))
        self.assertIsNone(accumulation_layout(2342912,2351104,1,1024,0,4125752772))
        # Four-lane variants must preserve the lane's verified source offset.
        for lane,fingerprint in enumerate([440011205,708422085,281369029,3731384773]):
            self.assertEqual(accumulation_layout(8192,16384,4,256,lane,fingerprint),(lane*8,32))
        for lane,fingerprint in enumerate([440011205,1825938117,3951943109,3535305925]):
            self.assertEqual(accumulation_layout(8192,16384,4,256,lane,fingerprint),(lane,32))

    def test_hardware_vector_start_fingerprint_accepts_all_sections(self):
        # Recorded vector=4 packet: all four sections carry the same hash of
        # vector starts, not a hash shifted by their section offset.
        for lane in range(4):
            self.assertEqual(accumulation_layout(1003520,1003776,4,4,lane,2196396053),(lane,64))
            self.assertEqual(accumulation_layout(1383424,1383936,4,8,lane,3847249253),(lane,64))

    def test_hann_notches_are_set_by_aperture_not_capture_alias_period(self):
        ref,radar = replay(256),SampleRadar(64)
        velocity,gap,n = 3.,2048,39
        for k in range(n):
            index=k*gap
            phase=np.exp(-2j*np.pi*2*RF*velocity/C*index*256/FS)
            radar.append(index,{0:np.roll(ref,23)*phase},count=64,step=2048,capture=index)
        span=((n-1)*gap*256+256)/FS
        dv=C/(2*RF*span)
        alias=C*FS/(2*RF*gap*256)
        result=radar.maps(ref,FS,RF,[velocity,velocity+2*dv,velocity+3*dv,velocity+alias],[23],np.array([[0]]))
        self.assertLess(dv,.3)
        self.assertGreater(alias,9.)
        self.assertLess(result.power[1,0]/result.power[0,0],1e-5)
        self.assertLess(result.power[2,0]/result.power[0,0],1e-5)
        self.assertAlmostEqual(result.power[3,0]/result.power[0,0],1.,places=5)

    def test_short_cpi_mean_removal_can_peak_at_half_capture_alias_spacing(self):
        ref,radar = replay(256),SampleRadar(16)
        gap=2048
        for k in range(4):
            index=k*gap
            radar.append(index,{0:np.roll(ref,23)*(1+.001*k)},count=64,step=2048,capture=index)
        alias=C*FS/(2*RF*gap*256)
        result=radar.maps(ref,FS,RF,[-alias,-alias/2,0,alias/2,alias],[23],np.array([[0]]),suppress=True)
        self.assertAlmostEqual(alias/2,4.765072790781657)
        self.assertGreater(result.power[1,0],1e-8)
        self.assertGreater(result.power[3,0],1e-8)
        self.assertLess(result.power[[0,2,4],0].max(),1e-10)

    def test_alias_spacing_uses_observed_burst_and_accumulation_positions(self):
        ref, radar = replay(), SampleRadar(64)
        # Requested K can differ from what arrived. Missing an entire burst
        # must not halve the inferred alias spacing.
        for i in [17,18,19, 4113,4114,4115, 12305,12306,12307]:
            radar.append(i, echo(i, ref, 2))
        self.assertAlmostEqual(radar.alias_spacing(FS, RF), C*FS/(2*RF*4096*256))
        self.assertIsNone(radar.alias_spacing(FS, RF, max_age=256/FS))
        radar.clear()
        for start in [0,8192,16384]:
            for lane in range(4):
                radar.append(start+lane, echo(start+lane,ref,2), count=2048,step=1024,capture=start)
        self.assertAlmostEqual(radar.alias_spacing(FS, RF), C*FS/(2*RF*8192*256))

    def test_repeated_requested_ranges_keep_original_order(self):
        ref, radar = replay(), SampleRadar(8)
        for i in range(4):
            radar.append(i,echo(i,ref,2))
        result = radar.maps(ref,FS,RF,[2],[23,5,151,279],GEOMETRY,window='rectangular')
        self.assertEqual(result.power.shape,(1,4))
        np.testing.assert_allclose(result.power[0,[0,2,3]],PEAK_POWER,rtol=1e-6)
        self.assertLess(result.power[0,1],result.power[0,0])

    def test_velocity_tiling_can_cancel_stale_work(self):
        ref, radar = replay(), SampleRadar(16)
        for i in range(8):
            radar.append(i,echo(i,ref,2))
        calls = []
        def cancelled():
            calls.append(True)
            return len(calls) == 2
        self.assertIsNone(radar.maps(ref,FS,RF,np.arange(64),[23],GEOMETRY,cancelled=cancelled))
        self.assertEqual(len(calls),2)

    def test_velocity_uses_every_matched_pulse_and_gap_timestamps(self):
        ref, radar = replay(), SampleRadar(32)
        for index in [0, 1, 2, 3, 2048, 2049, 2050, 2051]:
            radar.append(index, echo(index, ref, 3000))
        v = np.linspace(2500, 3500, 65)
        result = radar.maps(ref, FS, RF, v, np.arange(128), GEOMETRY, window='rectangular')
        peak = np.unravel_index(np.argmax(result.power), result.power.shape)
        self.assertEqual(v[peak[0]], 3000)
        self.assertEqual(peak[1], 23)
        self.assertAlmostEqual(result.power[peak], matched_power(ref,3000), places=4)
        self.assertEqual(result.pulses,16)
        self.assertEqual(result.samples, 8*8*256)
        self.assertGreater(result.aperture_s, 0.006)

    def test_power_average_uses_all_separate_bursts_without_cross_gap_phase(self):
        ref, radar = replay(), SampleRadar(16)
        for index in [0, 1, 2, 3, 2048, 2049, 2050, 2051]:
            amplitude = 1 if index < 2048 else 1j
            radar.append(index, echo(index, ref, 3000, amplitude=amplitude))
        v = np.array([2800., 3000., 3200.])
        result = radar.maps(ref, FS, RF, v, [23], GEOMETRY, mode='bursts', window='rectangular')
        self.assertEqual(result.groups, 2)
        self.assertLess(result.coherent_s, result.aperture_s/100)
        self.assertAlmostEqual(result.power[1, 0], matched_power(ref,3000), places=4)
        self.assertEqual(np.argmax(result.power[:, 0]), 1)

    def test_both_pulses_in_every_received_chunk_contribute_to_doppler(self):
        ref,radar = replay(),SampleRadar(64)
        gap = 8192*256/FS
        dv = C/(2*RF*32*gap)
        velocities = np.array([3*dv,-5*dv,0.])
        target = np.roll(ref,23)
        for k in range(32):
            index = k*8192
            times = (index*256+np.array([63.5,191.5]))/FS
            phase = np.exp(-2j*np.pi*2*RF*velocities[:2]/C*times)
            trace = np.concatenate([target*phase[0],target*phase[1]])
            radar.append(index,{a:trace for a in range(8)})
        result = radar.maps(ref,FS,RF,velocities,[23],GEOMETRY,window='rectangular')
        self.assertEqual(result.pulses,64)
        self.assertEqual(result.samples,32*8*256)
        np.testing.assert_allclose(result.power[:2,0],PEAK_POWER/4,rtol=1e-6)
        self.assertLess(result.power[2,0],1e-10)

    def test_missing_antennas_and_chunks_are_not_zero_measurements(self):
        ref, radar = replay(), SampleRadar(16)
        for index in [0, 1, 5, 8]:
            traces = echo(index, ref, 1)
            if index == 5:
                del traces[0]
            radar.append(index, traces)
        result = radar.maps(ref, FS, RF, [1.], [23], GEOMETRY, window='rectangular')
        self.assertAlmostEqual(result.power[0, 0], PEAK_POWER, places=4)
        self.assertEqual(len(radar.records), 4)
        self.assertTrue(radar.append(5, echo(5, ref, 1)))  # Fill the formerly absent antenna.
        self.assertFalse(radar.append(5, echo(5, ref, 1)))

    def test_accumulated_all_lanes_preserve_measured_averaging_loss(self):
        ref,radar = replay(),SampleRadar(16)
        count,step,velocity = 8,4*256,600
        for index in range(4):
            radar.append(index,echo(index,ref,velocity,count=count,step=step),count=count,step=step,capture=0)
        result = radar.maps(ref,FS,RF,[velocity],[23],GEOMETRY,window='rectangular')
        self.assertAlmostEqual(result.power[0,0],matched_power(ref,velocity,count=count,step=step),places=4)
        self.assertLess(result.power[0,0],matched_power(ref,velocity))
        self.assertEqual(result.samples,4*8*256)
        self.assertEqual(result.pulses,8)

    def test_accumulation_null_is_not_recovered(self):
        ref,radar = replay(),SampleRadar(16)
        count,step = 8,1024
        velocity = C/(2*RF*(count*step/FS))
        radar.append(0,echo(0,ref,velocity,count=count,step=step),count=count,step=step)
        result = radar.maps(ref,FS,RF,[velocity],[23],GEOMETRY,window='rectangular')
        self.assertLess(result.power.max(),1e-10)

    def test_long_replay_joins_all_chunks_without_inverting_waveform_mixing(self):
        ref,radar = replay(512),SampleRadar(8)
        for index in (0,1):
            radar.append(index,echo(index,ref,200,count=5,step=256),count=5,step=256)
        result = radar.maps(ref,FS,RF,[200.],[23,80,300],GEOMETRY,window='rectangular')
        trace = np.concatenate([echo(i,ref,200,count=5,step=256)[0] for i in (0,1)])
        expected = np.abs(np.fft.ifft(np.fft.fft(trace)*np.fft.fft(ref).conj())/np.vdot(ref,ref))**2*PEAK_POWER
        np.testing.assert_allclose(result.power[0],expected[[23,80,300]],rtol=1e-5,atol=1e-6)
        self.assertEqual(result.pulses,1)
        self.assertEqual(result.samples,2*8*256)
        self.assertFalse(result.averaging_aligned)

    def test_fast_burst_czt_and_irregular_fallback_match_timestamp_dft(self):
        rng = np.random.default_rng(12)
        schedules = [np.arange(64)*128,
                     np.array([k//4*8192*256+k%4*128 for k in range(64)]),
                     np.delete(np.arange(64)*128,[3,19,20]),
                     np.cumsum(rng.integers(129,100000,size=64))]
        frequency = np.linspace(500,-500,129)
        for schedule in schedules:
            times = schedule/FS
            profiles = (rng.normal(size=(len(schedule),8,7))+1j*rng.normal(size=(len(schedule),8,7))).astype(np.complex64)
            expected = np.einsum('vp,pad->vad',np.exp(-2j*np.pi*frequency[:,None]*times[None]),profiles)
            from scipy.signal import CZT
            with patch('scipy.signal.CZT',wraps=CZT) as fast:
                actual = slow_time_spectrum(profiles,times,frequency,FS,128)
            self.assertEqual(bool(fast.call_count),schedule is not schedules[-1])
            np.testing.assert_allclose(actual,expected,rtol=3e-5,atol=3e-5)

    def test_range_filtering_cost_is_independent_of_velocity_grid(self):
        ref,radar = replay(),SampleRadar(32)
        for i in range(16):
            radar.append(i,echo(i,ref,100))
        calls = []
        for bins in (17,513):
            with patch('range_doppler.np.fft.fft',wraps=np.fft.fft) as fft:
                result = radar.maps(ref,FS,RF,np.linspace(-200,200,bins),[23],GEOMETRY)
            self.assertEqual(result.pulses,32)
            calls.append(fft.call_count)
        self.assertEqual(calls[0],calls[1])
        self.assertLess(calls[1],10)

    def test_age_limit_and_history_budget(self):
        ref, radar = replay(), SampleRadar(3)
        for index in [0, 10, 20, 30]:
            radar.append(index, echo(index, ref, 1))
        self.assertEqual(list(radar.records), [10, 20, 30])
        result = radar.maps(ref, FS, RF, [1.], [23], GEOMETRY, max_age=5*256/FS, window='rectangular')
        self.assertEqual(result.samples, 8*256)

    def test_late_partial_capture_fills_only_missing_samples(self):
        ref, radar = replay(), SampleRadar(8)
        traces = echo(5, ref, 200)
        radar.append(5, {0: traces[0]})
        snapshot = radar.records[5]
        self.assertTrue(radar.append(5, traces))
        self.assertEqual(snapshot[1].sum(), 256)  # In-flight readers remain immutable.
        self.assertEqual(radar.records[5][1].sum(), 8*256)
        self.assertFalse(radar.append(5, traces))
        self.assertFalse(radar.append(5, traces, count=2, step=256))
        result = radar.maps(ref, FS, RF, [200.], [23], GEOMETRY, window='rectangular')
        self.assertAlmostEqual(result.power[0, 0], PEAK_POWER, places=4)

    def test_zero_doppler_suppression_and_tapers(self):
        ref, radar = replay(), SampleRadar(16)
        for index in range(8):
            radar.append(index, echo(index, ref, 0))
        for window in ('hann', 'rectangular'):
            result = radar.maps(ref, FS, RF, [-100., 0., 100.], np.arange(128), GEOMETRY, window=window, suppress=True)
            self.assertLess(result.power.max(), 1e-10)


if __name__ == '__main__':
    unittest.main()
