"""Threaded priority queue manager supporting speech preemption."""
import queue
import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np

try:
    from schemas import PriorityEnum, TTSRequest, TTSStatusEnum, TTSStatusEvent
    from .player import AudioPlayer
except ImportError:
    from ..schemas import PriorityEnum, TTSRequest, TTSStatusEnum, TTSStatusEvent
    from .player import AudioPlayer


class AudioQueueManager:
    """Coordinates priority audio playback and handles immediate preemption."""

    def __init__(
        self,
        player: Optional[AudioPlayer] = None,
        status_callback: Optional[Callable[[TTSStatusEvent], None]] = None,
    ) -> None:
        self.player = player or AudioPlayer()
        self.status_callback = status_callback
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
        self._entry_counter = 0
        self._lock = threading.Lock()
        self._active_request_id: Optional[str] = None

    def start(self) -> None:
        """Start the background playback processing thread."""
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="TTS-AudioQueue-Worker"
        )
        self._worker_thread.start()

    def stop(self) -> None:
        """Stop playback worker and drain queue."""
        self._running = False
        self.player.stop()
        self.clear_queue()
        if self._worker_thread and self._worker_thread.is_alive():
            self._queue.put((-1, -1, None, None, None, False, 0.0, 0.0))  # Sentinel
            self._worker_thread.join(timeout=1.0)

    def clear_queue(self) -> int:
        """Discard all pending audio requests waiting in queue."""
        discarded = 0
        with self._lock:
            while not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                    req_id = item[2]
                    if req_id and self.status_callback:
                        event = TTSStatusEvent(
                            request_id=req_id,
                            status=TTSStatusEnum.PREEMPTED,
                            latency_ms=0.0,
                            duration_s=0.0,
                            cached=item[5],
                            error_msg="Purged from queue due to preemption",
                        )
                        self.status_callback(event)
                    discarded += 1
                except queue.Empty:
                    break
        return discarded

    def enqueue(
        self,
        request: TTSRequest,
        audio_data: np.ndarray,
        sample_rate: int,
        cached: bool,
        synthesis_time_s: float = 0.0,
    ) -> None:
        """Enqueue synthesized audio for playback according to its priority tier."""
        priority_val = int(request.priority)
        duration_s = len(audio_data) / sample_rate if sample_rate > 0 else 0.0

        # Check for preemption condition
        is_preemptive = request.preempt or (request.priority == PriorityEnum.CRITICAL)

        if is_preemptive:
            # 1. Immediately interrupt currently active speech
            self.player.stop()

            # 2. Clear any pending lower-priority audio from the queue
            self.clear_queue()

            # 3. Notify preemption of active request if any
            if self._active_request_id and self.status_callback:
                preempt_event = TTSStatusEvent(
                    request_id=self._active_request_id,
                    status=TTSStatusEnum.PREEMPTED,
                    latency_ms=0.0,
                    duration_s=0.0,
                    error_msg=f"Preempted by critical request: {request.request_id}",
                )
                self.status_callback(preempt_event)

        with self._lock:
            self._entry_counter += 1
            item = (
                priority_val,
                self._entry_counter,
                request.request_id,
                audio_data,
                sample_rate,
                cached,
                request.timestamp,
                synthesis_time_s,
            )
            self._queue.put(item)

        if self.status_callback:
            queued_event = TTSStatusEvent(
                request_id=request.request_id,
                status=TTSStatusEnum.QUEUED,
                cached=cached,
                duration_s=duration_s,
            )
            self.status_callback(queued_event)

    def _worker_loop(self) -> None:
        """Worker thread processing queue items sequentially by priority."""
        while self._running:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            (
                priority_val,
                _,
                request_id,
                audio_data,
                sample_rate,
                cached,
                req_timestamp,
                synthesis_time_s,
            ) = item

            if request_id is None:
                # Sentinel shutdown
                break

            self._active_request_id = request_id
            duration_s = len(audio_data) / sample_rate if sample_rate > 0 else 0.0
            rtf = synthesis_time_s / duration_s if duration_s > 0 else 0.0
            latency_ms = (time.time() - req_timestamp) * 1000.0

            # Notify playback started
            if self.status_callback:
                started_event = TTSStatusEvent(
                    request_id=request_id,
                    status=TTSStatusEnum.STARTED,
                    cached=cached,
                    latency_ms=round(latency_ms, 2),
                    duration_s=round(duration_s, 3),
                    rtf=round(rtf, 4),
                )
                self.status_callback(started_event)

            # Perform audio playback (blocking until finished or stopped)
            success = self.player.play(audio_data, sample_rate=sample_rate, blocking=True)

            # Notify playback result
            if self.status_callback:
                final_status = TTSStatusEnum.COMPLETED if success else TTSStatusEnum.PREEMPTED
                result_event = TTSStatusEvent(
                    request_id=request_id,
                    status=final_status,
                    cached=cached,
                    latency_ms=round(latency_ms, 2),
                    duration_s=round(duration_s, 3),
                    rtf=round(rtf, 4),
                    error_msg=None if success else "Playback aborted by preemption",
                )
                self.status_callback(result_event)

            self._active_request_id = None
            self._queue.task_done()
