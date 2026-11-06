"""Bounded complete-observation gate for firmware radar accumulation windows."""


class RadarBatchGate:
    def __init__(self):
        self.reset()

    def reset(self):
        self.key = None
        self.pending = {}
        self.last = -1
        self.message = None

    def feed(self, arrived, cluster, board, positions, trigger):
        n, cycle = int(trigger['pulses']), int(trigger['cycle_windows'])
        count = int(trigger.get('accumulation_chunks', 8))
        interval, offset = int(trigger['interval_chunks']), int(trigger.get('offset_chunks', 0))
        if not 2 <= n <= 1024 or cycle <= n or not 1 <= count <= min(64, interval) or not positions:
            self.message = 'Invalid radar batch configuration.'
            return None
        completion = cluster.section_completion[board]
        if cluster.vector_chunks != 1 or not all(completion[r, c, 0] for r, c in positions):
            return None
        generations = {int(cluster.config_generation[board, r, c]) for r, c in positions}
        if len(generations) != 1:
            return None
        # Firmware window numbers restart when the 24-bit source counter
        # wraps. IQReceiver already unwraps chunk_index, including delayed
        # packets from the previous epoch. Keep observations on either side
        # separate without mistaking the new, lower window numbers for old
        # batches. The unmasked sync-grid offset cancels in this difference.
        epoch = (cluster.chunk_index-cluster.source_chunk_start) // (1 << 24)
        key = (board, tuple(positions), generations.pop(), n, cycle, interval, offset, count,
               cluster.sample_rate_hz, cluster.center_freq_hz, epoch)
        if self.key != key:
            # Reject delayed older generations instead of resetting to old data.
            if self.key and self.key[:2] == key[:2] and ((key[2]-self.key[2]) & 0xffffffff) >= 0x80000000:
                return None
            if self.key and self.key[:3] == key[:3] and epoch < self.key[-1]:
                return None
            self.reset()
            self.key = key
        batch, slot = divmod(cluster.window_index, cycle)
        if batch <= self.last or slot >= n:
            return None
        hashes = {int(cluster.coverage_hashes[board, r, c, 0]) for r, c in positions}
        valid = (cluster.nominal_chunks == count and len(hashes) == 1 and
                 all(int(cluster.counts[board, r, c, 0]) == count and
                     not int(cluster.flags[board, r, c, 0]) & 3 for r, c in positions) and
                 ((cluster.source_chunk_end-cluster.source_chunk_start) & 0xffffffff) == interval and
                 cluster.source_chunk_start == ((offset+cluster.window_index*interval) & 0xffffffff))
        if valid:
            fingerprint = 2166136261
            for i in range(count):
                fingerprint = ((fingerprint ^ ((cluster.source_chunk_start+i) & 0xffffffff))*16777619) & 0xffffffff
            valid = next(iter(hashes)) == fingerprint
        bucket = self.pending.setdefault(batch, {})
        bucket[slot] = (arrived, cluster) if valid else None
        # Preserve out-of-order arrivals for at most three observation cycles.
        for old in sorted(self.pending)[:-3]:
            del self.pending[old]
            self.message = 'Radar batch incomplete; holding previous observation.'
        if len(bucket) != n:
            return None
        self.last = batch
        self.pending = {k: v for k, v in self.pending.items() if k > batch}
        if any(v is None for v in bucket.values()):
            self.message = 'Radar batch rejected: partial or inconsistent source coverage.'
            return None
        rows = [bucket[i] for i in range(n)]
        indices = [c.chunk_index for _, c in rows]
        if any(b-a != interval for a,b in zip(indices, indices[1:])):
            self.message = 'Radar batch rejected: discontinuous source timestamps.'
            return None
        self.message = None
        return rows
