"""Order periodic delay bins after calibrated range zero without interpolation."""
import math
import numpy as np


def range_origin(zero):
    """Integer rotation and fractional origin on the rotated sample grid."""
    if not math.isfinite(zero):
        return 0, float('nan')
    shift = math.ceil(zero)
    return shift, zero - shift


def range_indices(start, stop, zero, period):
    shift, _ = range_origin(zero)
    return (np.arange(start, stop) + shift) % period
