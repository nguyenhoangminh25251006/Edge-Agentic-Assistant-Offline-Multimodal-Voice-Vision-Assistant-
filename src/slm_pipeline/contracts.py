"""contracts.py — Core data schemas, enums, and protocols for the SLM pipeline.

All models use Pydantic v2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import enum
from typing import Any, AsyncIterator, Callable, FrozenSet, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from control_pipeline.schemas.fsm import Action, FsmSnapshot


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Outcome(str, enum.Enum):
    ACTION = "ACTION"
    ANSWER = "ANSWER"
    CLARIFY = "CLARIFY"
    NO_COMMAND = "NO_COMMAND"
    ILLEGAL_STATE = "ILLEGAL_STATE"
    REJECTED_RANGE = "REJECTED_RANGE"
    UNAVAILABLE = "UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    RATE_LIMITED = "RATE_LIMITED"
    ERROR = "ERROR"


class IntentClass(str, enum.Enum):
    DEENERGIZE = "DEENERGIZE"   # stop, pause
    ENERGIZE = "ENERGIZE"       # start, resume
    PARAM = "PARAM"             # speed
    RECOVERY = "RECOVERY"       # recover
    INFO = "INFO"               # status, why, help
    META = "META"               # none


# ---------------------------------------------------------------------------
# Inbound & Internal Request Models
# ---------------------------------------------------------------------------


class UtteranceUnmatched(BaseModel):
    """Payload of event type `stt.unmatched`. Produced by STT.

    Transient in-memory payload: text is NEVER persisted or logged.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=400)
    ingested_mono_ns: int
    wake_confirmed: bool
    asr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class SlmRequest(BaseModel):
    """Internal request structure held in memory only during processing."""
    model_config = ConfigDict(frozen=True)

    request_id: str
    event_id: str
    text: str
    ingested_mono_ns: int
    admitted_mono_ns: int
    state_at_prompt: str


# ---------------------------------------------------------------------------
# Scoring & LLM Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenProb:
    token: str
    prob: float


class IntentScore(BaseModel):
    """Raw probabilities per label, mass, top labels, and margins."""
    model_config = ConfigDict(frozen=True)

    label_probs: dict[str, float]
    mass: float
    top: list[tuple[str, float]]
    p1: float
    p2: float
    margin: float


# ---------------------------------------------------------------------------
# Outbound Event Models
# ---------------------------------------------------------------------------


class SlmResult(BaseModel):
    """Payload of event type `slm.result`. MUST NOT contain utterance text."""
    model_config = ConfigDict(extra="forbid")

    request_id: str
    outcome: Outcome
    label: str | None = None
    intent_class: IntentClass | None = None
    p1: float | None = None
    margin: float | None = None
    reason: str
    state_at_prompt: str | None = None
    state_at_decision: str | None = None
    latency_ms: dict[str, float] = Field(default_factory=dict)
    model_id: str
    prompt_version: str
    action_request_id: str | None = None


class SlmStatus(BaseModel):
    """Payload of event type `slm.status`."""
    model_config = ConfigDict(extra="forbid")

    state: Literal["UP", "DEGRADED", "DOWN", "DISABLED"]
    reason: str
    since_mono_ns: int
    breaker: Literal["CLOSED", "OPEN", "HALF_OPEN"]


# ---------------------------------------------------------------------------
# Action Request (submitted to FSM / Arbiter)
# ---------------------------------------------------------------------------


class ActionRequest(BaseModel):
    """Action requested from the FSM / Safety Invariant Arbiter."""
    model_config = ConfigDict(frozen=True)

    request_id: str
    origin: Literal["VOICE", "HMI", "CV", "SLM", "SYSTEM"] = "SLM"
    action: Action
    params: dict[str, int] = Field(default_factory=dict)
    created_mono_ns: int
    expires_in_ms: int
    context_fsm_version: int | None = None
    confidence: float | None = None
    requires_confirmation: bool = False
    meta: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Decision Bus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionEvent:
    request_id: str
    verdict: Literal["ACCEPT", "REJECT", "NEED_CONFIRMATION", "CONFIRMED", "EXPIRED", "CANCELLED"]
    reason_code: str | None = None
    mcu_result: Literal["ACCEPTED", "REJECTED", "DUPLICATE"] | None = None


# ---------------------------------------------------------------------------
# Protocols (External Ports)
# ---------------------------------------------------------------------------


@runtime_checkable
class FsmPort(Protocol):
    def snapshot(self) -> FsmSnapshot:
        """Synchronous in-memory snapshot of current FSM state."""
        ...

    async def submit(self, req: ActionRequest) -> None:
        """Submit an ActionRequest to the FSM/Arbiter queue."""
        ...


@runtime_checkable
class DecisionBus(Protocol):
    def subscribe(self, request_id: str) -> AsyncIterator[DecisionEvent]:
        """Subscribe to decisions concerning request_id."""
        ...


@runtime_checkable
class Speaker(Protocol):
    async def say(self, key: str, **params: object) -> None:
        """Say a phrase identified by key from the phrase table."""
        ...


@runtime_checkable
class EventSink(Protocol):
    async def publish(self, payload: BaseModel) -> None:
        """Publish an event to downstream subscribers."""
        ...


@runtime_checkable
class Clock(Protocol):
    def now_mono_ns(self) -> int:
        """Current monotonic timestamp in nanoseconds."""
        ...


class LlmError(Exception):
    def __init__(self, kind: Literal["TIMEOUT", "CONNECT", "HTTP_5XX", "BAD_BODY"], detail: str = "") -> None:
        super().__init__(f"LlmError({kind}): {detail}")
        self.kind = kind
        self.detail = detail


@runtime_checkable
class LlmClient(Protocol):
    async def score_first_token(self, prompt: str, *, n_probs: int, timeout_s: float) -> list[TokenProb]:
        ...

    async def generate(self, prompt: str, *, max_tokens: int, temperature: float,
                       stop: list[str], timeout_s: float) -> str:
        ...

    async def tokenize(self, text: str) -> list[int]:
        ...

    async def health(self) -> bool:
        ...

    async def props(self) -> dict[str, Any]:
        ...
