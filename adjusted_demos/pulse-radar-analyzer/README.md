# Pulse radar analyzer

An extended IQ signal analyzer built on the standard `espargos.IQPool` and
shared demo framework. It adds ping correlation, angle-of-arrival estimation,
a 2D range–azimuth heatmap, and optional Pluto chirp transmission to the power
and phase waterfalls, I/Q traces, constellations, and spectra.

From a pyespargos checkout with the standard demo dependencies installed:

```sh
python demos/pulse-radar-analyzer/pulse-radar-analyzer.py -s 192.168.1.2
```

Use `-c config/single-espargos-one.yml` instead of `-s` to load a combined-array
configuration. Start directly in the range–azimuth view with:

```sh
python demos/pulse-radar-analyzer/pulse-radar-analyzer.py -s 192.168.1.2 -o app.display_mode=radar
```

Select other views in the Display drawer. The RX drawer provides the standard
frequency, sample-rate, gain, trigger, synchronization, and calibration controls.
Startup synchronizes the array before entering IQ mode; exiting restores
WiFi/CSI mode.

## Range–azimuth view

The heatmap is always visible when this view is selected. Before IQ arrives,
it shows the colormap floor. It displays uncalibrated samples and partial arrays,
with missing channels contributing zero. Calibration is applied when available.
The status line reports the processing mode, calibration, and available channels.

Only measured samples are mapped to the radial axis; no empty bins are appended.
With TX active, powers from repeated ping periods are averaged onto one period.
Color scaling uses those measured bins, with a minimum 20 dB span so small
noise variations do not fill the entire colormap.

With Pluto TX inactive, the display uses the full received IQ window; its radial axis
represents sample delay rather than a matched-filter range measurement. With TX
active, it uses chirp correlation. Calibration and a suitable reference waveform
are still needed to interpret the map as physical angles and ranges.

The angle-of-arrival view also works with Pluto TX inactive, using calibrated
received IQ. When TX is active it uses matched-filtered samples. The radar
beamformer uses all antenna columns with uniform weights, and matched-filter
peak indices identify the start sample of the received pulse.

## Azimuth spectrum convention

The angle-of-arrival chart remains a one-dimensional azimuth curve. Its
conventional, uniformly weighted beamformer follows ATWC SS25 Lecture 4,
section 4.1, equations (4.1)–(4.2):

```
psi_x = (2 pi dx / wavelength) cos(elevation) sin(azimuth)
psi_y = (2 pi dy / wavelength) sin(elevation)
y = sum_m sum_n x[m,n] exp(-j (m psi_y + n psi_x)) / antenna_count
P = mean(abs(y)**2)
```

Here `dx = dy = 0.06 m`, and wavelength comes from the current receiver center
frequency. This is the spatial DTFT; at DFT grid points it equals the spatial
FFT described in section 4.4. The implementation evaluates physical look
directions directly using the sample covariance, which is algebraically
identical to averaging the coherent beam output's power.

The internal search uses 0.5-degree azimuth and 1-degree elevation steps.
The plotted curve is a cut through that response at the estimated elevation,
normalized to its maximum. It does not sum hard-masked FFT bins or mix their
elevation-dependent azimuth coordinates, and it adds no smoothing or 2D view.
The receive-phase sign convention is the same as the previous spatial FFT.

## Optional Pluto transmitter

Reception and visualization do not require Pluto or its Python packages.
To use the TX controls, install `pyadi-iio` and libiio (including its Python
bindings) and connect a Pluto at `ip:192.168.2.1`. The demo transmits at 2.4 GHz
with a 40 MSa/s sample rate. The Display drawer configures chirp length,
bandwidth, repetition count, and TX gain.

## Shaders

Compiled `.qsb` shaders are included. After editing the GLSL sources, run
`compile_shader.sh` or `compile_shader.bat` using Qt Shader Baker (`qsb`).
The heatmap requires a graphics backend that supports Qt Quick ShaderEffect;
Mesa software OpenGL works, whereas Qt Quick's `software` backend does not.

## Regression test

The test exercises processing with and without calibration and TX, partial
arrays, the initial empty display, texture updates, and window resizing. It
uses the actual QML and synthetic data without connecting to radio hardware.
On headless Linux with Xvfb and Mesa installed:

```sh
QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a \
    python demos/pulse-radar-analyzer/tests/test_radar_rendering.py
```

The focused processing checks run without a display or radio hardware:

```sh
python demos/pulse-radar-analyzer/tests/test_radar_processing.py
```

The lecture-equation and analytic array-response checks are:

```sh
python demos/pulse-radar-analyzer/tests/test_beamforming.py
```
