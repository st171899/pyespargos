import sys, unittest
from pathlib import Path
from types import SimpleNamespace
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from radar_batch import RadarBatchGate

class BatchTest(unittest.TestCase):
    config=dict(pulses=3,cycle_windows=10,interval_chunks=24,offset_chunks=0)
    def cluster(self,w,g=1,flags=256,count=8):
        start=w*24;h=2166136261
        for i in range(start,start+count):h=((h^i)*16777619)&0xffffffff
        return SimpleNamespace(window_index=w,chunk_index=start,source_chunk_start=start,
            source_chunk_end=start+24,nominal_chunks=count,vector_chunks=1,
            sample_rate_hz=20000000,center_freq_hz=2385000000,
            section_completion=np.ones((1,2,4,1),bool),config_generation=np.full((1,2,4),g),
            counts=np.full((1,2,4,1),count),flags=np.full((1,2,4,1),flags),
            coverage_hashes=np.full((1,2,4,1),h,dtype=np.uint32))
    def feed(self,gate,c):return gate.feed(0,c,0,[(r,c) for r in range(2) for c in range(4)],self.config)
    def test_out_of_order_and_duplicate(self):
        g=RadarBatchGate()
        for w in (2,0,0):self.assertIsNone(self.feed(g,self.cluster(w)))
        rows=self.feed(g,self.cluster(1));self.assertEqual([c.window_index for _,c in rows],[0,1,2])
        self.assertIsNone(self.feed(g,self.cluster(1)))
    def test_partial_and_missing_sensor(self):
        g=RadarBatchGate();c=self.cluster(0);c.section_completion[0,0,0,0]=False
        self.assertIsNone(self.feed(g,c));self.assertIsNone(self.feed(g,self.cluster(1)))
        self.assertIsNone(self.feed(g,self.cluster(2)))
        self.assertIsNone(self.feed(g,self.cluster(0,flags=259)))
        self.assertIn('rejected',g.message)
    def test_new_generation_and_forged_coverage(self):
        g=RadarBatchGate();self.feed(g,self.cluster(0));self.feed(g,self.cluster(1,g=2))
        self.assertIsNone(self.feed(g,self.cluster(0,g=1)))
        self.feed(g,self.cluster(0,g=2));c=self.cluster(2,g=2);c.coverage_hashes[:]=0
        self.assertIsNone(self.feed(g,c));self.assertIn('rejected',g.message)
    def test_missing_batches_bounded_and_recovery(self):
        g=RadarBatchGate()
        for w in range(0,100,10):self.feed(g,self.cluster(w))
        self.assertLessEqual(len(g.pending),3)
        self.feed(g,self.cluster(91));self.assertIsNotNone(self.feed(g,self.cluster(92)))
    def test_source_counter_wrap_and_delayed_previous_epoch(self):
        gate = RadarBatchGate()
        for w in (100,101,102): rows = self.feed(gate,self.cluster(w))
        self.assertIsNotNone(rows)
        for w in (0,1):
            c=self.cluster(w);c.chunk_index += 1 << 24
            self.assertIsNone(self.feed(gate,c))
        # A delayed pre-wrap observation cannot reset the new epoch.
        self.assertIsNone(self.feed(gate,self.cluster(110)))
        c=self.cluster(2);c.chunk_index += 1 << 24
        rows=self.feed(gate,c)
        self.assertEqual([c.window_index for _,c in rows],[0,1,2])
        self.assertEqual(gate.key[-1],1)
    def test_counter_wrap_discards_incomplete_old_observation(self):
        gate=RadarBatchGate()
        self.feed(gate,self.cluster(100));self.feed(gate,self.cluster(101))
        for w in (0,1,2):
            c=self.cluster(w);c.chunk_index += 1 << 24
            rows=self.feed(gate,c)
        self.assertEqual([c.chunk_index for _,c in rows],[(1<<24)+24*i for i in range(3)])
    def test_configurable_counts_and_wrong_count_rejected(self):
        for count in (1, 4, 16, 24):
            self.config = dict(type(self).config, accumulation_chunks=count)
            gate = RadarBatchGate()
            self.feed(gate, self.cluster(0, count=count))
            self.feed(gate, self.cluster(1, count=count))
            self.assertIsNotNone(self.feed(gate, self.cluster(2, count=count)))
            gate = RadarBatchGate()
            self.feed(gate, self.cluster(0, count=count))
            self.feed(gate, self.cluster(1, count=count))
            self.assertIsNone(self.feed(gate, self.cluster(2, count=count+1)))
            self.assertIn('rejected', gate.message)
            gate = RadarBatchGate()
            self.feed(gate, self.cluster(0, count=count))
            self.feed(gate, self.cluster(1, count=count))
            c = self.cluster(2, count=count); c.coverage_hashes[:] = 0
            self.assertIsNone(self.feed(gate, c))
            self.assertIn('rejected', gate.message)
if __name__=='__main__':unittest.main()
