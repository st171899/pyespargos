# Pulse radar analyzer

An extended IQ signal analyzer built on the standard `espargos.Pool` / `pool.iq` and
shared demo framework. It adds ping correlation, angle-of-arrival estimation,
a 2D range–azimuth heatmap, and optional Pluto chirp transmission to the power
and phase waterfalls, I/Q traces, constellations, and spectra.

From a pyespargos checkout with the standard demo dependencies installed:

```sh
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer.py -s 192.168.1.2
```

Use `-c config/single-espargos-one.yml` instead of `-s` to load a combined-array
configuration. Start directly in the range–azimuth view with:

```sh
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer.py -s 192.168.1.2 -o app.display_mode=radar
```

Select other views in the Display drawer. The RX drawer provides the standard
frequency, sample-rate, gain, trigger, synchronization, and calibration controls.
Startup synchronizes the array before entering IQ mode; exiting restores
WiFi/CSI mode.

## Upstream integration (26 October 2026)

This branch includes upstream `ESPARGOS/pyespargos` through `5b3eebe`.
The custom radar entry points live in `demos/custom/pulse-radar-analyzer`.
The previous `demos/pulse-radar-analyzer/*.py` launch commands forward here. Upstream demos
now use these paths:

| Demo | Entry point |
| --- | --- |
| IQ signal analyzer | `demos/iq/signal-analyzer/main.py` |
| IQ transmission / beamforming | `demos/iq/transmit-beamforming/main.py` |
| MIMO radar | `demos/iq/mimo-radar/main.py` |
| WiFi demos | `demos/wifi/<demo>/main.py` |

Both pulse analyzers import the current shared IQ framework from
`demos.custom.common.iq_application` and `demos.custom.common.iq_waterfall`. The ESPARGOS
Transmission tab uses the beamspace component copied from `iq/transmit-beamforming` into `demos/custom/common`.
Calibration runs through `Pool.calibrate()`; reciprocal RX helpers stay within
the calibration selection, and final receive refinement preserves TX epochs.
The receive drawer supports mixed TX/RX boards and updates only receive sensors;
TX-only boards retain their settings. Accumulation displays use unwrapped
sample indices and the active board's generation, including partial captures.

The earlier entry points under `adjusted_demos` also use the native receiver
and current QML paths. Their independent processing behavior is retained; use
this directory's pulse analyzers for the current radar feature set.

Run GUI test files in separate processes: they each own a Qt application.
Hardware-free checks cover processing, mixed-role transmission, calibration
recovery, helper selection, receive refinement, accumulation counter wraps,
and both radar interfaces. Live RF calibration, echo measurements, and Pluto
transmission still require the connected hardware.

## Range–azimuth view

The heatmap is always visible when this view is selected. Before IQ arrives,
it shows the colormap floor. It displays uncalibrated samples and partial arrays,
with missing channels contributing zero. Calibration is applied when available.
The status line reports the processing mode, calibration, and available channels.

Only measured samples are mapped to the radial axis; no empty bins are appended.
Repeated ping periods retain their within-window sample positions. In the Pluto
demo, color scaling uses those measured bins, with a minimum 20 dB span so small
noise variations do not fill the entire colormap. The ESPARGOS demo instead uses
the configured dBFS color limits.

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

### Matched filtering, steering, and board selection

Both demos correlate each receive channel with the same complex, conjugated,
time-reversed transmitted chirp: `z_a[k] = sum_n x_a[k+n] conj(s[n])`.
The template comes from the submitted replay samples (including ESPARGOS
source quantization), with chirp settings frozen when that replay is started.
It is sampled on the actual RX clock: ESPARGOS replay runs at 80 MSa/s and
Pluto at 40 MSa/s. Faster reception of Pluto uses Fourier interpolation of
the cyclic DAC sequence. This models an ideal DAC; analog filter distortion
is not measured or included in the template. Output index `k` is the pulse
start, and the peak detector normalizes over those same chirp samples.
Peak lists require a normalized match of at least 0.6 and correlation power
above a robust median-based noise threshold. Only local correlation-magnitude
maxima are listed; there is no chirp-length exclusion zone, so separately
resolved echoes can be listed even when their raw chirps overlap. Neither
correlation nor its match-quality denominator includes surrounding TX silence.
Filtering retains complex channel phase; taking magnitudes before spatial
processing would discard the angle information.

