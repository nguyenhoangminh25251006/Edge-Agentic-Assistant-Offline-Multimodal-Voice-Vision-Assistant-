"""fakes.py — Test fakes for LLM, FSM, DecisionBus, Speaker, EventSink, and Clock."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator
from pydantic import BaseModel

from control_pipeline.schemas.fsm import Action
from slm_pipeline.contracts import (
    ActionRequest,
    DecisionEvent,
    FsmSnapshot,
    LlmError,
    TokenProb,
)


class FakeClock:
    """Controllable monotonic clock."""

    def __init__(self, initial_mono_ns: int = 1_000_000_000) -> None:
        self._mono_ns = initial_mono_ns

    def now_mono_ns(self) -> int:
        return self._mono_ns

    def advance_ms(self, ms: float) -> None:
        self._mono_ns += int(ms * 1_000_000)


class FakeSpeaker:
    """Records all spoken phrase keys and parameters."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def say(self, key: str, **params: object) -> None:
        self.calls.append((key, params))


class FakeEventSink:
    """Records all published events."""

    def __init__(self) -> None:
        self.events: list[BaseModel] = []

    async def publish(self, payload: BaseModel) -> None:
        self.events.append(payload)


class FakeFsm:
    """In-memory FsmPort recording submissions and serving controllable snapshots."""

    def __init__(self, initial_state: str = "IDLE") -> None:
        self.current_state = initial_state
        self.fsm_version = 1
        self.submitted: list[ActionRequest] = []
        self.last_reason: str | None = None

    def snapshot(self) -> FsmSnapshot:
        state_upper = self.current_state.upper()
        permitted: set[Action] = set()
        if state_upper == "IDLE":
            permitted.add(Action.START)
        elif state_upper == "RUNNING":
            permitted.update({Action.STOP, Action.PAUSE, Action.SET_SPEED})
        elif state_upper == "PAUSED":
            permitted.update({Action.RESUME, Action.STOP})
        elif state_upper == "JAMMED":
            permitted.add(Action.RECOVER)
        elif state_upper == "RECOVERING":
            permitted.add(Action.STOP)

        return FsmSnapshot(
            state=state_upper,
            fsm_version=self.fsm_version,
            permitted=frozenset(permitted),
            last_reason=self.last_reason,
        )

    async def submit(self, req: ActionRequest) -> None:
        self.submitted.append(req)


class FakeDecisionBus:
    """Controllable DecisionBus that feeds scripted events to subscribers.

    Events queued before or after subscribe() are delivered to active subscribers.
    The generator stays open (blocking on queue.get) until a terminal verdict arrives,
    matching the behaviour of InMemoryDecisionBus.
    """

    def __init__(self) -> None:
        self.events_to_emit: list[DecisionEvent] = []
        self._subscribers: dict[str, list[asyncio.Queue[DecisionEvent]]] = {}

    def queue_event(self, event: DecisionEvent) -> None:
        """Enqueue an event and push it to any active subscriber for that request_id."""
        self.events_to_emit.append(event)
        for q in self._subscribers.get(event.request_id, []):
            q.put_nowait(event)

    async def subscribe(self, request_id: str) -> AsyncIterator[DecisionEvent]:
        q: asyncio.Queue[DecisionEvent] = asyncio.Queue()
        self._subscribers.setdefault(request_id, []).append(q)
        # Replay any events already queued for this request
        for ev in self.events_to_emit:
            if ev.request_id == request_id:
                q.put_nowait(ev)
        try:
            while True:
                ev = await q.get()
                yield ev
                if ev.verdict in ("ACCEPT", "REJECT", "EXPIRED", "CONFIRMED", "CANCELLED"):
                    return
        finally:
            subs = self._subscribers.get(request_id, [])
            if q in subs:
                subs.remove(q)


class FakeLlm:
    """Scriptable LlmClient fake for unit and property testing."""

    def __init__(self) -> None:
        self.scored_tokens: list[TokenProb] = [
            TokenProb(token="start", prob=0.92),
            TokenProb(token="resume", prob=0.03),
            TokenProb(token="stop", prob=0.01),
        ]
        self.token_map: dict[str, list[int]] = {
            "start": [101],
            "resume": [102],
            "pause": [103],
            "stop": [104],
            "speed": [105],
            "recover": [106],
            "status": [107],
            "why": [108],
            "help": [109],
            "none": [110],
        }
        self.generated_text: str = "Operating normally."
        self.health_ok: bool = True
        self.props_data: dict[str, Any] = {"model": "qwen3-0.5b-gguf", "build": "b3000"}
        self.delay_s: float = 0.0
        self.error_to_raise: Exception | None = None
        self.calls: list[str] = []

    async def score_first_token(
        self, prompt: str, *, n_probs: int, timeout_s: float
    ) -> list[TokenProb]:
        self.calls.append(f"score_first_token:{prompt[:30]}")
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.error_to_raise:
            raise self.error_to_raise
        return list(self.scored_tokens)

    async def generate(
        self, prompt: str, *, max_tokens: int, temperature: float, stop: list[str], timeout_s: float
    ) -> str:
        self.calls.append(f"generate:{prompt[:30]}")
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        if self.error_to_raise:
            raise self.error_to_raise
        return self.generated_text

    async def tokenize(self, text: str) -> list[int]:
        self.calls.append(f"tokenize:{text}")
        if self.error_to_raise:
            raise self.error_to_raise
        return self.token_map.get(text.strip().lower(), [999])

    async def health(self) -> bool:
        return self.health_ok

    async def props(self) -> dict[str, Any]:
        return self.props_data
