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

Both pulse analyzers import the current shared IQ framework from
`demos.custom.common.iq_application` and `demos.custom.common.iq_waterfall`. The ESPARGOS
Transmitter sidebar uses the beam editor and beamspace components copied from `iq/transmit-beamforming` into `demos/custom/common`.
Calibration runs through `Pool.calibrate()`; reciprocal RX helpers stay within
the calibration selection, and final receive refinement preserves TX epochs.
The receive drawer supports mixed TX/RX boards and updates only receive sensors;
TX-only boards retain their settings. Accumulation displays use unwrapped
sample indices and the active board's generation, including partial captures.

On ESPARGOS radar exit (including Ctrl+C), shutdown waits for in-flight TX and
calibration work, mutes transmission, and disables the reference source. Every
sensor, including RX-only calibration helpers, is brought into RX with a
temporary interval trigger and given an acknowledged silent 1024-sample replay.
This replaces retained user waveforms with a known recipe that the next
calibration can resize and restore. Shutdown restores the capture trigger and
confirms WiFi mode before closing connections. Cleanup failures are logged and
do not prevent attempts on the remaining boards. Forced termination or an
unreachable board can still prevent this hardware cleanup.

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
It is sampled on the actual RX clock. The ESPARGOS variant Fourier-resamples
its selectable TX source rate onto the receive grid; its hardware replay still
runs at 80 MSa/s. Pluto runs at 40 MSa/s. Faster reception of Pluto uses Fourier interpolation of
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

In the ESPARGOS-transmitter variant, Angle, Radar and range–Doppler use all
active receiving antennas in the configured aperture. Board tabs are hidden in
those modes. Power, phase, time, Ping and constellation views retain individual
board tabs labelled with their applied TX/RX roles. With radar accumulation,
these ordinary views display each acquired 256-sample profile independently;
spectral zero-padding does not turn the gaps between profiles into measurements.
The original Pluto-transmitter variant retains its board-local receive views.

A grouped aperture requires the physical board order/orientation and coherent
inter-board calibration. See the four-board configuration below. The library
keeps board and antenna identities distinct throughout collection and correction.

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

Wait for startup synchronization, then open **App → Transmitter** in the side panel:

1. Check **All** beside `espargostx` to transmit on that board. Leave
   `espargosrx` unchecked so its sensors receive. Alternatively, select
   individual firmware antenna IDs (0–7), which are not YAML row/column indices.
2. Set the center frequency, RF gains, output, chirp length/bandwidth/repetition,
   digital gain, azimuth/elevation, squint, and calibration method/path.
3. Click the central **Sync + Calibration** button in the Receive drawer.
   It applies the selected TX/RX roles and settings, prepares the chirp, and
   calibrates both transmission and reception, including when TX is stopped.
4. Click **Apply roles / Start TX** whenever ready. Start/Stop does not calibrate
   or restart unchanged replay engines. Starting before calibration is allowed;
   the status and Sync button indicate that corrections need measurement.
5. Choose the `espargosrx` receive board tab. Use **App → Display**
   to choose power, phase, I/Q, constellation, angle, radar, or Doppler.
   The amplitude spectrum view has been removed from the ESPARGOS TX/RX demo.

The ESPARGOS TX/RX display worker targets **25 Hz (40 ms)**. Power and
relative-phase waterfalls, time/ping I/Q charts, and constellations refresh at
that rate when new complete display data is available. Angle estimates are
computed on those worker updates; the angle curve refreshes at **10 Hz
(100 ms)**. Range–azimuth maps have a **100 ms minimum processing interval**
(typically 120 ms between updates on the 40 ms worker clock). Range–Doppler
maps process only new data or changed settings. The ESPARGOS variant computes
maps in one separate process, with one observation in flight and a 40 ms
minimum rest after each search. Their actual rate depends on history, reference
length and grid size. Hidden chart timers stop.
The receive queue also settles clusters for 200 ms; non-Doppler displays wait
another 200 ms while assembling blocks, so refresh rate is distinct from latency.

