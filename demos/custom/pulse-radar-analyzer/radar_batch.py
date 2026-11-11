"""Bounded, timestamp-validated observations with optional array loss tolerance."""
import math


def show_batch_message(message, last_frame, now, cycle_seconds):
    """Routine acquisition is not a warning while valid pictures are fresh.

    Keep configuration/integrity errors immediate. Allow two capture cycles
    (at least two seconds) before incomplete acquisition replaces map status.
    """
    if not message:
        return False
    routine = message.startswith(('Radar batch waiting', 'Radar batch incomplete'))
    return (not routine or last_frame is None
            or now - last_frame >= max(2.0, 2 * cycle_seconds))


class RadarBatchGate:
    def __init__(self, allow_partial_array=False, allow_partial_single=False):
        self.allow_partial_array = allow_partial_array
        self.allow_partial_single = allow_partial_single
        self.reset()

    def reset(self):
        self.key = None
        self.pending = {}
        self.last = -1
        self.newest = -1
        self.message = None

    def usable_cells(self, cluster, cells, trigger):
        """Keep only received sections with verified, complete source coverage."""
        count = int(trigger.get('accumulation_chunks', 8))
        fingerprint = 2166136261
        for i in range(count):
            fingerprint = ((fingerprint ^ ((cluster.source_chunk_start+i) & 0xffffffff))*16777619) & 0xffffffff
        return tuple((b,r,c) for b,r,c in cells
                     if cluster.section_completion[b,r,c,0]
                     and int(cluster.counts[b,r,c,0]) == count
                     and not int(cluster.flags[b,r,c,0]) & 3
                     and int(cluster.coverage_hashes[b,r,c,0]) == fingerprint)

    def feed(self, arrived, cluster, board, positions, trigger):
        n, cycle = int(trigger['pulses']), int(trigger['cycle_windows'])
        count = int(trigger.get('accumulation_chunks', 8))
        interval, offset = int(trigger['interval_chunks']), int(trigger.get('offset_chunks', 0))
        if not 2 <= n <= 1024 or cycle <= n or not 1 <= count <= min(64, interval) or not positions:
            self.message = 'Invalid radar batch configuration.'
            return None
        grouped = isinstance(board, tuple)
        positions = tuple(positions)
        cells = positions if grouped else tuple((board, r, c) for r, c in positions)
        all_cells = cells
        boards = board if grouped else (board,)
        completion = cluster.section_completion
        relaxed = (grouped and self.allow_partial_array) or (not grouped and self.allow_partial_single)
        if relaxed and cluster.vector_chunks == 1:
            cells = self.usable_cells(cluster, cells, trigger)
            if len(cells) < math.ceil(len(positions)/2):
                self.message = 'Radar pulse rejected: less than half the aperture received.'
                return None
        if cluster.vector_chunks != 1 or not all(completion[b, r, c, 0] for b, r, c in cells):
            return None
        sync = getattr(cluster, "sync_info", None)
        sync_epochs = {int(sync[b, r, c]) & 0xffff for b, r, c in cells} if sync is not None else {None}
        if len(sync_epochs) != 1:
            self.message = 'Radar batch rejected: receive grids are not synchronized.'
            return None
        sync_epoch = next(iter(sync_epochs))
        generations = [{int(cluster.config_generation[b, r, c]) for bb, r, c in cells if bb == b} for b in boards]
        if relaxed and self.key and self.key[:2] == (board, positions):
            previous = self.key[2] if grouped else (self.key[2],)
            for i,g in enumerate(generations):
                if not g:
                    g.add(previous[i])
        if any(len(g) != 1 for g in generations):
            return None
        generation = tuple(next(iter(g)) for g in generations) if grouped else next(iter(generations[0]))
        # Firmware window numbers restart when the 24-bit source counter
        # wraps. IQReceiver already unwraps chunk_index, including delayed
        # packets from the previous epoch. Keep observations on either side
        # separate without mistaking the new, lower window numbers for old
        # batches. The unmasked sync-grid offset cancels in this difference.
        epoch = (cluster.chunk_index-cluster.source_chunk_start) // (1 << 24)
        key = (board, tuple(positions), generation, n, cycle, interval, offset, count,
               cluster.sample_rate_hz, cluster.center_freq_hz, sync_epoch, epoch)
        if self.key != key:
            # Reject delayed older generations instead of resetting to old data.
            if self.key and self.key[:2] == key[:2] and any(((new-old) & 0xffffffff) >= 0x80000000 for new, old in zip(key[2] if grouped else (key[2],), self.key[2] if grouped else (self.key[2],))):
                return None
            if self.key and self.key[:3] == key[:3] and epoch < self.key[-1]:
                return None
            self.reset()
            self.key = key
        batch, slot = divmod(cluster.window_index, cycle)
        if batch <= self.last or slot >= n:
            return None
        hashes = {int(cluster.coverage_hashes[b, r, c, 0]) for b, r, c in cells}
        valid = (cluster.nominal_chunks == count and len(hashes) == 1 and
                 all(int(cluster.counts[b, r, c, 0]) == count and
                     not int(cluster.flags[b, r, c, 0]) & 3 for b, r, c in cells) and
                 ((cluster.source_chunk_end-cluster.source_chunk_start) & 0xffffffff) == interval and
                 cluster.source_chunk_start == ((offset+cluster.window_index*interval) & 0xffffffff))
        if valid:
            fingerprint = 2166136261
            for i in range(count):
                fingerprint = ((fingerprint ^ ((cluster.source_chunk_start+i) & 0xffffffff))*16777619) & 0xffffffff
            valid = next(iter(hashes)) == fingerprint
        new_batch = batch > self.newest
        self.newest = max(self.newest, batch)
        bucket = self.pending.setdefault(batch, {})
        if valid or not relaxed:
            bucket[slot] = (arrived, cluster) if valid else None
        # Preserve out-of-order arrivals for at most three observation cycles.
        for old in sorted(self.pending)[:-3]:
            del self.pending[old]
            self.message = 'Radar batch incomplete; holding previous observation.'
        if relaxed:
            # Prefer the newest observation whose last pulse has arrived, or
            # whose successor has begun. Gaps retain their actual timestamps.
            for candidate in sorted(self.pending, reverse=True):
                if candidate != batch and not new_batch:
                    continue
                values = self.pending[candidate]
                if candidate == batch and n-1 not in values:
                    continue
                if len(values) < math.ceil(.9*n):
                    continue
                slots = sorted(values)
                if slots[-1]-slots[0] < .95*(n-1):
                    continue
                totals = {b: 0 for b in boards}
                for _, c in values.values():
                    for b,_,_ in self.usable_cells(c, all_cells, trigger):
                        totals[b] += 1
                if any(totals[b] < math.ceil(.75*n*sum(bb == b for bb,_,_ in all_cells)) for b in boards):
                    continue
                rows = [values[i] for i in slots]
                if any(c.chunk_index-a.chunk_index != (j-i)*interval for (i,(_,a)),(j,(_,c)) in zip(sorted(values.items()), sorted(values.items())[1:])):
                    self.message = 'Radar batch rejected: discontinuous source timestamps.'
                    return None
                self.last = candidate
                self.pending = {k:v for k,v in self.pending.items() if k > candidate}
                self.message = None
                return rows
            self.message = 'Radar batch waiting for pulse span and 75% coverage per board.'
            return None
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
