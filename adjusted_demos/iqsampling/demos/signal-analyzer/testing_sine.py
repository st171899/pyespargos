import numpy as np
import adi

sample_rate = 40e6 # Hz
center_freq = 2400e6 # Hz

sdr = adi.Pluto("ip:192.168.2.1")
sdr.sample_rate = int(sample_rate)
sdr.tx_rf_bandwidth = int(sample_rate) # filter cutoff, just set it to the same as sample rate
sdr.tx_lo = int(center_freq)
sdr.tx_hardwaregain_chan0 = 0 # Increase to increase tx power, valid range is -90 to 0 dB

N = 1024 # number of samples to transmit at once
t = np.arange(N)/sample_rate

samples = 0.5*np.exp(2.0j*np.pi*1e6*t) # Simulate a sinusoid of 100 kHz, so it should show up at 2400 MHz at the receiver
samples *= 2**14 # The PlutoSDR expects samples to be between -2^14 and +2^14, not -1 and +1 like some SDRs

# cyclic buffer
sdr.tx_cyclic_buffer = True
# transmit until the program is stopped and destroy cyclic buffer after aborting program execution

sdr.tx(samples) # Transmit the samples
