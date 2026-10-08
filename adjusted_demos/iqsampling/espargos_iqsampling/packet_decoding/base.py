"""Plugin interface and protocol-neutral packet-decoding pipeline."""

from __future__ import annotations

from abc import ABC, abstractmethod
import numpy as np

from .detection import EnergyBurstDetector, extract_features
from .model import PacketObservation, ProtocolMatch, SignalFeatures, SignalRegion


class ProtocolDecoder(ABC):
    """A decoder that recognizes a protocol by an authoritative PHY header."""

    name = "unnamed decoder"

    @abstractmethod
    def decode(
        self,
        samples: np.ndarray,
        sample_rate_hz: float,
        center_frequency_hz: float,
        regions: list[SignalRegion],
    ) -> list[ProtocolMatch]:
        raise NotImplementedError


def classify_features(features: SignalFeatures, duration_us: float) -> tuple[str, float, str]:
    """Conservatively classify a burst not claimed by an exact decoder."""

    bandwidth_mhz = features.occupied_bandwidth_hz / 1e6
    if 10.0 <= bandwidth_mhz <= 25.0 and features.ofdm_short_repeat_score >= 0.42:
        confidence = min(0.94, 0.62 + 0.45 * (features.ofdm_short_repeat_score - 0.42))
        return "IEEE 802.11 OFDM", confidence, "OFDM short-training repetition; header not decoded"
    if 12.0 <= bandwidth_mhz <= 35.0 and duration_us >= 8.0:
        return (
            "IEEE 802.11 wideband candidate",
            0.58,
            "20 MHz channel-sized burst; OFDM/DSSS header not validated",
        )
    if 0.35 <= bandwidth_mhz <= 2.8 and features.envelope_variation <= 0.75 and features.binary_fm_score >= 0.82:
        return "Bluetooth-class GFSK", 0.62, "narrowband constant-envelope binary-FM burst"
    if 0.7 <= bandwidth_mhz <= 4.0 and duration_us >= 20.0:
        return "Narrowband packet", 0.45, "packet-like narrowband burst; protocol header not recognized"
    if bandwidth_mhz > 8.0:
        return "Unknown wideband packet", 0.35, "isolated wideband burst"
    return "Unknown packet", 0.25, "isolated energy burst"


class PacketDecoderPipeline:
    """Detect once, decode with plugins, then classify only unmatched bursts."""

    def __init__(self, decoders=(), detector=None):
        self.decoders = tuple(decoders)
        self.detector = detector if detector is not None else EnergyBurstDetector()

    @staticmethod
    def _overlaps(match: ProtocolMatch, region: SignalRegion, tolerance: int) -> bool:
        return match.start_sample < region.end_sample + tolerance and match.end_sample > region.start_sample - tolerance

    def process(
        self,
        signals: np.ndarray,
        sample_rate_hz: float,
        center_frequency_hz: float,
        decode_sensor_count: int = 1,
    ) -> list[PacketObservation]:
        signals = np.asarray(signals)
        if signals.ndim == 1:
            signals = signals[np.newaxis, :]
        signals = signals.astype(np.complex64, copy=False)
        signals = signals - np.mean(signals, axis=1, keepdims=True)
        powers = np.mean(np.abs(signals) ** 2, axis=1)
        sensor_order = np.argsort(powers)[::-1]
        regions = self.detector.detect(signals, sample_rate_hz)

        matches: list[tuple[ProtocolMatch, int]] = []
        for sensor_index in sensor_order[: max(1, int(decode_sensor_count))]:
            sensor_index = int(sensor_index)
            for decoder in self.decoders:
                for match in decoder.decode(
                    signals[sensor_index],
                    sample_rate_hz,
                    center_frequency_hz,
                    regions,
                ):
                    matches.append((match, sensor_index))

        # Antenna and conjugation diversity regularly produce the same decode.
        # Bucket start times loosely and retain the strongest result.
        unique = {}
        bucket = max(1, round(sample_rate_hz * 0.5e-6))
        for match, sensor_index in matches:
            payload_key = match.payload if match.payload is not None else match.summary
            key = (match.protocol, round(match.start_sample / bucket), payload_key)
            old = unique.get(key)
            if old is None or match.confidence > old[0].confidence:
                unique[key] = (match, sensor_index)

        observations = []
        claimed_regions = set()
        overlap_tolerance = max(1, round(sample_rate_hz * 2e-6))
        for match, sensor_index in unique.values():
            overlapping = [(index, region) for index, region in enumerate(regions) if self._overlaps(match, region, overlap_tolerance)]
            clean_onset = any(region.clean_onset for _, region in overlapping)
            claimed_regions.update(index for index, _ in overlapping)
            observations.append(
                PacketObservation(
                    protocol=match.protocol,
                    status=match.status,
                    confidence=match.confidence,
                    start_sample=match.start_sample,
                    end_sample=match.end_sample,
                    sensor_index=sensor_index,
                    summary=match.summary,
                    bitrate=match.bitrate,
                    center_offset_hz=match.center_offset_hz,
                    bandwidth_hz=match.bandwidth_hz,
                    clean_onset=clean_onset,
                    decoder=match.decoder,
                    fields=match.fields,
                    payload=match.payload,
                )
            )

        strongest = int(sensor_order[0]) if sensor_order.size else 0
        for index, region in enumerate(regions):
            if index in claimed_regions:
                continue
            features = extract_features(signals[strongest], sample_rate_hz, region)
            duration_us = 1e6 * region.sample_count / sample_rate_hz
            protocol, confidence, summary = classify_features(features, duration_us)
            observations.append(
                PacketObservation(
                    protocol=protocol,
                    status="classified" if confidence >= 0.4 else "unknown",
                    confidence=confidence,
                    start_sample=region.start_sample,
                    end_sample=region.end_sample,
                    sensor_index=strongest,
                    summary=summary,
                    center_offset_hz=features.center_offset_hz,
                    bandwidth_hz=features.occupied_bandwidth_hz,
                    clean_onset=region.clean_onset,
                    fields={
                        "OFDM repetition": round(features.ofdm_short_repeat_score, 3),
                        "spectral flatness": round(features.spectral_flatness, 3),
                        "binary FM score": round(features.binary_fm_score, 3),
                    },
                )
            )

        if not observations:
            observations.append(
                PacketObservation(
                    protocol="Unresolved trigger",
                    status="unknown",
                    confidence=0.0,
                    start_sample=0,
                    end_sample=signals.shape[1],
                    sensor_index=strongest,
                    summary="no packet boundary with preceding silence was found",
                    clean_onset=False,
                )
            )
        observations.sort(key=lambda result: result.start_sample)
        return observations
