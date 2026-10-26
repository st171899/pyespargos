#!/usr/bin/env python

"""IQ-sampling addon for pyespargos.

This package re-exports the native ``iq`` types, which provide
IQ capture configuration, reference tone control, sync anchors and the IQ
chunk stream (``board.iq``).

The IQ data path mirrors the CSI stack: :class:`.IQReceiver` assembles the boards'
chunk streams into synchronized :class:`.IQCluster` snapshots and manages the
IQ-mode lifecycle (configuration, array-wide time sync, reference tone, fine
calibration), and :class:`.IQBacklog` stores delivered clusters in a ring
buffer. The demo application modality (:mod:`espargos_iqsampling.iq_application`)
is imported explicitly by demos, since it depends on Qt and the pyespargos
demo framework.
"""

import espargos as _espargos

from . import iq_tone
from . import spatial
from espargos.board_iq import IQCapability
from espargos.iq_backlog import IQBacklog, IQBacklogFilter
from espargos.iq_accum_cluster import IQAccumCluster
from espargos.iq_cluster import CHUNK_SAMPLES, IQCluster
from espargos.iq_signal_capture import (
    IQ_SIGNAL_BANK_CHUNKS,
    IQ_SIGNAL_MAX_CAPTURE_CHUNKS,
    IQSignalAssembler,
    IQSignalCapture,
)
from .wifi_legacy import LegacyOFDMFrame, decode_legacy_ofdm, decode_legacy_ofdm_all
from espargos.iq_packet import (
    IQ_ACCUM_TYPE_HEADER,
    IQ_CHUNK_FLAG_SIGNAL_CAPTURE,
    IQ_CHUNK_FLAG_SIGNAL_FIRST,
    IQ_CHUNK_FLAG_SIGNAL_LAST,
    IQ_CHUNK_SAMPLE_WORDS,
    IQ_CHUNK_TYPE_HEADER,
    IQAccumPacket,
    IQChunkPacket,
)
from espargos.iq_receiver import DECIM_TO_FS, SENSOR_COUNT, IQCalibrationError, IQReceiver, iq_receiver_config

# IQ is registered by pyespargos. Never replace its current board capability.