**Stop TX** mutes transmission. Closing the demo waits for any active setup
operation, mutes TX, and restores WiFi mode. Leave at least one sensor receiving.
TX channels have empty receive displays and do not count toward receive
completeness, clutter calibration, or angle processing. Angle estimation needs
at least four receive antennas. The ESPARGOS variant automatically uses all
active RX antennas in the configured physical aperture for angle, range–azimuth
and range–Doppler views; those views have no board-selection tabs. The other
views retain individual-board selection, with applied TX/RX roles in the tabs.

For the separate myspargel plus three-board 6×4 aperture, launch:

```sh
python demos/custom/pulse-radar-analyzer/pulse-radar-analyzer-espargi.py \
  --config config/radar-four-boards.yml
```

The architecture file lists all boards, the separate physical aperture groups,
reference master and cable parameters. It does not choose TX/RX roles. Selecting
a TX group in the application selects all its antennas and leaves other groups
receiving; individual sensor settings remain available. Geometric TX steering
cannot span independent apertures with unknown relative positions. Likewise,
spatial RX processing reports a configuration error when receivers span those
independent apertures. Existing `combined-array.array` configurations remain
supported for the two-board workflow.

Calibration checks the boards' live `calib-source` settings: one master and
three slaves must agree with the configured reference master. It measures RX
and TX phase/timing, then the common range origin through the reference network.
Timing-probe gain is lowered on clipping or increased for weak captures, with
bounded retries and unchanged quality checks. Firmware radar observations are
accepted with at least **75% of antenna profiles from each receive board**
over either a single-board or multi-board observation, at least **90% of pulse timestamps**, and
at least **95% of the requested time span**. Each retained pulse needs at least
half the selected aperture. For 216 pulses and three eight-antenna boards,
that means at least 3,888 valid profiles of 5,184, including at least 1,296
from each board, and at least 195 distinct pulse timestamps. All 24 antennas
are used when available; this threshold does not deliberately discard them.
The same thresholds apply to a single RX board (all eight antennas remain selected). Invalid
accumulation counts, coverage fingerprints and mixed synchronization epochs
remain rejected. Missing antenna/pulse data stay masked at their physical
positions and source timestamps, never interpolated or relabelled. Loss reduces
sensitivity and can raise sidelobes; the status reports actual coverage and the
velocity spacing calculated from the retained time span. Instantaneous angle
and range-azimuth views require six antennas per board in their selected
snapshot. Incomplete radar clusters wait at most 1.5 s for late packets;
complete clusters proceed immediately. This increases loss-recovery latency,
not the steady capture interval. Ordinary trigger modes retain their original
settling behavior. Board-local generation counters need not be identical. Physical order is second, third, fourth; all reference cables are
2 m with velocity factor 0.76. A simulator adds its own processing latency;
reference-network range calibration does not remove that external delay.

The receive view stays visible while the nonmodal sidebar is open. Running TX
updates automatically after beam, waveform, chirp, gain, or RF edits; rapid
steering edits are coalesced and the latest direction is applied after the
current hardware operation finishes. TX/RX assignments remain staged until
**Apply roles / Start TX** or **Sync + Calibration**, even if another setting
updates in the meantime. Settings edited while TX is stopped are used at start.

Select one to four beams and drag each numbered marker independently. Each
beam has its own amplitude and squint correction. By default, all beams use
the existing radar pulse settings. Enable **Use IQ demo waveforms** to use the
IQ transmission demo's per-beam CW, two-tone, LFM, noise, QPSK, or I/Q-file
waveforms and 256–8192-sample replay lengths. The single per-beam
**Amplitude / digital gain** field sets the linear amplitude and shows its dB
value. Amplitude is applied once and divided by the number of beams; the legacy
`tx_gain_db` configuration is an alias for beam 1, not an extra multiplier. Beam 1's
unsteered waveform supplies the common receive matched-filter reference;
returns from other waveform types may not correlate with that reference.
The sidebar provides waveform previews and the extended power range switch.
The reference monitor section has been removed. Preview and transmission use
the same quantized source in both pulse and IQ waveform modes.

