"""Short-lived, complex-IQ background estimates for moving-target displays."""

from collections import deque

import numpy as np


class RollingClutter:
    # At 25 acquisitions/s the time bound retains about ten snapshots. The
    # count bound also limits adaptation at high rates. Departed targets leave
    # the estimate after at most 16 new chunks or 0.4 s of ongoing reception.
    def __init__(self, chunks=16, max_age=0.4):
        self.chunks, self.max_age = chunks, max_age
        self.clear()

    def clear(self):
        self.history = {}
        self.sums = {}
        self.last_index = {}

    def process(self, index, traces, now):
        """Subtract previous raw acquisitions, then learn this acquisition.

        Work before display decimation and matched filtering. Learning only
        rendered frames would make ghost duration depend on GUI load. Never
        train on residuals or add the current target to its own background.
        """
        cleaned = {}
        for antenna, values in traces.items():
            trace = np.asarray(values, dtype=np.complex64).reshape(-1)
            history = self.history.setdefault(antenna, deque())
            if antenna not in self.sums or self.sums[antenna].shape != trace.shape:
                history.clear()
                self.sums[antenna] = np.zeros(trace.shape, np.complex128)
                self.last_index.pop(antenna, None)
            total = self.sums[antenna]
            while history and (len(history) > self.chunks or now - history[0][0] >= self.max_age):
                total -= history.popleft()[1]
            # The first acquisition establishes the background; do not flash
            # the entire static scene while the rolling estimate warms up.
            cleaned[antenna] = (trace - total / len(history)).astype(np.complex64) if history else np.zeros_like(trace)
            if index > self.last_index.get(antenna, -1):
                history.append((now, trace.copy()))
                total += trace
                self.last_index[antenna] = index
                while len(history) > self.chunks:
                    total -= history.popleft()[1]
        return cleaned