TX steering applies phase/delay weights at transmission, changing illumination
and echo strength. Its commanded angle is not the measured arrival angle.
RX steering happens after pulse compression and receiver phase/delay
calibration: for each candidate direction, the beamformer compensates the
predicted phase progression across the physical antennas, sums their complex
signals, and measures power. The strongest response gives the angle estimate.
The angle chart searches both azimuth and elevation. The heatmap scans columns
at each delay using the current RF wavelength and 6 cm spacing, then adds row
powers. Its horizontal coordinate measures `cos(elevation)*sin(azimuth)`;
it agrees with azimuth for returns in the board's horizontal plane. It does
not separately resolve elevation at each range bin. Calibration, sufficient
aperture, and an isolated far-field return are needed for a meaningful bearing;
TX steering cannot remove multipath or establish a known-angle reference.

The **Receive board** tabs remain available in Angle and Radar modes as well
as the antenna views. Every receive view uses only the selected board's IQ,
calibration, and physical 2×4 antenna positions; mixed-role TX sensors are
excluded. Switching boards clears the previous bearing and map while waiting
for fresh IQ. A board-list or YAML display layout does not establish the
relative physical positions of separate boards.

For a future measured regular multi-board grid, see the `MULTI-BOARD GRID`
comments in `_angle_geometry()` and `_drain_sets()` in both Python demos.
Retain unique `(board, antenna)` identities, collect synchronized data from all
boards, apply each board's calibration, and supply measured offsets and
orientations to the steering calculation. A joint aperture also needs coherent
timing/phase calibration between boards. Enlarging the local geometry alone
would reuse local antenna IDs and give incorrect angles.

## ESPARGOS transmitter and receiver demo

Use `pulse-radar-analyzer-espargi.py` for ESPARGOS transmission. It does not
require Pluto, `adi`, or libiio. The current example config,
[`rx-and-tx-espargos.yml`](../../../config/rx-and-tx-espargos.yml), defines
`espargosrx` at `10.42.0.35` (0 m cable) and `espargostx` at
`10.42.0.119` (1 m cable, velocity factor 0.76). Connect the boards to the
common clock/reference network; the YAML geometry alone does not synchronize
independent hardware. Enable transmit authorization in both boards' web Settings,
as required by the shared IQ transmitter API.

From the repository root, run:

```sh
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer-espargi.py \
    -c config/rx-and-tx-espargos.yml
```

Wait for startup synchronization, then open the **Transmission** tab:

1. Check **All** beside `espargostx` to transmit on that board. Leave
   `espargosrx` unchecked so its sensors receive. Alternatively, select
   individual firmware antenna IDs (0–7), which are not YAML row/column indices.
2. Set the center frequency, RF gains, output, chirp length/bandwidth/repetition,
   digital gain, azimuth/elevation, squint, and calibration method/path.
3. Click the central **Sync + Calibration** button in the Receive drawer.
   It applies the selected TX/RX roles and settings, prepares the chirp, and
   calibrates both transmission and reception, including when TX is stopped.
4. Click **Apply and start TX** whenever ready. Start/Stop does not calibrate
   or restart unchanged replay engines. Starting before calibration is allowed;
   the status and Sync button indicate that corrections need measurement.
5. Switch to **Receive** and choose the `espargosrx` board tab. Use the Display
   drawer to choose power, phase, I/Q, constellation, spectrum, angle, or radar.

