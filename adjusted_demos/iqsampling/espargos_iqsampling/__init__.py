#!/usr/bin/env python

"""IQ-sampling addon for pyespargos.

Importing this package registers the ``iq`` Board capability, which provides
IQ capture configuration, reference tone control, sync anchors and the IQ
chunk stream (``board.iq``).

The IQ data path mirrors the CSI stack: :class:`.IQPool` assembles the boards'
chunk streams into synchronized :class:`.IQCluster` snapshots and manages the
IQ-mode lifecycle (configuration, array-wide time sync, reference tone, fine
calibration), and :class:`.IQBacklog` stores delivered clusters in a ring
buffer. The demo application modality (:mod:`espargos_iqsampling.iq_application`)
is imported explicitly by demos, since it depends on Qt and the pyespargos
demo framework.
"""

import espargos as _espargos

from . import board_iq
from . import iq_backlog
from . import iq_cluster
from . import iq_packet
from . import iq_pool
from . import iq_sync
from . import iq_tone
from . import spatial
from .board_iq import IQCapability
from .iq_backlog import IQBacklog, IQBacklogFilter
from .iq_accum_cluster import IQAccumCluster
from .iq_cluster import CHUNK_SAMPLES, IQCluster
from .iq_signal_capture import (
    IQ_SIGNAL_BANK_CHUNKS,
    IQ_SIGNAL_MAX_CAPTURE_CHUNKS,
    IQSignalAssembler,
    IQSignalCapture,
)
from .wifi_legacy import LegacyOFDMFrame, decode_legacy_ofdm, decode_legacy_ofdm_all
from .iq_packet import (
    IQ_ACCUM_TYPE_HEADER,
    IQ_CHUNK_FLAG_SIGNAL_CAPTURE,
    IQ_CHUNK_FLAG_SIGNAL_FIRST,
    IQ_CHUNK_FLAG_SIGNAL_LAST,
    IQ_CHUNK_SAMPLE_WORDS,
    IQ_CHUNK_TYPE_HEADER,
    IQAccumPacket,
    IQChunkPacket,
)
from .iq_pool import DECIM_TO_FS, SENSOR_COUNT, IQCalibrationError, IQPool, iq_receiver_config

_espargos.Board.register_capability("iq", IQCapability)
