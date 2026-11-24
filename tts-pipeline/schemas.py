"""Event and request schemas for the TTS pipeline over ZeroMQ.

Follows Pydantic v2 conventions and provides strict serialization
for inter-container communication with container-orchestrator.
"""
from enum import Enum, IntEnum
import time
from typing import Optional
from pydantic import BaseModel, Field


class PriorityEnum(IntEnum):
    """Urgency level for speech alerts.

    Lower integer values correspond to higher priority.
    """
    CRITICAL = 0   # E-stop, motor stall, physical jam (preempts active audio)
    HIGH = 1       # PCB defect detected, current/thermal overload warning
    NORMAL = 2     # Operational transitions (started, paused, resumed)
    LOW = 3        # Diagnostic summaries, guided tutorial steps


class TTSStatusEnum(str, Enum):
    """Execution status of a speech request."""
    QUEUED = "QUEUED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    PREEMPTED = "PREEMPTED"
    FAILED = "FAILED"


class TTSRequest(BaseModel):
    """Incoming speech request payload sent by the orchestrator."""
    request_id: str = Field(..., description="Unique correlation identifier for tracking")
    text: str = Field(..., min_length=1, description="Text string to be spoken")
    priority: PriorityEnum = Field(default=PriorityEnum.NORMAL, description="Playback priority tier")
    preempt: bool = Field(default=False, description="Whether to abort current active playback immediately")
    use_cache_only: bool = Field(default=False, description="Only play if available in pre-rendered cache")
    timestamp: float = Field(default_factory=time.time, description="Unix timestamp of request generation")


class TTSStatusEvent(BaseModel):
    """Status feedback emitted by the TTS pipeline to the orchestrator."""
    source: str = "container-tts"
    request_id: str = Field(..., description="Correlating request identifier")
    status: TTSStatusEnum = Field(..., description="Current status of the speech request")
    cached: bool = Field(default=False, description="Whether audio was served from pre-rendered cache")
    latency_ms: float = Field(default=0.0, description="Latency from request to playback start in ms")
    duration_s: float = Field(default=0.0, description="Duration of the audio clip in seconds")
    rtf: float = Field(default=0.0, description="Real-Time Factor (synthesis_time / duration_s)")
    timestamp: float = Field(default_factory=time.time, description="Unix timestamp of event emission")
    error_msg: Optional[str] = Field(default=None, description="Error details if status is FAILED")
