# espargos-iqsampling

IQ sampling addon for pyespargos: raw synchronized IQ samples from all sensors
(instead of CSI), with timestamp-anchored array-wide time sync, reference-tone
fine calibration, a live multi-channel signal analyzer, and an IQ camera demo
with a combined-power picture-in-picture waterfall.

## Installation

Clone this repository into the `addons/` directory of a pyespargos checkout:

    git clone <this-repository-url> addons/iqsampling

pyespargos discovers and loads it automatically on `import espargos`
(registers the `iq` board capability, `board.iq`).

## Usage

    ./addons/iqsampling/demos/signal-analyzer/signal-analyzer.py <host>[,<host>...]

The first IQ-camera version supports one 2x4 controller and calibrates itself
at startup:

    ./addons/iqsampling/demos/camera/camera.py <host>

It provides real-time FFT and covariance-averaged MUSIC beamforming. MUSIC's
assumed source count is selectable from 1 to 7. Its `Active` bin mode gates
frequency bins by both power above the rolling noise floor and cross-antenna
coherence. `Peak`, a manual absolute-frequency `Band`, and `All` are also
available, with an optional DC exclusion region.
Use `--no-camera` to show beamspace over black while testing without a webcam.

Requires controller firmware with the IQ endpoints (`set_iq_control` etc.).
The processing unit tests and optional live camera validation are:

    PYTHONPATH=addons/iqsampling python -m unittest discover -s addons/iqsampling/tests -p 'test_*.py'
    python addons/iqsampling/tests/live_camera_diagnostic.py <host> --center-mhz 2440 --gain 30

### Accumulated vectors

Firmware trigger mode `3` folds source chunks into signed 32-bit complex
vectors. For example, this config builds a four-chunk (1024-sample) vector and
finishes one accumulation every 32768 source chunks (the accumulation-mode defaults):

```python
pool.apply_config({
    "mode": "iq",
    "gain_mode": "manual",
    "trigger_mode": 3,
    "trigger_config": [4, 32768, 0, 0],
})

handle = pool.add_accumulation_callback(
    lambda cluster: consume(cluster.iq)
)
```

The four trigger words are vector chunks (1–16), stream interval, global-grid
offset, and a reserved zero. Firmware selects vector repetitions on a sparse,
deterministic global-grid lattice (approximately one repetition per 512 source
chunks), giving every sensor identical source coverage without depending on
local bank boundaries or task scheduling. The default callback accepts only
complete array clusters whose per-section source counts and coverage hashes
match on every sensor. Pass a custom predicate to inspect partial/deadline-
aborted windows. `cluster.iq` is count-normalized
`complex64`; packet `i_sum` and `q_sum` retain the exact signed integer sums.

### Array-wide signal trigger

Firmware trigger mode `4` continuously searches the live IQ stream without
forwarding it. Each participating sensor probes a deterministic subset of the
samples and compares `abs(I)` or `abs(Q)` with one shared ADC-count threshold:

```python
pool.apply_config({
    "mode": "iq",
    "trigger_mode": 4,
    # threshold, sensors allowed to request, holdoff ms, capture chunks
    "trigger_config": [12, 0xff, 300, 56],
})

handle = pool.add_signal_capture_callback(consume)
```

The sensors use the shared open-drain BOOT net as a wired-OR detector and
flow-control barrier. Ready/search transitions occur on named global bank
epochs, and a bounded same-bank decision guard makes a hit visible to every
sensor before that bank is selected. Each accepted event contains the complete
56-chunk SRAM bank where detection ran, optionally followed by up to 56 chunks
from the next bank. The configurable 56–112 chunk window spans 716.8–1433.6 us
at 20 MSa/s, 358.4–716.8 us at 40 MSa/s, or 179.2–358.4 us at 80 MSa/s.

Frozen SRAM is copied into raw PSRAM before acquisition resumes. If one
sensor's PSRAM/SPI path falls behind, that sensor keeps BOOT low and the whole
array stops admitting events; old events are never replaced with newer ones.
On the host, `IQSignalAssembler` requires the complete advertised event from
all eight sensors, with FIRST/LAST markers, synchronized-grid metadata, and
contiguous per-sensor raw indices. It rejects an incomplete or inconsistent
event as a unit and never publishes a shortened intersection.

The packet-decoder demo synchronizes the array and uses signal-triggered
captures to detect packet boundaries, run registered protocol decoders, and
conservatively classify anything no decoder claims. Built-in plugins currently
cover 802.11 legacy OFDM (including MAC/FCS for complete non-HT frames),
802.11n mixed-format PHY headers, 802.11ax/802.11be HE/EHT-family preambles,
long-preamble 802.11b DSSS/CCK PLCP headers, and Bluetooth LE advertising
access addresses. The packet list retains
unresolved triggers instead of silently discarding them.

It uses the standard ESPARGOS application shell; the RX drawer changes
frequency, sample rate, filter, gain, RF switch, and Signal
threshold/mask/holdoff live:

```bash
python addons/iqsampling/demos/packet-decoder/packet-decoder.py \
    192.168.0.223 --channel 11 --sample-rate 80 --gain 50 --threshold 24
```

Use `--center-hz 2350000000` for a controlled SDR/HackRF experiment. The
packet decoder defaults to a 112-chunk capture so even the 192-us long 11b
preamble and PLCP header fit at 80 MSa/s. `--record-dir` stores a bounded raw
capture set for offline decoder development. The same RX drawer also makes
Signal available to the regular signal-analyzer demo alongside Interval and
Accumulate.

### Reusing packet decoding

The decoder is independent of Qt and the demo:

```python
from espargos_iqsampling.packet_decoding import default_pipeline

pipeline = default_pipeline()
observations = pipeline.process(
    array_iq, sample_rate_hz, center_frequency_hz, decode_sensor_count=2
)
```

`ProtocolDecoder` is the plugin contract. A plugin receives one antenna's IQ
plus protocol-independent `SignalRegion` boundaries and returns validated
`ProtocolMatch` objects. `PacketDecoderPipeline` handles array sensor
selection, cross-sensor deduplication, feature classification of unclaimed
bursts, and fail-visible unknown events. Consequently, other demos can reuse
the exact same detector/decoders without importing the packet-decoder GUI.

## Layout

- `espargos_iqsampling/` — packet parsing, `iq` board capability, sync,
  IQPool / IQCluster / IQAccumCluster / IQBacklog, reusable waterfall/spatial
  processing, and the demo application modality (IQController)
- `demos/` — signal analyzer, IQ camera, generic packet decoder, and
  shared IQ QML components
- `tests/` — hardware acceptance tests