**TX sampling rate** selects the source waveform rate: 80 (default), 40, 20,
10, 5, or 2.5 MSa/s. It is also configurable with `-o app.fs_hz=20000000`.
Chirp duration and delay are entered in samples at that rate (50 ns each at
20 MSa/s). The ESPARGOS DAC still replays at 80 MSa/s: the demo converts the
source with `scipy.signal.resample`, preserving duration and frequency content.
The allowed source replay lengths shrink at lower rates so the converted
hardware buffer fits within 8192 samples. Changing the source rate requires
Sync + Calibration for the resulting replay timing.

The duration field and lower chirp-length setting control the same sample count.
Chirp bandwidth sets symmetric sweep limits at −BW/2 and +BW/2, using the
existing replay frequency-bin quantization. **Chirps per 256 samples** sets
repetition and caps duration plus delay to one pulse period. Longer replay
buffers repeat this 256-sample pattern. These settings update the preview live,
including silence and delay. IQ-mode LFM uses these same chirp controls.
The receive template Fourier-resamples the complete periodic source onto the
RX grid before extracting the chirp, excluding its delay and tail silence.
It keeps TX steering and amplitude out of the reference,
preserving correlation peak positions and relative receive phase.

The radar defaults to 2400 MHz, gain A/B 0, 40 MHz RF bandwidth, and right
antenna output. Both RF gains permit 0–7. Digital waveform gain defaults to
-20 dB and permits up to 0 dB (full-scale amplitude 1). The RF filter choices
include open (0), 13, 20, 40, and 60 MHz; output choices include isolation.
Chirp sweep bandwidth is limited to the selected TX source sampling rate.
For a 20 MHz radar signal, select a 20 MSa/s TX source rate, 20 MHz chirp
bandwidth, 20 MHz TX RF filter, and 20 MSa/s RX with a suitable RX filter.
The source is interpolated to 80 MSa/s for transmission without interpolation
images. Analog filter rolloff and hardware quantization still apply; the
sampling-rate setting does not change the DAC clock or create a digital IQ
interface to an external simulator.
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

Hardware replay uses 80 MSa/s; the TX source rate is selected in Transmission
setup and receive sample rate is controlled in the Receive drawer.
The chirp count must divide 256 (1, 2, 4,
8, …); chirp length is capped at one pulse period. The uploaded quantized
chirp is also the matched-filter reference, Fourier-resampled onto the actual
RX sample rate when the TX and RX rates differ. This filters out-of-band
content when downsampling rather than aliasing it into additional components.
The converter repeats complete source periods when needed to represent the
exact rate ratio, rather than rounding the sample interval. Each RX channel uses this same
complex reference, excluding TX steering and TX calibration coefficients, so
relative receive phase remains available for angle estimation. Chirp and waveform references update after each successful live TX update.
Changing RX frequency or sample rate invalidates the corresponding calibration;
use Sync + Calibration to measure the new setup. Starting TX never performs
that measurement automatically. Receive fine calibration
still corrects receiver hardware phase and delay. The relative-phase waterfall
corrects both the signal and reference channel before subtracting their phases.
The calibration toggle also applies the correction to I/Q, constellation,
and ping traces; angle/radar processing retains its coherent calibration path.

The RX drawer's Sync action runs combined TX/RX calibration for the selected
settings whether transmission is active or stopped. Active transmission resumes
after successful calibration; stopped transmission stays muted. Start/Stop,
digital gain, chirp edits at an unchanged replay length, and steering
reuse valid calibration. Changes to source rate, replay length, sensor roles or RF settings mark it stale;
use Sync + Calibration to measure the new setup. Both startup and later
calibration use the YAML reference-cable lengths. Calibration acquisition chunks
are excluded from the displays, and incomplete captures are checked against the
epochs of the receive sensors actually present.

