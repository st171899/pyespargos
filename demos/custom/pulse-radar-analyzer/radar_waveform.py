"""One source recipe for radar transmission, preview and receive references."""
import numpy as np
from espargos.waveforms import IQSamples
from demos.custom.common.iq_tx_waveform import synthesized_waveform

DAC_RATE_HZ = 80e6
TX_SAMPLE_RATES = (80e6, 40e6, 20e6, 10e6, 5e6, 2.5e6)


def replay_samples(config, samples):
    """Map source samples to a supported, duration-preserving DAC buffer."""
    rate = float(config.get("fs_hz", DAC_RATE_HZ))
    if rate not in TX_SAMPLE_RATES:
        raise ValueError("TX sampling rate must be 80, 40, 20, 10, 5 or 2.5 MSa/s")
    count = int(samples * DAC_RATE_HZ / rate)
    if samples not in (256, 512, 1024, 2048, 4096, 8192) or count > 8192:
        raise ValueError("Replay exceeds 8192 DAC samples; reduce source samples or increase TX sampling rate")
    return count


def prepare_source(source, config, samples):
    """Return DAC samples and the corresponding source-rate reference.

    Share interpolation headroom between the preview, TX and matched filter.
    A common scale avoids clipping any overshoot, which would create harmonics.
    """
    from radar_signal import resample_replay

    replay_samples(config, samples)
    native = source.synthesize(samples)
    rate = config.get("fs_hz", DAC_RATE_HZ)
    if rate == DAC_RATE_HZ:
        return source, native
    replay = resample_replay(native, rate, DAC_RATE_HZ)
    scale = max(1.0, float(np.max(np.abs(replay.real))), float(np.max(np.abs(replay.imag))))
    return IQSamples(replay / scale), native / scale


def pulse_waveform(cfg):
    chirp_frames = int(cfg["chirp_frames"])
    bw_hz = float(cfg["bandwidth_hz"])
    pings_per_256 = int(cfg["pings_per_256"])

    # Read dynamic sampling rate from the interface config
    fs_hz = float(cfg.get("fs_hz", 80e6))
    if not np.isfinite(fs_hz) or fs_hz <= 0:
        raise ValueError("TX sampling rate must be positive and finite")
    if not np.isfinite(bw_hz) or not 0 < bw_hz <= fs_hz:
        raise ValueError("Chirp bandwidth must be positive and no greater than the TX sampling rate")

    CHUNK_SAMPLES = 256
    if pings_per_256 <= 0 or CHUNK_SAMPLES % pings_per_256:
        raise ValueError("Chirps per 256 must divide 256 (1, 2, 4, 8, …, 256)")
    burst_length = CHUNK_SAMPLES // pings_per_256
    chirp_frames = max(1, min(chirp_frames, burst_length))

    # 1. Replicate the C code's discrete bin conversion logic
    f_bin_resolution = fs_hz / CHUNK_SAMPLES
    s_start_bin = int(np.round((-bw_hz / 2.0) / f_bin_resolution))
    s_stop_bin = int(np.round((bw_hz / 2.0) / f_bin_resolution))

    # 2. Map those discrete bins back out to physical analog frequencies
    # This step aligns your continuous template with the hardware's true output
    f_start = s_start_bin * f_bin_resolution
    f_end = s_stop_bin * f_bin_resolution

    # 3. Generate high-precision continuous phase tracking arrays
    t = np.arange(chirp_frames, dtype=np.float64) / fs_hz
    chirp_duration = chirp_frames / fs_hz

    # Calculate sweep rate using the updated discrete bounds
    k = (f_end - f_start) / chirp_duration
    phase = 2.0 * np.pi * (f_start * t + 0.5 * k * (t**2))

    # 4. Continuous exponential synthesis
    active_chirp = np.exp(1j * phase).astype(np.complex64)

    # Pad tail silence to complete the burst window
    zero_samples = max(0, burst_length - chirp_frames)
    single_burst = np.concatenate((active_chirp, np.zeros(zero_samples, dtype=np.complex64)))

    # Repeat block across the full buffer sequence
    waveform_256 = np.tile(single_burst, pings_per_256)[:CHUNK_SAMPLES]

    # Scale to the UI amplitude target configuration
    amplitude_target = float(cfg.get("amplitude_target", 1.0))
    waveform_scaled = waveform_256 * amplitude_target

    return waveform_scaled


def beam_source(beam, config, samples):
    editor = config.get("waveform_editor", False)
    if not editor or beam["type"] == "lfm":
        cfg = dict(config)
        period = 256 // int(cfg["pings_per_256"])
        delay = min(max(0, int(beam.get("delay_samples", 0))), period - 1)
        cfg["chirp_frames"] = min(int(cfg["chirp_frames"]), period - delay)
        block = pulse_waveform(cfg)
        block = np.roll(block, delay)
        return IQSamples(np.tile(block, (samples + 255)//256)[:samples])
    if beam["type"] == "file":
        return IQSamples.from_file(beam["file"], normalize=True)
    # Built-in recipes synthesize on an 80 MSa/s grid. Scale their physical
    # parameters so these samples represent the selected source sampling rate.
    rate = float(config.get("fs_hz", DAC_RATE_HZ))
    beam = dict(beam)
    for key in ("offset_mhz", "first_mhz", "second_mhz", "start_mhz", "stop_mhz"):
        if key in beam:
            beam[key] *= DAC_RATE_HZ / rate
    for key in ("duration_us", "delay_us"):
        if key in beam:
            beam[key] *= rate / DAC_RATE_HZ
    return synthesized_waveform(beam)


def reference_config(config, settings):
    """Describe the active part of beam 1 without steering or amplitude."""
    config = dict(config)
    beams = settings.get("beams", [])
    pulse = not settings.get("waveform_editor") or (beams and beams[0]["type"] == "lfm")
    config["waveform_editor"] = not pulse
    if pulse:
        period = 256 // int(config["pings_per_256"])
        delay = min(max(0, int(beams[0].get("delay_samples", 0))) if beams else 0, period-1)
        config["reference_delay_samples"] = delay
        config["chirp_frames"] = min(int(config["chirp_frames"]), period-delay)
    return config
