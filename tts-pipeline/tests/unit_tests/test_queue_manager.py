"""Unit tests for the AudioQueueManager and preemptive priority queue."""
import os
import sys
import time
import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from audio_playback.player import AudioPlayer
from audio_playback.queue_manager import AudioQueueManager
from schemas import PriorityEnum, TTSRequest, TTSStatusEnum, TTSStatusEvent


def create_dummy_audio(duration_s: float, sample_rate: int = 16000) -> np.ndarray:
    return np.zeros(int(duration_s * sample_rate), dtype=np.int16)


def test_priority_ordering():
    player = AudioPlayer(headless=True)
    events = []

    def callback(evt: TTSStatusEvent):
        events.append(evt)

    mgr = AudioQueueManager(player=player, status_callback=callback)

    # Don't start worker thread yet, just inspect queue order
    req_normal = TTSRequest(request_id="req_norm", text="Normal text", priority=PriorityEnum.NORMAL)
    req_critical = TTSRequest(request_id="req_crit", text="Critical alarm", priority=PriorityEnum.CRITICAL)
    req_high = TTSRequest(request_id="req_high", text="High priority", priority=PriorityEnum.HIGH)

    mgr.enqueue(req_normal, create_dummy_audio(0.1), 16000, cached=True)
    mgr.enqueue(req_critical, create_dummy_audio(0.1), 16000, cached=True)
    mgr.enqueue(req_high, create_dummy_audio(0.1), 16000, cached=True)

    # In PriorityQueue, items should come out in order: CRITICAL (0), HIGH (1), NORMAL (2)
    # Notice: when req_critical was enqueued, because it was CRITICAL, it preempted/cleared req_normal!
    item1 = mgr._queue.get_nowait()
    assert item1[2] == "req_crit"

    item2 = mgr._queue.get_nowait()
    assert item2[2] == "req_high"

    assert mgr._queue.empty()


def test_playback_lifecycle_events():
    player = AudioPlayer(headless=True)
    events = []

    def callback(evt: TTSStatusEvent):
        events.append(evt)

    mgr = AudioQueueManager(player=player, status_callback=callback)
    mgr.start()

    req = TTSRequest(request_id="req_life", text="Short beep", priority=PriorityEnum.NORMAL)
    dummy = create_dummy_audio(0.1)

    mgr.enqueue(req, dummy, 16000, cached=True)

    # Wait for completion
    time.sleep(0.3)
    mgr.stop()

    statuses = [e.status for e in events if e.request_id == "req_life"]
    assert TTSStatusEnum.QUEUED in statuses
    assert TTSStatusEnum.STARTED in statuses
    assert TTSStatusEnum.COMPLETED in statuses


def test_emergency_preemption():
    player = AudioPlayer(headless=True)
    events = []

    def callback(evt: TTSStatusEvent):
        events.append(evt)

    mgr = AudioQueueManager(player=player, status_callback=callback)
    mgr.start()

    # Enqueue a long low-priority message (e.g. 2.0 seconds)
    req_long = TTSRequest(request_id="req_long", text="Very long speech...", priority=PriorityEnum.LOW)
    mgr.enqueue(req_long, create_dummy_audio(2.0), 16000, cached=False)

    # Wait for it to start
    time.sleep(0.1)
    assert mgr._active_request_id == "req_long"

    # Now enqueue an immediate emergency stop request (CRITICAL + preempt)
    req_estop = TTSRequest(
        request_id="req_estop",
        text="Emergency stop!",
        priority=PriorityEnum.CRITICAL,
        preempt=True,
    )
    mgr.enqueue(req_estop, create_dummy_audio(0.1), 16000, cached=True)

    # Allow worker to process
    time.sleep(0.3)
    mgr.stop()

    long_statuses = [e.status for e in events if e.request_id == "req_long"]
    estop_statuses = [e.status for e in events if e.request_id == "req_estop"]

    # The long request should have been PREEMPTED
    assert TTSStatusEnum.PREEMPTED in long_statuses
    # The estop request should have COMPLETED
    assert TTSStatusEnum.COMPLETED in estop_statuses
