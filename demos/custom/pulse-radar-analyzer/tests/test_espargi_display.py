"""Guard display processing against redundant work and mismatched array data."""

import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

DEMO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('espargi_display', DEMO / 'pulse-radar-analyzer-espargi.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Analyzer = module.EspargosPulseRadarAnalyzer


class DisplayProcessingTest(unittest.TestCase):
    def backend(self, mode):
        return SimpleNamespace(
            display_mode=mode, remove_los=False, iqcontrol=None,
            _tx_waveform=np.ones(16, np.complex64), _spectrum_sample_rate_hz=80e6,
            _reference_ping_config=lambda: dict(chirp_frames=16, pings_per_256=1),
            _angle_matched_trace=Mock(side_effect=lambda trace, reference: trace),
            _update_angle_estimate=Mock(), _render_radar_heatmap=Mock(),
            _radar_last_render_time=0, fft_size=256,
            _sample_window_lock=threading.Lock(), _sample_window_generation=0,
        )

    def test_waiting_for_samples_retains_the_previous_map(self):
        backend = self.backend('radar')
        backend.radarStatusChanged = Mock()
        with patch.object(module,'publish_map') as publish:
            Analyzer._publish_sample_windows(backend,{})
        publish.assert_not_called()
        self.assertIn('holding previous frame',backend._radar_status)
        backend.remove_los = True
        backend._clutter_mode = 'zero_doppler'
        backend._doppler = module.DopplerHistory(64,uniform=False)
        with patch.object(module,'publish_map') as publish:
            Analyzer._publish_sample_windows(backend,{0:np.ones(256,np.complex64)})
        publish.assert_not_called()
        self.assertIn('collecting 0/2',backend._radar_status)

    def test_all_clutter_modes_clean_ping_time_and_aoa_before_display_processing(self):
        baseline = np.full(256,100+30j,np.complex64)
        motion = np.zeros(256,np.complex64)
        motion[23] = 20j
        for mode in ('ping','time','angle'):
            for strategy in ('manual','automatic','zero_doppler'):
                for length in (256,1024):
                    with self.subTest(mode=mode,strategy=strategy,length=length):
                        backend = self.backend(mode)
                        backend.remove_los = backend._remove_los = True
                        backend._clutter_mode = strategy
                        backend._clutter_lock = threading.Lock()
                        backend._clutter_calibrating = False
                        backend._clutter_calibrated = True
                        backend._clutter_reference = {a:baseline for a in range(8)}
                        backend._cancel_line_of_sight = lambda traces: Analyzer._cancel_line_of_sight(backend,traces)
                        backend._rx_sensor_ids = lambda: tuple(range(8))
                        backend._doppler = module.DopplerHistory(64,uniform=False)
                        for k in range(8):
                            backend._doppler.append(k*8192,np.stack([baseline]*8),1)
                        backend._doppler.append(65536,np.stack([baseline+motion]*8),1)
                        # Rolling subtraction has already happened during ingestion.
                        raw = motion if strategy == 'automatic' else baseline+motion
                        traces = {a:np.tile(raw,length//256) for a in range(8)}
                        expected = np.tile(motion*(8/9 if strategy == 'zero_doppler' else 1),length//256)
                        Analyzer._publish_sample_windows(backend,traces)
                        if mode == 'angle':
                            # The first matched-filter input must already be clean.
                            np.testing.assert_allclose(backend._angle_matched_trace.call_args_list[0].args[0],expected,atol=1e-5)
                            rendered = backend._update_angle_estimate.call_args.args[0]
                            for trace in rendered.values():
                                np.testing.assert_allclose(trace,expected,atol=1e-5)
                        else:
                            for trace in backend._sample_windows:
                                np.testing.assert_allclose(trace,expected,atol=1e-5)
                            backend._angle_matched_trace.assert_not_called()

    def test_zero_doppler_history_learns_raw_chunks_in_ping_time_and_aoa(self):
        for mode in ('ping','time','angle'):
            with self.subTest(mode=mode):
                backend = self.backend(mode)
                backend._display_config = {}
                backend._display_schedule = None
                backend._clutter_calibrating = False
                backend._clutter_mode = 'zero_doppler'
                backend.remove_los = backend._remove_los = True
                backend._doppler_indices = {}
                backend._radar_capture_meta = {}
                backend._doppler = module.DopplerHistory(64,uniform=False)
                backend._rx_sensor_ids = lambda: tuple(range(8))
                backend._append_doppler = lambda i,p,s: Analyzer._append_doppler(backend,i,p,s)
                backend.block_acc, backend.block_time = {}, {}
                backend.last_display_block = -1
                backend._render_sample_blocks = Mock()
                static = {a:np.full(256,100+30j,np.complex64) for a in range(8)}
                Analyzer._render_block_rows(backend,[(17,static),(8209,static),(16403,static)],'time' if mode == 'ping' else mode)
                self.assertEqual(len(backend._doppler.frames),3)
                np.testing.assert_allclose(backend._doppler.mean_frame(),100+30j)

    def test_ping_keeps_raw_iq_without_unused_array_correlation(self):
        backend = self.backend('ping')
        traces = {0: np.arange(256).astype(np.complex64)}
        Analyzer._publish_sample_windows(backend, traces)
        backend._angle_matched_trace.assert_not_called()
        np.testing.assert_array_equal(backend._sample_windows[0], traces[0])
        self.assertFalse(np.shares_memory(backend._sample_windows[0], traces[0]))
        self.assertEqual(backend._sample_window_present, [True] + [False]*7)

    def test_angle_builds_one_reference_for_all_antennas(self):
        backend = self.backend('angle')
        backend._angle_matched_trace.side_effect = lambda trace, reference: Analyzer._angle_matched_trace(backend, trace, reference)
        traces = {a: np.ones(256, np.complex64) for a in range(8)}
        with patch.object(module, 'receive_chirp_reference', wraps=module.receive_chirp_reference) as reference:
            Analyzer._publish_sample_windows(backend, traces)
        reference.assert_called_once()
        self.assertEqual(backend._angle_matched_trace.call_count, 8)
        backend._update_angle_estimate.assert_called_once()
        rendered = backend._update_angle_estimate.call_args.args[0]
        for antenna, trace in traces.items():
            np.testing.assert_array_equal(rendered[antenna], Analyzer._angle_matched_trace(backend, trace))
        self.assertEqual(backend._sample_window_generation, 0)

    def test_radar_throttles_before_filtering_and_rendering(self):
        backend = self.backend('radar')
        traces = {0: np.ones(256, np.complex64)}
        with patch.object(module.time, 'monotonic', side_effect=[1, 1.04, 1.08, 1.12]):
            for _ in range(4):
                Analyzer._publish_sample_windows(backend, traces)
        self.assertEqual(backend._angle_matched_trace.call_count, 2)
        self.assertEqual(backend._render_radar_heatmap.call_count, 2)

    def test_spatial_grid_preserves_permutations_and_missing_antennas(self):
        traces = np.arange(8*256).reshape(8, 256).astype(np.complex64)
        geometry = np.array([[7, -1, 3, 1], [0, 6, -1, 4]])
        samples = module.spatial_samples(traces, geometry)
        for position in np.ndindex(geometry.shape):
            antenna = geometry[position]
            np.testing.assert_array_equal(samples[position], traces[antenna] if antenna >= 0 else np.zeros(256))

    def test_shared_color_scale_matches_previous_radar_mapping(self):
        power = np.array([[0, 1e-8, .1, 1, 2], [0, .001, .02, .5, 1]])
        for lower, upper in [(-50, 0), (-10, -9.5), (-5, -5)]:
            upper = upper if upper > lower else lower+1
            for linear in (False, True):
                if linear:
                    low, high = 10**(lower/10), 10**(upper/10)
                    norm = (power-low)/(high-low)
                else:
                    norm = (10*np.log10(np.maximum(power, 1e-12))-lower)/(upper-lower)
                expected = (module.matplotlib.colormaps['viridis'](np.clip(norm, 0, 1))*255).astype(np.uint8)
                np.testing.assert_array_equal(module.power_pixels(power, lower, upper, linear), expected)


if __name__ == '__main__':
    unittest.main()
