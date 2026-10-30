"""End-to-end synthetic delay/Doppler ambiguity checks on raw IQ samples."""
from pathlib import Path
import sys
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from range_doppler import SampleRadar, accumulation_response
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


class SampleRadarTest(unittest.TestCase):
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

    def test_high_velocity_uses_every_sample_and_gap_timestamps(self):
        ref, radar = replay(), SampleRadar(32)
        for index in [0, 1, 2, 3, 2048, 2049, 2050, 2051]:
            radar.append(index, echo(index, ref, 3000))
        v = np.linspace(2500, 3500, 65)
        result = radar.maps(ref, FS, RF, v, np.arange(128), GEOMETRY, window='rectangular')
        peak = np.unravel_index(np.argmax(result.power), result.power.shape)
        self.assertEqual(v[peak[0]], 3000)
        self.assertEqual(peak[1], 23)
        self.assertAlmostEqual(result.power[peak], PEAK_POWER, places=4)
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
        self.assertAlmostEqual(result.power[1, 0], PEAK_POWER, places=4)
        self.assertEqual(np.argmax(result.power[:, 0]), 1)

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

    def test_accumulated_all_lanes_match_forward_model_and_correction(self):
        ref, radar = replay(), SampleRadar(16)
        count, step, velocity = 8, 4*256, 600
        for index in range(4):
            radar.append(index, echo(index, ref, velocity, count=count, step=step), count=count, step=step, capture=0)
        result = radar.maps(ref, FS, RF, [velocity], [23], GEOMETRY, window='rectangular')
        h = accumulation_response(-2*RF*velocity/C, count, step/FS)
        self.assertAlmostEqual(result.power[0, 0], PEAK_POWER*abs(h)**2, places=4)
        corrected = radar.maps(ref, FS, RF, [velocity], [23], GEOMETRY, window='rectangular', compensate=True, response_floor_db=-40)
        self.assertAlmostEqual(corrected.power[0, 0], PEAK_POWER, places=4)
        self.assertEqual(result.samples, 4*8*256)

    def test_accumulation_null_is_not_recovered(self):
        ref, radar = replay(), SampleRadar(16)
        count, step = 8, 1024
        velocity = C/(2*RF*(count*step/FS))
        radar.append(0, echo(0, ref, velocity, count=count, step=step), count=count, step=step)
        for compensate in (False, True):
            result = radar.maps(ref, FS, RF, [velocity], [23], GEOMETRY, window='rectangular', compensate=compensate)
            self.assertLess(result.power.max(), 1e-10)

    def test_long_replay_averaging_models_changed_fast_time_waveform(self):
        ref, radar = replay(512), SampleRadar(8)
        for index in (0, 1):
            radar.append(index, echo(index, ref, 200, count=5, step=256), count=5, step=256)
        result = radar.maps(ref, FS, RF, [200.], [23, 80, 300], GEOMETRY, window='rectangular', compensate=True, response_floor_db=-40)
        self.assertAlmostEqual(result.power[0, 0], PEAK_POWER, places=4)
        self.assertEqual(np.argmax(result.power), 0)

    def test_range_migration_follows_aircraft_across_range_bins(self):
        ref, radar = replay(), SampleRadar(32)
        indices = [k*8192 for k in range(16)]
        midpoint = (indices[-1]*256+256)/2
        velocity, delay = 250., 60
        for index in indices:
            time = index*256+np.arange(256)
            drift = (2*velocity/C)*(time-midpoint)
            basis = np.exp(2j*np.pi*(time-delay-drift)[:, None]*np.fft.fftfreq(len(ref))[None])
            trace = (basis @ np.fft.fft(ref)/len(ref))*np.exp(-2j*np.pi*2*RF*velocity/C*time/FS)
            radar.append(index, {a: trace.astype(np.complex64) for a in range(8)})
        unaligned = radar.maps(ref, FS, RF, [velocity], np.arange(128), GEOMETRY, window='rectangular')
        for mode in ('coherent', 'bursts'):
            aligned = radar.maps(ref, FS, RF, [velocity], np.arange(128), GEOMETRY, window='rectangular', mode=mode, migration=True)
            self.assertEqual(np.argmax(aligned.power[0]), delay)
            np.testing.assert_allclose(aligned.power[0, delay], PEAK_POWER, rtol=0.002)
            self.assertLess(unaligned.power.max(), aligned.power[0, delay]/2)

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
