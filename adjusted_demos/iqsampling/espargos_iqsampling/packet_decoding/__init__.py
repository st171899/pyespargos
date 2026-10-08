"""Extensible radio-packet detection, classification, and decoding."""

from .base import PacketDecoderPipeline, ProtocolDecoder, classify_features
from .detection import EnergyBurstDetector, extract_features
from .model import PacketObservation, ProtocolMatch, SignalFeatures, SignalRegion
from .bluetooth import BluetoothLEAdvertisingDecoder
from .wifi import LegacyOFDMDecoder, WiFiDSSSDecoder


def default_pipeline() -> PacketDecoderPipeline:
    """Construct the standard decoder registry in authoritative-first order."""

    return PacketDecoderPipeline(decoders=(LegacyOFDMDecoder(), WiFiDSSSDecoder(), BluetoothLEAdvertisingDecoder()))


__all__ = [
    "BluetoothLEAdvertisingDecoder",
    "EnergyBurstDetector",
    "LegacyOFDMDecoder",
    "WiFiDSSSDecoder",
    "PacketDecoderPipeline",
    "PacketObservation",
    "ProtocolDecoder",
    "ProtocolMatch",
    "SignalFeatures",
    "SignalRegion",
    "classify_features",
    "default_pipeline",
    "extract_features",
]