**Stop TX** mutes transmission. Closing the demo waits for any active setup
operation, mutes TX, and restores WiFi mode. Leave at least one sensor receiving.
TX channels have empty receive displays and do not count toward receive
completeness, clutter calibration, or angle processing. Angle estimation needs
at least four receive antennas in the selected board. Receive views process one
selected board at a time; they do not combine apertures across board tabs.

The Transmission tab includes the same draggable beamspace view as
`iq-transmit-beamforming`; dragging its marker updates the azimuth and elevation
fields. Apply and start TX sends the new direction to the selected sensors.

The radar defaults to 2400 MHz, gain A/B 0, 40 MHz RF bandwidth, and right
antenna output. Both RF gains permit 0–7. Digital waveform gain defaults to
-20 dB and permits up to 0 dB (full-scale amplitude 1). The RF filter choices
include open (0), 13, 20, 40, and 60 MHz; output choices include isolation.
Chirp sweep bandwidth can reach 80 MHz at the fixed 80 MSa/s replay rate.
The center frequency remains within 2310–2640 MHz for broadband calibration.
Calibration defaults to reciprocity via the reference path. Choosing monitor
calibration selects the required reference path automatically. Reciprocity uses
both boards' IQ sensors for temporary pilots, including sensors assigned to RX.
This supplies cross-board reciprocal links even when only `espargostx` is selected
for transmission. Helpers return to their receive roles afterward, and transmit
corrections apply only to the selected TX sensors. A final broadband RX
measurement then fits the receivers' current phase and delay after all role
switches, without restarting the calibrated transmitters. RX corrections are
bound to the actual capture epochs after restoring the capture rate and trigger;
TX replay epochs and origins are verified unchanged. Pilot measurements use the
chosen calibration path; the default reference path keeps them off the antennas.

All TX settings are available in the tab. The earlier TX command-line options
remain optional initial values for scripted launches, including
`--tx-sensors espargostx` and `--tx-azimuth 25`.

Startup calibrates the roles selected at startup. With the default empty TX
selection, this measures RX only. Selecting transmitters afterward changes
sensor roles and requires a new central calibration. Chirp length, sweep,
repetition, digital amplitude and steering edits preserve calibration when
the replay engines retain their origins. Editing the calibration method or
path only chooses the next measurement and does not invalidate an existing
fit. Changes to RF frequency, RF gains/filter/output, roles, or capture
sample rate require calibration for the resulting hardware state. Calibration
temporarily uses interval captures, so it also works when the selected user
trigger is Accumulate or Signal, and restores that trigger afterward.

TX/RX calibration here measures relative timing within the transmit array
and within the receive array. It does not measure their common absolute
TX-to-RX delay. Sample zero in the correlation chart is the start of the RX
window, while sample zero of the uploaded chirp belongs to the TX replay
engine. Consequently, a direct received chirp can peak away from zero even
with valid array calibration. The plotted index includes the replay/capture
epoch offset, hardware group delay and propagation, and is ambiguous modulo
the pulse repetition period. At 80 MSa/s, 80 samples represent 1 microsecond
on the receive grid; this alone is not a calibrated flight time or range.
Absolute range requires a known-delay TX-to-RX reference measurement. The
demo does not shift the strongest received echo to zero.

Raw and accumulated receive windows use a continuously unwrapped grid index.
The underlying 24-bit source counter wraps every 53.7 seconds at 80 MSa/s;
this does not require a setting change or recalibration. An accumulation
window from another board also cannot reset the selected board's display
merely because its controller has a different configuration generation.

Array calibration removes receiver phase/time offsets. A stable corrected
phase vector can still represent several interfering reflections or near-field
coupling between neighboring arrays. The conventional beamforming maximum
then describes the strongest spatial response, with no guarantee that it is
the bearing of one physical reflector. TX steering changes illumination;
it is not a known-angle receive reference. Absolute angle accuracy needs an
isolated source or reflector with a measured bearing.

