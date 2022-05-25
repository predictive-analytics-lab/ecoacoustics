from typing import Union

import numpy as np
import torch
from torch import nn

__all__ = ["LogMelSpectrogramNp"]


class LogMelSpectrogramNp(nn.Module):
    # Mel spectrum constants and functions.

    def __init__(
        self,
        sample_rate: int = 16000,
        stft_window_length_seconds: float = 0.025,
        stft_hop_length_seconds: float = 0.010,
        log_offset: float = 0.01,  # Offset used for stabilized log of input mel-spectrogram.
        num_mel_bins: int = 64,  # Frequency bands in input mel-spectrogram patch.
        mel_min_hz: float = 125,
        mel_max_hz: float = 7500,
        mel_break_freq_hertz: float = 700.0,
        mel_high_freq_q: float = 1127.0,
    ):
        super().__init__()
        self.window_length_samples = int(round(sample_rate * stft_window_length_seconds))
        self.hop_length_samples = int(round(sample_rate * stft_hop_length_seconds))
        self.fft_length = 2 ** int(np.ceil(np.log(self.window_length_samples) / np.log(2.0)))
        self.log_offset = log_offset
        self.audio_sample_rate = sample_rate
        self.num_mel_bins = num_mel_bins
        self.lower_edge_hertz = mel_min_hz
        self.upper_edge_hertz = mel_max_hz
        self.mel_break_freq_hertz = mel_break_freq_hertz
        self.mel_high_freq_q = mel_high_freq_q

    def forward(self, data: torch.Tensor) -> torch.Tensor:
        """Convert waveform to a log magnitude mel-frequency spectrogram.
        Args:
          data: 1D np.array of waveform data.
          audio_sample_rate: The sampling rate of data.
          log_offset: Add this to values when taking log to avoid -Infs.
          window_length_secs: Duration of each window to analyze.
          hop_length_secs: Advance between successive analysis windows.
          **kwargs: Additional arguments to pass to spectrogram_to_mel_matrix.
        Returns:
          2D np.array of (num_frames, num_mel_bins) consisting of log mel filterbank
          magnitudes for successive frames.
        """
        np_data = data.numpy().squeeze(axis=0)
        spectrogram = self.stft_magnitude(
            np_data,
            fft_length=self.fft_length,
            hop_length=self.hop_length_samples,
            window_length=self.window_length_samples,
        )
        mel_spectrogram = np.dot(
            spectrogram,
            self.spectrogram_to_mel_matrix(
                num_spectrogram_bins=spectrogram.shape[1],
                audio_sample_rate=self.audio_sample_rate,
                num_mel_bins=self.num_mel_bins,
                lower_edge_hertz=self.lower_edge_hertz,
                upper_edge_hertz=self.upper_edge_hertz,
            ),
        )
        return torch.as_tensor(np.log(mel_spectrogram + self.log_offset), device=data.device)

    def stft_magnitude(
        self,
        signal: np.ndarray,
        fft_length: int,
        hop_length: int,
        window_length: int,
    ) -> np.ndarray:
        """Calculate the short-time Fourier transform magnitude.
        Args:
          signal: 1D np.array of the input time-domain signal.
          fft_length: Size of the FFT to apply.
          hop_length: Advance (in samples) between each frame passed to FFT.
          window_length: Length of each block of samples to pass to FFT.
        Returns:
          2D np.array where each row contains the magnitudes of the fft_length/2+1
          unique values of the FFT for the corresponding frame of input samples.
        """
        frames = self.frame(signal, window_length, hop_length)
        # Apply frame window to each frame. We use a periodic Hann (cosine of period
        # window_length) instead of the symmetric Hann of np.hanning (period
        # window_length-1).
        window = self.periodic_hann(window_length)
        windowed_frames = frames * window
        return np.abs(np.fft.rfft(windowed_frames, fft_length))

    def frame(self, data: np.ndarray, window_length: int, hop_length: int) -> np.ndarray:
        """Convert array into a sequence of successive possibly overlapping frames.
        An n-dimensional array of shape (num_samples, ...) is converted into an
        (n+1)-D array of shape (num_frames, window_length, ...), where each frame
        starts hop_length points after the preceding one.
        This is accomplished using stride_tricks, so the original data is not
        copied.  However, there is no zero-padding, so any incomplete frames at the
        end are not included.
        Args:
          data: np.array of dimension N >= 1.
          window_length: Number of samples in each frame.
          hop_length: Advance (in samples) between each window.
        Returns:
          (N+1)-D np.array with as many rows as there are complete frames that can be
          extracted.
        """
        num_samples = data.shape[0]
        num_frames = 1 + int(np.floor((num_samples - window_length) / hop_length))
        shape = (num_frames, window_length) + data.shape[1:]
        strides = (data.strides[0] * hop_length,) + data.strides
        return np.lib.stride_tricks.as_strided(data, shape=shape, strides=strides)

    def periodic_hann(self, window_length: int) -> np.ndarray:
        """Calculate a "periodic" Hann window.
        The classic Hann window is defined as a raised cosine that starts and
        ends on zero, and where every value appears twice, except the middle
        point for an odd-length window.  Matlab calls this a "symmetric" window
        and np.hanning() returns it.  However, for Fourier analysis, this
        actually represents just over one cycle of a period N-1 cosine, and
        thus is not compactly expressed on a length-N Fourier basis.  Instead,
        it's better to use a raised cosine that ends just before the final
        zero value - i.e. a complete cycle of a period-N cosine.  Matlab
        calls this a "periodic" window. This routine calculates it.
        Args:
          window_length: The number of points in the returned window.
        Returns:
          A 1D np.array containing the periodic hann window.
        """
        return 0.5 - (0.5 * np.cos(2 * np.pi / window_length * np.arange(window_length)))

    def spectrogram_to_mel_matrix(
        self,
        num_mel_bins: int = 20,
        num_spectrogram_bins: int = 129,
        audio_sample_rate: int = 8000,
        lower_edge_hertz: float = 125.0,
        upper_edge_hertz: float = 3800.0,
    ) -> np.ndarray:
        """Return a matrix that can post-multiply spectrogram rows to make mel.
        Returns a np.array matrix A that can be used to post-multiply a matrix S of
        spectrogram values (STFT magnitudes) arranged as frames x bins to generate a
        "mel spectrogram" M of frames x num_mel_bins.  M = S A.
        The classic HTK algorithm exploits the complementarity of adjacent mel bands
        to multiply each FFT bin by only one mel weight, then add it, with positive
        and negative signs, to the two adjacent mel bands to which that bin
        contributes.  Here, by expressing this operation as a matrix multiply, we go
        from num_fft multiplies per frame (plus around 2*num_fft adds) to around
        num_fft^2 multiplies and adds.  However, because these are all presumably
        accomplished in a single call to np.dot(), it's not clear which approach is
        faster in Python.  The matrix multiplication has the attraction of being more
        general and flexible, and much easier to read.
        Args:
          num_mel_bins: How many bands in the resulting mel spectrum.  This is
            the number of columns in the output matrix.
          num_spectrogram_bins: How many bins there are in the source spectrogram
            data, which is understood to be fft_size/2 + 1, i.e. the spectrogram
            only contains the nonredundant FFT bins.
          audio_sample_rate: Samples per second of the audio at the input to the
            spectrogram. We need this to figure out the actual frequencies for
            each spectrogram bin, which dictates how they are mapped into mel.
          lower_edge_hertz: Lower bound on the frequencies to be included in the mel
            spectrum.  This corresponds to the lower edge of the lowest triangular
            band.
          upper_edge_hertz: The desired top edge of the highest frequency band.
        Returns:
          An np.array with shape (num_spectrogram_bins, num_mel_bins).
        Raises:
          ValueError: if frequency edges are incorrectly ordered or out of range.
        """
        nyquist_hertz = audio_sample_rate / 2.0
        if lower_edge_hertz < 0.0:
            raise ValueError(f"lower_edge_hertz {lower_edge_hertz:.1f} must be >= 0")
        if lower_edge_hertz >= upper_edge_hertz:
            raise ValueError(
                f"lower_edge_hertz {lower_edge_hertz:.1f} >= upper_edge_hertz {upper_edge_hertz:.1f}"
            )
        if upper_edge_hertz > nyquist_hertz:
            raise ValueError(
                f'upper_edge_hertz {upper_edge_hertz:.1f} is greater than Nyquist {nyquist_hertz:.1f}'
            )
        spectrogram_bins_hertz: np.ndarray = np.linspace(0.0, nyquist_hertz, num_spectrogram_bins)
        spectrogram_bins_mel = self.hertz_to_mel(spectrogram_bins_hertz)
        # The i'th mel band (starting from i=1) has center frequency
        # band_edges_mel[i], lower edge band_edges_mel[i-1], and higher edge
        # band_edges_mel[i+1].  Thus, we need num_mel_bins + 2 values in
        # the band_edges_mel arrays.
        band_edges_mel = np.linspace(
            self.hertz_to_mel(lower_edge_hertz),
            self.hertz_to_mel(upper_edge_hertz),
            num_mel_bins + 2,
        )
        # Matrix to post-multiply feature arrays whose rows are num_spectrogram_bins
        # of spectrogram values.
        mel_weights_matrix = np.empty((num_spectrogram_bins, num_mel_bins))
        for i in range(num_mel_bins):
            lower_edge_mel, center_mel, upper_edge_mel = band_edges_mel[i : i + 3]
            # Calculate lower and upper slopes for every spectrogram bin.
            # Line segments are linear in the *mel* domain, not hertz.
            lower_slope = (spectrogram_bins_mel - lower_edge_mel) / (center_mel - lower_edge_mel)
            upper_slope = (upper_edge_mel - spectrogram_bins_mel) / (upper_edge_mel - center_mel)
            # .. then intersect them with each other and zero.
            mel_weights_matrix[:, i] = np.maximum(0.0, np.minimum(lower_slope, upper_slope))
        # HTK excludes the spectrogram DC bin; make sure it always gets a zero
        # coefficient.
        mel_weights_matrix[0, :] = 0.0
        return mel_weights_matrix

    def hertz_to_mel(self, frequencies_hertz: Union[float, np.ndarray]) -> np.ndarray:
        """Convert frequencies to mel scale using HTK formula.
        Args:
          frequencies_hertz: Scalar or np.array of frequencies in hertz.
        Returns:
          Object of same size as frequencies_hertz containing corresponding values
          on the mel scale.
        """
        return self.mel_high_freq_q * np.log(1.0 + (frequencies_hertz / self.mel_break_freq_hertz))
