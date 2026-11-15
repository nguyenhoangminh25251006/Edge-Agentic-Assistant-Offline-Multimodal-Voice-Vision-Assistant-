"""Audio ingestion module using sounddevice to stream microphone input or simulated audio."""
import os
import queue
import threading
import time
import wave
from typing import Optional

import sounddevice as sd

try:
    from config import SAMPLE_RATE, CHANNELS, FRAME_MS, BYTES_PER_FRAME
except ImportError:
    from ..config import SAMPLE_RATE, CHANNELS, FRAME_MS, BYTES_PER_FRAME


class AudioStream:
    """Captures microphone audio or simulates streaming from an audio file."""

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        frame_ms: int = FRAME_MS,
        device: Optional[int] = None,
        audio_file: Optional[str] = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_size = int(sample_rate * (frame_ms / 1000.0))
        self.device = device
        self.audio_file = audio_file or os.getenv("AUDIO_FILE")
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self._stream: Optional[sd.RawInputStream] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._exhausted = False

    @property
    def is_file_mode(self) -> bool:
        """Return True if streaming from a pre-recorded audio file."""
        return bool(self.audio_file and os.path.exists(self.audio_file))

    @property
    def is_exhausted(self) -> bool:
        """Return True if the file stream has finished feeding and queue is empty."""
        return self._exhausted and self._queue.empty()

    def _callback(self, indata: bytes, frames: int, time_info, status) -> None:
        """Callback invoked by PortAudio for each incoming audio chunk."""
        if status:
            print(f"[audio_stream] PortAudio status: {status}")
        self._enqueue(bytes(indata))

    def _enqueue(self, chunk: bytes) -> None:
        """Insert chunk into queue, dropping the oldest frame on overflow."""
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(chunk)

    def _file_worker(self) -> None:
        """Feeds audio frames from file at 50 Hz, followed by trailing silence."""
        chunk_bytes = self.block_size * 2
        frame_sec = self.block_size / self.sample_rate
        try:
            with wave.open(self.audio_file, "rb") as wf:
                raw_bytes = wf.readframes(wf.getnframes())
        except Exception as e:
            print(f"[audio_stream] Failed reading audio file '{self.audio_file}': {e}")
            self._exhausted = True
            return

        offset = 0
        while self._running and offset < len(raw_bytes):
            t0 = time.perf_counter()
            chunk = raw_bytes[offset : offset + chunk_bytes]
            if len(chunk) < chunk_bytes:
                chunk = chunk + b"\x00" * (chunk_bytes - len(chunk))
            offset += chunk_bytes
            self._enqueue(chunk)
            dt = time.perf_counter() - t0
            if frame_sec - dt > 0:
                time.sleep(frame_sec - dt)

        # Feed trailing silence frames so VAD hangover finishes cleanly
        silence = b"\x00" * chunk_bytes
        for _ in range(35):
            if not self._running:
                break
            self._enqueue(silence)
            time.sleep(frame_sec)

        self._exhausted = True

    def start(self) -> None:
        """Start the audio input stream."""
        if self.is_file_mode:
            self._running = True
            self._exhausted = False
            self._thread = threading.Thread(target=self._file_worker, daemon=True)
            self._thread.start()
            print(f"[audio_stream] Streaming from file: '{self.audio_file}' ({FRAME_MS} ms frames)")
            return

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                device=self.device,
                channels=CHANNELS,
                dtype="int16",
                callback=self._callback,
            )
            self._stream.start()
            print(
                f"[audio_stream] Stream started at {self.sample_rate} Hz, "
                f"{self.block_size} samples per frame ({FRAME_MS} ms)"
            )
        except sd.PortAudioError as e:
            hint = (
                "\n[audio_stream] No physical microphone available. "
                "You can feed an audio file by passing: python main.py <path_to_wav> "
                "or setting AUDIO_FILE=<path_to_wav>."
            )
            raise sd.PortAudioError(f"{e}{hint}") from e

    def read_chunk(self, timeout: float = 2.0) -> bytes:
        """Retrieve the next audio chunk from the queue, blocking up to timeout seconds."""
        if self.is_file_mode and self.is_exhausted:
            raise EOFError("Audio file stream completed")
        return self._queue.get(timeout=timeout)

    def stop(self) -> None:
        """Stop and close the audio stream."""
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            print("[audio_stream] Stream stopped.")
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
