"""Radar-only housekeeping cadence; leave shared receiver behavior unchanged."""
import time


class RadarReceiverMaintenance:
    """Avoid rescanning every pending pulse after each tiny network batch.

    Expiry is housekeeping, not delivery: complete clusters still publish on
    arrival. A 20 ms cadence bounds the added latency for incomplete clusters.
    Calibration and other trigger modes retain the receiver's original cadence.
    """
    def __init__(self, receiver):
        self.receiver = receiver
        self.original = receiver._expire_cluster_caches
        self.enabled = False
        self.next_scan = 0.0

    def expire(self):
        now = time.monotonic()
        if not self.enabled or self.receiver.calibrating or now >= self.next_scan:
            self.next_scan = now + .02
            return self.original()


def configure_radar_maintenance(receiver, enabled):
    maintenance = getattr(receiver, '_radar_maintenance', None)
    if maintenance is None:
        maintenance = RadarReceiverMaintenance(receiver)
        receiver._radar_maintenance = maintenance
        receiver._expire_cluster_caches = maintenance.expire
    maintenance.enabled = bool(enabled)
