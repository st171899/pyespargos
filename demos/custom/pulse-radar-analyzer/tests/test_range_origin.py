import unittest
import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from range_origin import range_origin, range_indices
from range_doppler import SampleRadar

class RangeOriginTest(unittest.TestCase):
    def test_full_period_is_positive_and_permutation(self):
        for zero in (0, 21, 21.187, 255.9, 256.2):
            shift, origin = range_origin(zero)
            indices = range_indices(0, 256, zero, 256)
            np.testing.assert_array_equal(np.sort(indices), np.arange(256))
            np.testing.assert_allclose((indices-zero)%256, np.arange(256)-origin, atol=1e-12)
            self.assertTrue(np.all((np.arange(256)-origin)>=0))
    def test_late_echo_moves_to_far_end_in_doppler_map(self):
        ref = np.zeros(256, complex); ref[0] = 1
        radar=SampleRadar(16)
        for k in range(16):
            trace=np.zeros(256,complex); trace[5]=100
            radar.append(k*24,{0:trace},count=1,step=0,capture=None)
        result=radar.maps(ref,20e6,2385e6,np.array([0.]),range_indices(0,256,21.187,256),np.array([[0]]))
        self.assertEqual(np.argmax(result.power[0]),239)
    def test_uncalibrated_order_unchanged(self):
        np.testing.assert_array_equal(range_indices(0,256,float('nan'),256),np.arange(256))