Sync + Calibration measures zero-range timing **through Reference** by default,
using the same `pool.calibrate_tx_rx_delay()` probes as the MIMO radar demo.
The timing measurement subtracts modeled cable and board-trace propagation
delays, using the YAML cable lengths and velocity factors. It restores the
prepared chirps, replay length, RX gain, capture settings, and RF routing.

Timing starts at 24 dB RX gain. A weak or inconsistent timing probe is retried
at 42 dB to handle low-level coupling across splitter output ports, especially
when every receiver is on a separate board from every transmitter. Both the
peak-quality threshold and the independent-capture consistency check still
apply; clipped captures and unrelated configuration errors are not retried.
A failed higher-gain measurement leaves TX muted and reports the error.

The timing path can be selected independently of array phase calibration in
the Transmitter sidebar or with `--timing-path`. Optional `antenna_r`/`antenna_l`
timing treats direct-path propagation as negligible and requires no board
positions or distances. Its measured zero includes the actual direct-path delay,
shifting equivalent monostatic range by half the TX-to-RX path length.
The heatmap subtracts the
measured zero-distance sample when labeling range below the disk, using the
current RX sample rate and `c / (2 * sample_rate)` meters per sample. This is
equivalent monostatic range (half the TX-to-target-to-RX path length). Multiple
transmitters use a circular mean of their delays, including TX calibration
corrections, over the 256-sample TX replay. Range remains ambiguous by the pulse
repetition period. Labels before the measured zero can be negative; no absolute
meter labels are shown until timing calibration is valid.

The radar observation start/end fields accept meters after timing calibration.
Each entry snaps to the nearest RX sample and displays the applied meter value;
cutoffs are clamped to the captured chunk with at least one sample between them.
The selected interval stretches to fill the disk, preserving observation-area
zoom. The grid uses adaptive major range rings, five subdivisions per major
interval, and azimuth spokes every 10 degrees. Outlined labels show major ranges
inside and below the disk and angles every 30 degrees.

A shared color-limit slider has separate minimum and maximum handles in dBFS
for both color scales. Select **dB** for logarithmic
power mapping or **Linear power** for linear mapping; the colorbar shows the
corresponding units (dBFS or power relative to full scale squared). Clutter
removal can be calibrated after TX and RX settings settle.

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


## ESPARGOS range–Doppler processing

Select **Range-Doppler (rectangular)** in Display. The radar-specific controls
are observation time (5/10/20/25/50/100/150/250/500 ms and 1/2/5 s), range start/end, velocity start/end,
clutter removal, and color scale/limits. FFT, relative-phase calibration, and
complete-only controls are hidden in map views because they do not control
Doppler resolution or map sample selection. Observation and velocity controls
are shown only in the Doppler view. Exact velocity bounds accept nine decimals.

The processor range-matches each received pulse once against the submitted,
RX-resampled periodic transmit reference. It then estimates Doppler from the
complex matched-filter phase across pulses at each range bin. A trial velocity
changes only this slow-time phase, not the samples within a pulse. Every valid
received chunk and every pulse within that chunk contributes; adjacent chunks
covering one longer pulse are joined. Missing antenna/sample observations have
zero coverage weight rather than becoming measurements of zero.

For regular schedules, a chirp-Z transform evaluates the existing velocity
limits efficiently. Short lattices group the same pulse position across bursts,
so large inter-burst gaps do not require a huge zero-filled pulse array. Missing
slots have zero weight. Irregular schedules use a tiled timestamped DFT of the
same matched-filter profiles. Both paths preserve source time and agree with a
direct slow-time Fourier sum. Hann slow-time weighting and coherent receive
beamforming produce the strongest horizontal beam at each range/velocity cell.

There is no intra-pulse Doppler compensation or range-migration correction.
Long chirps/high Doppler can therefore suffer ordinary matched-filter loss and
range–Doppler coupling. The former sample-domain trial-velocity engine and its
experimental amplitude-inversion/migration routines have been removed.

