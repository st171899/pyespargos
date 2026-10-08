"""Transport-neutral data types for captured radio packets."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class SignalRegion:
    """One energy burst with a measurable quiet interval before its onset."""

    start_sample: int
    end_sample: int
    peak_dbfs: float
    noise_dbfs: float
    clean_onset: bool

    @property
    def sample_count(self) -> int:
        return self.end_sample - self.start_sample


@dataclass(frozen=True)
class SignalFeatures:
    """Protocol-independent measurements of a :class:`SignalRegion`."""

    center_offset_hz: float
    occupied_bandwidth_hz: float
    spectral_flatness: float
    envelope_variation: float
    ofdm_short_repeat_score: float
    binary_fm_score: float


@dataclass(frozen=True)
class ProtocolMatch:
    """An authoritative match emitted by a protocol decoder plugin."""

    protocol: str
    decoder: str
    status: str
    confidence: float
    start_sample: int
    end_sample: int
    summary: str
    bitrate: str = ""
    center_offset_hz: float | None = None
    bandwidth_hz: float | None = None
    fields: Mapping[str, object] = field(default_factory=dict)
    payload: bytes | None = None

    def __post_init__(self):
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True)
class PacketObservation:
    """Final pipeline output displayed or recorded by an application."""

    protocol: str
    status: str
    confidence: float
    start_sample: int
    end_sample: int
    sensor_index: int
    summary: str
    bitrate: str = ""
    center_offset_hz: float | None = None
    bandwidth_hz: float | None = None
    clean_onset: bool = True
    decoder: str = "feature classifier"
    fields: Mapping[str, object] = field(default_factory=dict)
    payload: bytes | None = None

    def __post_init__(self):
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def duration_us(self, sample_rate_hz: float) -> float:
        return 1e6 * max(0, self.end_sample - self.start_sample) / sample_rate_hz
