"""Event schemas emitted by the voice pipeline over ZeroMQ."""
import time
from typing import Any, Dict
from pydantic import BaseModel, Field


class VoiceBaseEvent(BaseModel):
    """Base event payload containing source identifier, sequence counter, and timestamp."""
    source: str = "container-stt"
    seq: int = Field(..., description="Monotonically increasing sequence number")
    timestamp: float = Field(default_factory=time.time, description="Unix timestamp in seconds")


class VoiceStopEvent(VoiceBaseEvent):
    """Event emitted immediately when a stop keyword is detected."""
    event_type: str = "VOICE_STOP_DETECTED"
    confidence: float = Field(..., ge=0.0, le=1.0, description="Detection confidence score")
    raw_transcript: str = "stop"


class VoiceWakeEvent(VoiceBaseEvent):
    """Event emitted when a wake phrase is detected to open an active listening session."""
    event_type: str = "VOICE_WAKE_DETECTED"
    confidence: float = Field(..., ge=0.0, le=1.0, description="Detection confidence score")
    wake_phrase: str


class VoiceIntentEvent(VoiceBaseEvent):
    """Event emitted when a spoken command is transcribed and mapped to an intent code."""
    event_type: str = "VOICE_INTENT"
    raw_transcript: str
    intent: str = Field(..., description="Canonical command intent code")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Transcription confidence score")
    latency_ms: float = Field(..., description="Processing time from end of speech to emission in milliseconds")
    parameters: Dict[str, Any] = Field(default_factory=dict, description="Command parameters extracted from transcript")