Observation time limits the age of retained measurements; the internal buffer
holds at most 1024 chunks/lanes. Processing starts with the first valid capture.
The actual aperture and search-grid spacing are reported. The velocity grid
is selected automatically from the requested limits and acquired aperture,
using 129–513 points and including zero for symmetric limits. A broad search
can become coarser than the aperture supports; narrow the limits to inspect
individual peaks. Narrowing bounds is a prior assumption, not proof that an
alias is the true velocity. Packet reordering and computation add latency.

The plot computes the whole requested range of velocities. It does not fill
unmeasured velocities by repeating an FFT texture, and no physical range–Doppler
map is displayed without a transmit reference. Periodic capture gaps can still
produce nearly equal competing velocity peaks. The status reports approximate
capture-cadence alias spacing when applicable; aperture spacing must not be
read as ambiguity-free velocity accuracy.

Manual/static and rolling clutter removal retain their existing behavior.
**Zero Doppler (CPI mean)** remains an alternative for both radar maps and
attenuates stationary and nearby slow returns. Disable it when testing static
target range. In Doppler processing, incomplete firmware averaging windows are
discarded rather than fitted with an unknown response. Use Interval capture
to retain individual pulse information. Complete accumulated lanes retain their
actual source positions. Their matched-filter profiles are timestamped at the
averaging centroid. Known aligned averaging responses provide phase/sign
alignment only; their measured attenuation is retained. Averages that mix
waveform phases are reported as such. Averaging nulls remain blind, and no
amplitude inversion is applied. Individual pulses averaged away by firmware
are not recreated as independent slow-time observations.

The experimental processing selector, capture-cadence compatibility FFT,
burst power averaging, window selector, manual grid/history budgets, partial-
coverage approximation, averaging-loss correction/floor, velocity presets, and
range-migration toggle have been removed from the normal settings. They added
choices without a dependable benefit in the tested hardware workflow. The
single coherent path and observation time replace those overlapping controls.
The pulse-phase processing path is covered by numerical and UI regression
tests. The historical simulator
scene keeps delay fixed while applying Doppler independently. The displayed map
assumes a fixed delay during its observation; shorten the observation for real
targets that traverse range cells. Actual aircraft tracking/range migration is
not established by these fixed-delay simulator tests.

### Resolution and ambiguity

For carrier fc, wavelength lambda=c/fc and coherent aperture T:

* Nominal velocity aperture spacing is `lambda/(2*T)`. Hann weighting broadens
  peaks and capture gaps create competing responses. At 2.4 GHz, 0.625 s gives
  about 0.1 m/s aperture spacing, but this is not a guaranteed unique speed.
* The footer reports approximate **alias spacing inferred from received
  burst/window positions**, not an unambiguous velocity limit. Regular bursts
  separated by `Tgap` generate competing responses approximately
  `lambda/(2*Tgap)` apart. At 80 MSa/s, 2.4 GHz and actual K=8192 this is
  approximately 2.383 m/s. The often-quoted half interval, ±1.191 m/s, is the
  Nyquist interval for one uniformly sampled value per burst, not a guarantee
  for this gapped pulse-phase processor. Unequal coverage and a finite
  velocity grid can change which aliases are visible or strongest. Requested
  firmware cadence alone is insufficient to label an unambiguous band.
  Reducing gaps and increasing burst coverage add information. A longer
  observation sharpens peaks without removing periodic aliases.
* Pulse-to-pulse sampling has the reference Nyquist interval
  `±lambda/(4*PRI)`. Sparse capture cadence introduces additional aliases even
  when every received pulse is used. Raising plot limits does not resolve them.
* Search-grid spacing is `(velocity_max-velocity_min)/(points-1)` and adds no
  physical information beyond the acquired samples.
* Equivalent monostatic range is `c*(delay-delay_zero)/2`. RX sample spacing is
  `c/(2*fs)`, or 1.874 m at 80 MSa/s. Approximate range resolution is
  `c/(2*effective_bandwidth)`, or 7.495 m for an ideal 20 MHz effective bandwidth.
* Repetition makes range ambiguous modulo `c*PRI/2`. One pulse per 256 samples
  at 80 MSa/s repeats every 479.668 m. Calibration zero selects the displayed
  signed lag interval; a return can therefore wrap to a negative range.

