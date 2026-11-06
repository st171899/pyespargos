# Custom demos

The maintained pulse radar demos live in `pulse-radar-analyzer/`. Shared
Python, QML, and image assets live in `common/`. These two directories are
included in Git; other experiments remain ignored by default.

From the repository root:

```sh
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer.py -s BOARD_HOST
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer-espargi.py -c config/rx-and-tx-espargos.yml
```

Use these maintained paths directly. Existing arguments and configuration
files are supported.
See the [radar README](pulse-radar-analyzer/README.md) for operation and tests.

`common/` contains copies of the current generic demo shell and configuration
manager, IQ application/controller, RX drawer, IQ settings, reference-tone
controls, waterfall renderer, and TX UI/controller/plot/waveform components.
Python and QML imports resolve to these local copies. The radar still uses the
repository's `espargos` library and installed Python/Qt dependencies.

The copies were audited against upstream `f69d2f9` (6 November 2026).
The upstream sources are:

| Custom helper | Upstream source |
| --- | --- |
| Generic application, configuration manager, drawer and assets | `demos/common/` |
| `iq_application.py`, RX controls and waterfall | `demos/iq/common/` (`application.py` becomes `iq_application.py`) |
| `iq_tx_application.py`, `iq_tx_controller.py`, `iq_tx_plot.py`, `iq_tx_waveform.py` and TX QML | `demos/iq/transmit-beamforming/` (Python modules gain the `iq_tx_` prefix) |

These copies already contain the current upstream common code. Intentional
local differences are the custom import paths, configurable Radar trigger
fields (including chunks per sum), receiver-drawer placement, sample-based
radar waveform controls and preview hooks, and
`WaterfallImageProvider.set_frame` for native map textures. Preserve those
additions when refreshing the copies.

The latest upstream update changes only the shared library's UART transport:
it clears stale receive data when connecting or activating the transport,
closes the port after failed activation, and isolates failing stream/log
callbacks. No common-helper API changed, so the custom copies require no
code replacement. Radar accumulation settings, calibration, positive periodic
range, complete-batch validation and counter-wrap handling remain intact.

Both custom demos use the current `Pool.iq` callback interface and unwrapped
`IQAccumCluster.chunk_index` display grid. The library merge adds controller
API v5 support, live WiFi runtime configuration with an API v4 legacy fallback,
and idle retry of failed Signal acknowledgements. The IQ receiver owns Signal
transport maintenance; custom displays should not duplicate acknowledgement or
recovery logic.
