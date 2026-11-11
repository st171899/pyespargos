"""Isolate expensive radar maps from the Python packet reception threads."""
from concurrent.futures import ProcessPoolExecutor, TimeoutError
import multiprocessing


def compute_map(records, channels, args, kwargs):
    from range_doppler import SampleRadar
    radar = SampleRadar(len(records))
    radar.records, radar.channels = records, channels
    return radar.maps(*args, **kwargs)


class RadarMapWorker:
    """One bounded in-flight map; no growing background observation queue."""
    def __init__(self):
        self.executor = None

    def maps(self, radar, args, kwargs, cancelled):
        if self.executor is None:
            self.executor = ProcessPoolExecutor(max_workers=1,
                mp_context=multiprocessing.get_context('spawn'))
        future = self.executor.submit(compute_map, radar.records.copy(), radar.channels, args, kwargs)
        discard = False
        while True:
            try:
                result = future.result(timeout=.05)
                return None if discard or (cancelled is not None and cancelled()) else result
            except TimeoutError:
                discard = discard or (cancelled is not None and cancelled())

    def close(self):
        if self.executor is not None:
            self.executor.shutdown(wait=True, cancel_futures=True)
            self.executor = None
