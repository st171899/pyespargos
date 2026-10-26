"""Receive counter wraps and neighboring controllers must not freeze displays."""

import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

DEMO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO))
spec = importlib.util.spec_from_file_location("radar_liveness", DEMO / "pulse-radar-analyzer-espargi.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from espargos.iq_packet import IQAccumPacket
from espargos.iq_receiver import IQReceiver
from espargos.iq_accum_cluster import IQAccumCluster
from espargos.sensor_cluster import ClusterCollisionError


class ReceiveLivenessTest(unittest.TestCase):
    def receiver(self):
        receiver = object.__new__(IQReceiver)
        receiver._unwrap_lock = threading.Lock()
        receiver._unwrap_last, receiver._unwrap_epoch = None, 0
        return receiver

    def accumulation_key(self, receiver, source, generation=4):
        packet = object.__new__(IQAccumPacket)
        packet.config_generation, packet.source_chunk_start, packet.vector_chunks = generation, source, 1
        packet.source_chunk_end = source + 8192
        packet.sample_rate_hz, packet.center_freq_hz = 80_000_000, 2_400_000_000
        return receiver._get_cluster_key(0, SimpleNamespace(payload=packet))

    def test_board_local_generations_share_one_accumulation(self):
        receiver = self.receiver()
        self.assertEqual(self.accumulation_key(receiver, 8192, generation=4), self.accumulation_key(receiver, 8192, generation=900))

    def test_partial_board_generation_does_not_reset_display(self):
        cluster = self.cluster(0, 4, 8192, 8192)
        cluster._completion[0, 0, 0] = False
        cluster.config_generation[0, 0, 0] = 0
        backend = self.backend([(time.monotonic() - 1, cluster)])
        module.EspargosPulseRadarAnalyzer._drain_sets(backend)
        self.assertEqual(backend._display_epoch, ("accum", (4,)))
        self.assertEqual(backend.last_display_block, 8)

    def test_accumulation_wraps_with_sync_offset_and_late_previous_packets(self):
        receiver = self.receiver()
        wrap, offset = 1 << 24, 1 << 20
        # Wire indices include the sync offset even above the 24-bit range.
        wires = [offset + n for n in (0, wrap // 3, 2 * wrap // 3, wrap - 8192, 0, wrap - 4096, 8192)]
        keys = [self.accumulation_key(receiver, n)[0] for n in wires]
        self.assertEqual(keys, [offset, offset + wrap // 3, offset + 2 * wrap // 3, offset + wrap - 8192, offset + wrap, offset + wrap - 4096, offset + wrap + 8192])

    def test_accumulation_continues_across_multiple_wraps_and_generations(self):
        receiver = self.receiver()
        sources = np.arange(0, 3 * (1 << 24), 8192, dtype=np.int64)
        actual = [self.accumulation_key(receiver, int(n % (1 << 24)), generation=4 + int(n) // (1 << 24))[0] for n in sources]
        np.testing.assert_array_equal(actual, sources)

    def test_cluster_retains_wire_metadata_and_exposes_unwrapped_grid(self):
        receiver = self.receiver()
        receiver.board_revisions = (SimpleNamespace(),)
        packet = SimpleNamespace(window_index=1, source_chunk_start=8192, source_chunk_end=16384, vector_chunks=1, nominal_chunks=8192, config_generation=4, adc_decimation=1, sample_rate_hz=80_000_000, center_freq_hz=2_400_000_000)
        receiver._gain_phase_enabled = False
        cluster = receiver._create_cluster("accumulations", ((1 << 24) + 8192, 16384, 1, 80_000_000, 2_400_000_000), 0, SimpleNamespace(payload=packet))
        self.assertEqual(cluster.chunk_index, (1 << 24) + 8192)
        self.assertEqual(cluster.source_chunk_start, 8192)
        self.assertEqual(cluster.source_chunk_end, 16384)

    def test_accumulation_collision_identifies_conflicting_metadata(self):
        packet = SimpleNamespace(window_index=1, source_chunk_start=8192, source_chunk_end=16384, vector_chunks=1, nominal_chunks=8192, config_generation=4, adc_decimation=1, sample_rate_hz=80_000_000, center_freq_hz=2_400_000_000)
        cluster = IQAccumCluster(packet, (SimpleNamespace(),))
        incoming = SimpleNamespace(**{**vars(packet), "window_index": 9, "center_freq_hz": 2_450_000_000})
        with self.assertRaises(ClusterCollisionError) as error:
            cluster.add_message(0, SimpleNamespace(antenna_id=3, payload=incoming))
        message = str(error.exception)
        self.assertIn("board 0, antenna 3", message)
        self.assertIn("window_index: existing=1, incoming=9", message)
        self.assertIn("center_freq_hz: existing=2400000000, incoming=2450000000", message)
        self.assertNotIn("source_chunk_start:", message)
        self.assertFalse(np.any(cluster.section_completion))
        self.assertEqual(cluster.window_index, 1)

    def backend(self, clusters):
        return SimpleNamespace(
            _pending_lock=threading.Lock(),
            _pending_clusters=clusters,
            active_board=0,
            receiver=SimpleNamespace(boards=[SimpleNamespace(revision=SimpleNamespace(antenna_id_to_row_col=lambda a: divmod(a, 4))), object()]),
            _display_epoch=("accum", (4,)),
            _display_epoch_time=0,
            block_acc={9: {}},
            block_time={9: 0},
            last_display_block=8,
            _sample_window_lock=threading.Lock(),
            _rx_sensor_ids=lambda: tuple(range(8)),
        )

    def cluster(self, board, generation, wire_index, index):
        cluster = object.__new__(IQAccumCluster)
        cluster.config_generation = np.full((2, 2, 4), generation, np.uint32)
        cluster.source_chunk_start = wire_index
        cluster.chunk_index, cluster.vector_chunks, cluster.sample_rate_hz = index, 1, 80e6
        cluster.center_freq_hz = 2.4e9
        cluster._completion = np.zeros((2, 2, 4), bool)
        cluster._completion[board] = True
        cluster._section_completion = cluster._completion[..., None]
        cluster._sections = np.ones((2, 2, 4, 1, 256), np.complex64)
        return cluster

    def test_display_uses_unwrapped_accumulation_index(self):
        cluster = self.cluster(0, 4, 8192, (1 << 24) + 8192)
        backend = self.backend([(time.monotonic() - 1, cluster)])
        sets = module.EspargosPulseRadarAnalyzer._drain_sets(backend)
        self.assertEqual(sets[0][0], (1 << 24) + 8192)
        self.assertEqual(set(sets[0][1]), set(range(8)))

    def test_other_board_generation_does_not_reset_active_display(self):
        cluster = self.cluster(1, 900, 8192, 8192)
        backend = self.backend([(time.monotonic() - 1, cluster)])
        self.assertEqual(module.EspargosPulseRadarAnalyzer._drain_sets(backend), [])
        self.assertEqual(backend._display_epoch, ("accum", (4,)))
        self.assertEqual(backend.last_display_block, 8)
        self.assertEqual(backend.block_acc, {9: {}})

    def test_only_selected_board_samples_enter_spatial_processing(self):
        for board in (0, 1):
            cluster = self.cluster(0, 4, 8192, 8192)
            cluster._completion[:] = True
            cluster._sections[0] = 3
            cluster._sections[1] = 91
            backend = self.backend([(time.monotonic() - 1, cluster)])
            backend.receiver.boards[1] = backend.receiver.boards[0]
            backend.active_board = board
            sets = module.EspargosPulseRadarAnalyzer._drain_sets(backend)
            self.assertEqual(len(sets), 1)
            for trace in sets[0][1].values():
                np.testing.assert_array_equal(trace, 3 if board == 0 else 91)


if __name__ == "__main__":
    unittest.main()
