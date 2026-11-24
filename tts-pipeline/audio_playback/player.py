"""Cross-platform audio player with preemption and headless support."""
import threading
import time
from typing import Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None


class AudioPlayer:
    """Low-latency audio playback controller supporting immediate interruption."""

    def __init__(self, headless: bool = False, default_volume: float = 1.0) -> None:
        self.headless = headless or (sd is None)
        self.volume = max(0.0, min(1.0, default_volume))
        self._playing = False
        self._abort_flag = threading.Event()
        self._lock = threading.Lock()
        self._current_stream = None

    @property
    def is_playing(self) -> bool:
        return self._playing

    def stop(self) -> None:
        """Immediately interrupt active playback."""
        with self._lock:
            self._abort_flag.set()
            if not self.headless and sd is not None:
                try:
                    sd.stop()
                except Exception as err:
                    print(f"[AudioPlayer] Error during stop: {err}")
            self._playing = False

    def play(
        self,
        audio_data: np.ndarray,
        sample_rate: int,
        blocking: bool = True,
    ) -> bool:
        """Play PCM audio data.

        Args:
            audio_data: int16 mono numpy array.
            sample_rate: Sample rate in Hz.
            blocking: If True, waits until playback finishes or is aborted.

        Returns:
            bool: True if completed normally, False if aborted/preempted.
        """
        if len(audio_data) == 0:
            return True

        with self._lock:
            self._abort_flag.clear()
            self._playing = True

        duration_s = len(audio_data) / sample_rate

        # Scale volume if requested
        if self.volume < 1.0:
            play_data = (audio_data * self.volume).astype(np.int16)
        else:
            play_data = audio_data

        if self.headless:
            # Simulate playback duration in headless mode with abort checking
            step = 0.05
            elapsed = 0.0
            while elapsed < duration_s:
                if self._abort_flag.is_set():
                    self._playing = False
                    return False
                time.sleep(min(step, duration_s - elapsed))
                elapsed += step
            self._playing = False
            return True

        try:
            sd.play(play_data, samplerate=sample_rate)
            if blocking:
                # Poll for completion or abort
                step = 0.02
                elapsed = 0.0
                while elapsed < duration_s:
                    if self._abort_flag.is_set():
                        sd.stop()
                        self._playing = False
                        return False
                    time.sleep(min(step, duration_s - elapsed))
                    elapsed += step
                sd.wait()
            self._playing = False
            return not self._abort_flag.is_set()

        except Exception as err:
            print(f"[AudioPlayer] Hardware playback failed ({err}), falling back to headless simulation")
            self.headless = True
            self._playing = False
            return False
