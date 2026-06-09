"""Real-time mel-scale spectrogram bar computation."""

import numpy as np

NUM_BARS = 28
FFT_SIZE = 1024
MIN_FREQ = 50
MAX_FREQ = 8000
SMOOTHING = 0.3


def _mel(f):
    return 2595.0 * np.log10(1.0 + f / 700.0)


def _inv_mel(m):
    return 700.0 * (10.0 ** (m / 2595.0) - 1.0)


class SpectrogramProcessor:
    """Computes 24 mel-scale frequency bars from audio chunks."""

    def __init__(self, sample_rate):
        self.sample_rate = sample_rate
        self._prev_bars = np.zeros(NUM_BARS, dtype=np.float64)
        self._window = np.hanning(FFT_SIZE)

        mel_min = _mel(MIN_FREQ)
        mel_max = _mel(MAX_FREQ)
        mel_points = np.linspace(mel_min, mel_max, NUM_BARS + 2)
        freq_points = _inv_mel(mel_points)
        self._bin_edges = (freq_points * FFT_SIZE / sample_rate).astype(int)
        self._bin_edges = np.clip(self._bin_edges, 0, FFT_SIZE // 2)

    def process(self, chunk):
        """Process audio chunk, return normalized bar heights (0.0-1.0, length NUM_BARS)."""
        if len(chunk) < FFT_SIZE:
            padded = np.zeros(FFT_SIZE, dtype=np.float64)
            padded[:len(chunk)] = chunk
        else:
            padded = chunk[-FFT_SIZE:].astype(np.float64)

        windowed = padded * self._window
        spectrum = np.abs(np.fft.rfft(windowed))

        bars = np.zeros(NUM_BARS, dtype=np.float64)
        for i in range(NUM_BARS):
            lo = self._bin_edges[i]
            hi = self._bin_edges[i + 2]
            if hi <= lo:
                hi = lo + 1
            bars[i] = np.mean(spectrum[lo:hi])

        # Log scale
        bars = np.log1p(bars)

        # Normalize
        max_val = bars.max()
        if max_val > 1e-6:
            bars /= max_val

        # Smooth with previous frame
        bars = SMOOTHING * self._prev_bars + (1.0 - SMOOTHING) * bars
        self._prev_bars = bars.copy()

        return bars
