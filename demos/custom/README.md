# Custom demos

The maintained pulse radar demos live in `pulse-radar-analyzer/`. Shared
Python, QML, and image assets live in `common/`. These two directories are
included in Git; other experiments remain ignored by default.

From the repository root:

```sh
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer.py -s BOARD_HOST
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer-espargi.py -c config/rx-and-tx-espargos.yml
```

The previous `demos/pulse-radar-analyzer/*.py` commands continue to work through
compatibility launchers. Existing arguments and configuration files are supported.
See the [radar README](pulse-radar-analyzer/README.md) for operation and tests.

`common/` contains copies of the current generic demo shell and configuration
manager, IQ application/controller, RX drawer, IQ settings, reference-tone
controls, waterfall renderer, and TX UI/controller/plot/waveform components.
Python and QML imports resolve to these local copies. The radar still uses the
repository's `espargos` library and installed Python/Qt dependencies.

The copies come from `demos/common`, `demos/iq/common`, and
`demos/iq/transmit-beamforming` after integration of upstream `5b3eebe`.
When integrating future upstream changes, compare these sources and port their
updates into `common/`, preserving the local import paths and radar hooks.