### Responsiveness and clutter processing

Both radar maps publish native RGBA textures through thread-safe image
providers. Their asynchronous Image sources retain the previous texture until
the replacement is ready, so the existing shaders never sample an empty image
between updates. Waiting for IQ or clutter warm-up also retains the previous
map. They do not send individual pixel values through QML. Only the
latest texture is retained, avoiding a queue of large frame payloads.
The Doppler worker performs range filtering once per pulse, then uses a fast
slow-time chirp-Z transform where the timestamps permit it. Its irregular-grid
fallback uses direct contractions without BLAS thread spawning. It computes
repeated range lags once and caps the automatic grid at 513 velocities. It
yields between transform tiles, cancels stale searches after view/limit changes,
and rests at least 200 ms after a search.


### Accumulation source timestamps

Doppler accepts complete accumulation windows using the firmware-reported
`nominal_chunks`, which need not equal the window length divided by vector
length. For example, recorded 8192-chunk windows contain 1024 contributions
at eight-chunk spacing. Comparing that count with 8192 wrongly rejects every
window and leaves the plot waiting for timestamp data.

For sparse accumulation, the analyzer verifies candidate regular source
positions against the packet's coverage fingerprint before assigning the
first sample timestamp and averaging step. It models averaging with that
verified step, preserving its real attenuation/null velocities.


For a full 24-antenna, 256-sample radar observation near one update/s at
2.4 GHz, start with 216 pulses, four chunks per sum, and 2,850 cycle windows.
Use interval 24 at 20 MSa/s / 20 MHz bandwidth, or interval 48 at 40 MSa/s /
40 MHz bandwidth. Both give PRI 307.2 microseconds, cycle 0.87552 s,
nominal velocity band +/-101.65 m/s and approximately 0.95 m/s Fourier
spacing. Hann-windowed target separation is wider than that spacing. The
Doppler display uses a grid no coarser than the observation's Fourier spacing;
additional interpolation would not improve physical resolution. Range span
remains about 1,919 m at 20 MSa/s and 959 m at 40 MSa/s. Hardware validation
results and injected packet-loss tests are recorded in the ignored
`demos/radar-array-tests/optimization` folder.

### Angle and screen convention

The radar heatmap retains its original boards’ outward-looking view. Right is
positive azimuth, left is negative, zero is broadside, and elevation is positive
upwards. TX circles, drag controls, numerical steering and the AoA curve use the
same convention. The underlying library direction cosine `u` and calibration
phases retain their original convention: `u = -sin(display_azimuth) * cos(elevation)`.
The conversion happens at the custom analyzer's input/output boundaries; it
must not be applied a second time to library steering or calibration.

The heatmap reports projected horizontal angle `-asin(u)`, while AoA reports
azimuth at the estimated elevation. Their numbers agree at zero elevation;
at nonzero elevation the projection differs. TX/RX bearings can also differ
for separated apertures. The configured antenna geometry must match mounting.

### Radar reception and batch latency

The custom ESPARGOS analyzer uses a bounded UDP reader/reassembly process per
board. This keeps kernel sockets draining while Python assembles observations
or renders. Payloads, integer sums, timestamps, indices, gains and status messages
retain their original protocol and decoding. Other demos and shared framework
files are unchanged. Reader overload is reported as a stream error.

Radar callbacks wait for the selected aperture to complete or settle; ordinary
modes retain their original behavior. There is no additional 200 ms display wait
for radar clusters. Expiry housekeeping runs at most every 20 ms in radar mode;
calibration and other triggers retain the original cadence. Complete observations
still arrive immediately. Doppler computation remains in its existing worker.
The radar heatmap's coordinate conversion already runs in the fragment shader.

A retained partial observation must cover 90% of pulse timestamps, 95% of the
requested time span and 75% of antenna profiles per board. Missing measurements
are masked, not filled or assigned replacement timestamps. When processing falls
behind, the display selects the newest usable batch rather than accumulating an
unbounded queue of stale pictures.