Replay uses 80 MSa/s; receive sample rate is controlled in the Receive drawer.
The chirp count must divide 256 (1, 2, 4,
8, …); chirp length is capped at one pulse period. The uploaded quantized
chirp is also the matched-filter reference, subsampled onto the actual RX
sample rate when reception runs below 80 MSa/s. Each RX channel uses this same
complex reference, excluding TX steering and TX calibration coefficients, so
relative receive phase remains available for angle estimation. Chirp settings
are frozen when transmission starts; click **Apply and start TX** to apply edits.
Changing RX frequency or sample rate invalidates the corresponding calibration;
use Sync + Calibration to measure the new setup. Starting TX never performs
that measurement automatically. Receive fine calibration
still corrects receiver hardware phase and delay. The relative-phase waterfall
corrects both the signal and reference channel before subtracting their phases.
The calibration toggle also applies the correction to I/Q, constellation, spectrum,
and ping traces; angle/radar processing retains its coherent calibration path.

The RX drawer's Sync action runs combined TX/RX calibration for the selected
settings whether transmission is active or stopped. Active transmission resumes
after successful calibration; stopped transmission stays muted. Start/Stop,
digital gain, chirp edits at the fixed 256-sample replay length, and steering
reuse valid calibration. Changes to sensor roles or RF settings mark it stale;
use Sync + Calibration to measure the new setup. Both startup and later
calibration use the YAML reference-cable lengths. Calibration acquisition chunks
are excluded from the displays, and incomplete captures are checked against the
epochs of the receive sensors actually present.

Delay bins identify pulse starts at 12.5 ns per sample. Absolute range requires
a timing/path-delay reference; a shared clock alone does not establish the
zero-range bin. Clutter removal can be calibrated after TX and RX settings
settle.

## Clutter removal

In the Display drawer, enable **Clutter removal** and choose a **Clutter mode**.
Both demos subtract complex raw IQ before matched filtering and spatial
processing in Ping, Angle, and Radar views.

* **Manual (200 chunks):** press **Calibrate Clutter** with the scene clear of
  moving targets. The demo averages 200 synchronized 256-sample chunks from
  the selected board's receive antennas, then holds that background fixed.
  Enabling manual mode alone does not start a capture. Press the button again
  to replace the background.
* **Automatic (rolling):** no calibration button is needed. For each antenna,
  subtract the mean of its previous raw acquisitions, then add the current
  acquisition to that history. Retain at most **16 chunks** and at most
  **0.4 seconds** of history. The short history lets old target positions
  leave the estimate quickly; it updates on every acquired chunk, including
  chunks that are not selected for display. Acquisition and display latency
  are additional to this history lifetime.

The first automatic acquisition establishes the background and displays a
zero residual. Static returns and targets that stop moving are gradually
suppressed. Slow motion can also be attenuated: this is adaptive background
subtraction, not a Doppler measurement or a guarantee of detecting every moving
target. Missing antennas do not train the background with artificial zeros.
Backgrounds are discarded when changing boards, clutter modes, or receive
epochs; manual mode then waits for another button-triggered capture.

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
    python demos/custom/pulse-radar-analyzer/tests/test_radar_rendering.py
```

The focused processing checks run without a display or radio hardware:

```sh
python demos/custom/pulse-radar-analyzer/tests/test_radar_processing.py
```

The lecture-equation and analytic array-response checks are:

```sh
python demos/custom/pulse-radar-analyzer/tests/test_beamforming.py
```

The ESPARGOS transmission and UI checks run without radio hardware:

```sh
python demos/custom/pulse-radar-analyzer/tests/test_espargi_tx.py
python demos/custom/pulse-radar-analyzer/tests/test_reciprocity_helpers.py
python demos/custom/pulse-radar-analyzer/tests/test_rx_refinement.py
QT_QPA_PLATFORM=offscreen QT_QUICK_BACKEND=software \
    python demos/custom/pulse-radar-analyzer/tests/test_espargi_ui.py
```
