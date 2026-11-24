"""Abstract base class for TTS synthesis engines."""
from abc import ABC, abstractmethod
from typing import Tuple
import numpy as np


class BaseTTSEngine(ABC):
    """Abstract interface for speech synthesis implementations."""

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Native audio sample rate (Hz)."""
        pass

    @abstractmethod
    def synthesize(self, text: str) -> Tuple[np.ndarray, int, float, float]:
        """Synthesize text into raw 16-bit PCM numpy array.

        Args:
            text: Plain text string to synthesize.

        Returns:
            Tuple of:
                - audio_data: np.ndarray (int16 PCM, mono)
                - sample_rate: int (Hz)
                - synthesis_time_s: float (seconds elapsed during generation)
                - rtf: float (Real-Time Factor = synthesis_time / audio_duration)
        """
        pass
