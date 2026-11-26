"""pipeline.py — Integration and pipeline wiring for the EAA orchestrator block.

Wires together:
1. FSMEngine (table-driven deterministic state machine)
2. SafetyInvariantArbiter (safety gate enforcing physical invariants)
3. McuBridgeWorker (B-03 v1.0 MPU-MCU protocol bridge)
4. FeedableSensorSource (in-memory sensor provider connecting telemetry to the arbiter)

Provides an easily modifiable input interface:
- `submit_event(trigger, origin, payload)`: Injects perception/HMI events.
- `feed_sensor(reading)`: Injects or overrides sensor readings.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from control_pipeline.bridge.mock_mcu import MockMcu
from control_pipeline.bridge.transport import ByteTransport, MockMcuTransport
from control_pipeline.bridge.worker import McuBridgeWorker
from control_pipeline.config import OrchestratorSettings
from control_pipeline.contracts import FsmObserver
from control_pipeline.fsm.engine import FSMEngine
from control_pipeline.safety.arbiter import SafetyInvariantArbiter
from control_pipeline.schemas.arbiter import ArbiterDecision, ReasonCode, SensorReading
from control_pipeline.schemas.bridge import BridgeHealth, TelemetryFrame
from control_pipeline.schemas.fsm import (
    Action,
    FsmEvent,
    FsmSnapshot,
    FsmStateChanged,
    FsmTrigger,
    SystemState,
)
from slm_pipeline.contracts import ActionRequest, DecisionEvent


class InMemoryDecisionBus:
    """In-memory pub-sub bus for DecisionEvent streaming."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[DecisionEvent]]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, event: DecisionEvent) -> None:
        req_id = event.request_id
        queues = list(self._subscribers.get(req_id, []))
        for q in queues:
            await q.put(event)

    async def subscribe(self, request_id: str) -> AsyncIterator[DecisionEvent]:
        q: asyncio.Queue[DecisionEvent] = asyncio.Queue()
        if request_id not in self._subscribers:
            self._subscribers[request_id] = []
        self._subscribers[request_id].append(q)
        try:
            while True:
                ev = await q.get()
                yield ev
                if ev.verdict in ("ACCEPT", "REJECT", "EXPIRED", "CONFIRMED", "CANCELLED"):
                    break
        finally:
            if request_id in self._subscribers and q in self._subscribers[request_id]:
                self._subscribers[request_id].remove(q)
                if not self._subscribers[request_id]:
                    del self._subscribers[request_id]


class FeedableSensorSource:
    """An in-memory SensorSource that can be dynamically updated by bridge telemetry or tests."""

    def __init__(self, initial_reading: SensorReading | None = None) -> None:
        self._reading: SensorReading = initial_reading or SensorReading(
            estop_closed=True,
            pe_clear=True,
            hb_valid=True,
            fault_latched=False,
            motor_enabled=False,
            sensors_ok=True,
            motor_current_ma=0.0,
            conveyor_speed_mms=0.0,
            infrared_temp_celsius=25.0,
            sensor_timestamp_ms=None,
            link_state="up",
            last_frame_age_ms=10.0,
        )
        self._lock = asyncio.Lock()

    async def latest(self) -> SensorReading:
        async with self._lock:
            return self._reading

    async def feed(self, reading: SensorReading) -> None:
        async with self._lock:
            self._reading = reading


class OrchestratorPipeline(FsmObserver):
    """Integrated pipeline managing FSM, Safety Arbiter, and Bridge Worker."""

    def __init__(
        self,
        settings: OrchestratorSettings | None = None,
        *,
        transport: ByteTransport | None = None,
        mock_mcu: MockMcu | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._settings = settings or OrchestratorSettings()
        self._logger = logger or logging.getLogger(__name__)

        # Sensor source
        self._sensor_source = FeedableSensorSource()

        # Arbiter
        self._arbiter = SafetyInvariantArbiter(
            self._sensor_source,
            max_current_ma=self._settings.max_current_ma,
            max_temp_celsius=self._settings.max_temp_celsius,
            min_speed_mms=self._settings.min_speed_mms,
            max_speed_mms=self._settings.max_speed_mms,
            min_stt_confidence=self._settings.min_stt_confidence,
            max_intent_age_ms=self._settings.max_intent_age_ms,
            logger=self._logger.getChild("arbiter"),
        )

        # FSM Engine
        init_state = SystemState(self._settings.initial_state)
        self._fsm = FSMEngine(
            initial_state=init_state,
            observers=[self],
            anti_flap_lockout_s=self._settings.anti_flap_lockout_s,
            logger=self._logger.getChild("fsm"),
        )

        # Transport and Bridge
        self._mock_mcu = mock_mcu
        if transport is not None:
            self._transport = transport
        else:
            mpu_tr, mcu_tr = MockMcuTransport.linked_pair()
            self._transport = mpu_tr
            if self._mock_mcu is None:
                self._mock_mcu = MockMcu(mcu_tr, logger=self._logger.getChild("mock_mcu"))

        self._bridge = McuBridgeWorker(
            self._transport,
            telemetry_listener=self._on_bridge_telemetry,
            link_lost_callback=self._on_bridge_link_lost,
            heartbeat_interval_s=self._settings.heartbeat_interval_s,
            hello_retry_interval_s=self._settings.hello_retry_interval_s,
            stale_threshold_ms=self._settings.stale_threshold_ms,
            lost_threshold_ms=self._settings.lost_threshold_ms,
            default_ttl_ms=self._settings.default_ttl_ms,
            logger=self._logger.getChild("bridge"),
        )

        self._decision_bus = InMemoryDecisionBus()
        self._last_arbiter_decision: ArbiterDecision | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def fsm_state(self) -> SystemState:
        return self._fsm.state

    @property
    def fsm(self) -> FSMEngine:
        return self._fsm

    @property
    def decision_bus(self) -> InMemoryDecisionBus:
        return self._decision_bus

    @property
    def arbiter(self) -> SafetyInvariantArbiter:
        return self._arbiter

    def snapshot(self) -> FsmSnapshot:
        """FS-SLM-1: Export snapshot directly through orchestrator pipeline."""
        return self._fsm.get_snapshot()

    @property
    def bridge(self) -> McuBridgeWorker:
        return self._bridge

    @property
    def bridge_health(self) -> BridgeHealth:
        return self._bridge.health

    @property
    def arbiter_stats(self) -> dict[str, Any]:
        return self._arbiter.stats()

    @property
    def mock_mcu(self) -> MockMcu | None:
        return self._mock_mcu

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start all components and optional MockMcu."""
        if self._running:
            return
        self._running = True

        if self._mock_mcu is not None:
            await self._mock_mcu.start()

        await self._bridge.start()
        self._logger.info("OrchestratorPipeline started (initial_state=%s)", self._fsm.state)

    async def stop(self) -> None:
        """Stop all components cleanly."""
        self._running = False
        await self._bridge.stop()
        if self._mock_mcu is not None:
            await self._mock_mcu.stop()
        self._logger.info("OrchestratorPipeline stopped")

    # ------------------------------------------------------------------
    # Easily Modifiable Input Interface
    # ------------------------------------------------------------------

    async def submit_event(
        self,
        trigger: str | FsmTrigger,
        origin: str = "ui",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inject an event into the pipeline.

        External modules (ZeroMQ EventRouter, REST API, WebSockets, or test scripts)
        invoke this method to drive the pipeline.

        Args:
            trigger: FsmTrigger enum or string matching trigger token.
            origin:  Identifier of the caller ("ui", "stt", "cv", "mcu", "system").
            payload: Optional dictionary carrying metadata (e.g. setpoint_mms, confidence).

        Returns:
            Dictionary containing transition outcome, state, and arbiter decision.
        """
        payload = payload or {}
        trig_enum = FsmTrigger(trigger) if isinstance(trigger, str) else trigger

        # Special case: SET_SPEED_CMD in RUNNING does not trigger a state transition,
        # but needs Arbiter validation and Bridge dispatch.
        if trig_enum == FsmTrigger.SET_SPEED_CMD and self._fsm.state == SystemState.RUNNING:
            setpoint = payload.get("setpoint_mms", self._settings.min_speed_mms)
            decision = await self._arbiter.evaluate(Action.SET_SPEED, setpoint, origin, payload)
            self._last_arbiter_decision = decision
            if decision.permitted:
                await self._bridge.send_action(Action.SET_SPEED, setpoint)
                return {
                    "accepted": True,
                    "state": self._fsm.state.value,
                    "arbiter_decision": decision.model_dump(),
                    "detail": f"Speed set to {setpoint} mm/s",
                }
            else:
                return {
                    "accepted": False,
                    "state": self._fsm.state.value,
                    "arbiter_decision": decision.model_dump(),
                    "detail": f"Speed change blocked by arbiter: {decision.reason_name}",
                }

        # Standard transition path
        event = FsmEvent(
            trigger=trig_enum,
            origin=origin,
            payload=payload,
            timestamp=time.time(),
        )

        state_changed = await self._fsm.process(event)
        if state_changed is None:
            return {
                "accepted": False,
                "state": self._fsm.state.value,
                "arbiter_decision": None,
                "detail": f"Trigger {trig_enum.value} is not permitted from {self._fsm.state.value}",
            }

        decision_dump = (
            self._last_arbiter_decision.model_dump() if self._last_arbiter_decision else None
        )
        return {
            "accepted": True,
            "state": self._fsm.state.value,
            "previous_state": state_changed.previous.value,
            "arbiter_decision": decision_dump,
            "detail": f"Transitioned from {state_changed.previous.value} to {state_changed.current.value}",
        }

    async def feed_sensor(self, reading: SensorReading) -> None:
        """Manually override or supply sensor readings."""
        await self._sensor_source.feed(reading)

    async def submit_action_request(self, req: ActionRequest) -> None:
        """Submit an ActionRequest fulfilling the FsmPort protocol."""
        if req.action == Action.STOP:
            res = await self.submit_event(FsmTrigger.STOP_CMD, origin=req.origin, payload=req.meta)
            decision = self._last_arbiter_decision
            verdict = "ACCEPT" if res.get("accepted") else "REJECT"
            reason = decision.reason_name if decision else None
            await self._decision_bus.publish(
                DecisionEvent(
                    request_id=req.request_id,
                    verdict=verdict,
                    reason_code=reason,
                    mcu_result="ACCEPTED" if verdict == "ACCEPT" else "REJECTED",
                )
            )
        elif req.action == Action.PAUSE:
            res = await self.submit_event(FsmTrigger.PAUSE_CMD, origin=req.origin, payload=req.meta)
            decision = self._last_arbiter_decision
            verdict = "ACCEPT" if res.get("accepted") else "REJECT"
            reason = decision.reason_name if decision else None
            await self._decision_bus.publish(
                DecisionEvent(
                    request_id=req.request_id,
                    verdict=verdict,
                    reason_code=reason,
                    mcu_result="ACCEPTED" if verdict == "ACCEPT" else "REJECTED",
                )
            )
        elif req.action in (Action.START, Action.RESUME, Action.SET_SPEED, Action.RECOVER):
            payload = {
                "request_id": req.request_id,
                "confirm_ttl_ms": req.expires_in_ms,
                "setpoint_mms": req.params.get("setpoint_mms"),
                **req.meta,
            }
            decision = await self._arbiter.evaluate(
                req.action,
                req.params.get("setpoint_mms"),
                origin=req.origin,
                payload=payload,
            )
            self._last_arbiter_decision = decision
            if decision.outcome == "NEED_CONFIRMATION":
                await self._decision_bus.publish(
                    DecisionEvent(
                        request_id=req.request_id,
                        verdict="NEED_CONFIRMATION",
                        reason_code=decision.reason_name,
                    )
                )
            elif decision.permitted:
                await self._decision_bus.publish(
                    DecisionEvent(request_id=req.request_id, verdict="ACCEPT", mcu_result="ACCEPTED")
                )
            else:
                await self._decision_bus.publish(
                    DecisionEvent(
                        request_id=req.request_id,
                        verdict="REJECT",
                        reason_code=decision.reason_name,
                    )
                )

    async def submit(self, req: ActionRequest) -> None:
        """Alias for submit_action_request fulfilling FsmPort protocol."""
        await self.submit_action_request(req)

    async def confirm_action(self, request_id: str, origin: str = "voice") -> dict[str, Any]:
        """Confirm a pending action through Arbiter re-evaluation and execute if permitted."""
        decision = await self._arbiter.confirm_pending(request_id, origin=origin)
        self._last_arbiter_decision = decision

        if decision.permitted:
            act = decision.proposed_action
            s = decision.proposed_setpoint_mms
            if act == Action.START:
                await self._fsm.process(
                    FsmEvent(
                        trigger=FsmTrigger.START_CMD,
                        origin=origin,
                        payload={"setpoint_mms": s or 100},
                        timestamp=time.time(),
                    )
                )
            elif act == Action.RESUME:
                await self._fsm.process(
                    FsmEvent(
                        trigger=FsmTrigger.RESUME_CMD,
                        origin=origin,
                        payload={"setpoint_mms": s or 100},
                        timestamp=time.time(),
                    )
                )
            elif act == Action.SET_SPEED:
                if self._fsm.state == SystemState.RUNNING and s is not None:
                    await self._bridge.send_action(Action.SET_SPEED, s)
            elif act == Action.RECOVER:
                await self._fsm.process(
                    FsmEvent(
                        trigger=FsmTrigger.VERIFY_CLEARANCE,
                        origin=origin,
                        payload={},
                        timestamp=time.time(),
                    )
                )

            await self._decision_bus.publish(
                DecisionEvent(request_id=request_id, verdict="CONFIRMED", mcu_result="ACCEPTED")
            )
            return {"confirmed": True, "action": act.value, "decision": decision.model_dump()}
        else:
            verdict = "EXPIRED" if decision.reason_name == "ERR_EXPIRED" else "REJECT"
            await self._decision_bus.publish(
                DecisionEvent(request_id=request_id, verdict=verdict, reason_code=decision.reason_name)
            )
            return {"confirmed": False, "reason": decision.reason_name, "decision": decision.model_dump()}

    # ------------------------------------------------------------------
    # FsmObserver Callback & Action Dispatch
    # ------------------------------------------------------------------

    async def on_state_changed(self, event: FsmStateChanged) -> None:
        """Called automatically by FSMEngine whenever state transitions."""
        self._bridge.set_fsm_state_for_heartbeat(self._fsm_state_to_int(event.current))

        # AR-SLM-7: Transition any pending decision to CANCELLED on state change
        cancelled_ids = self._arbiter.cancel_pending("STATE_CHANGE")
        for cid in cancelled_ids:
            asyncio.create_task(
                self._decision_bus.publish(
                    DecisionEvent(request_id=cid, verdict="CANCELLED", reason_code="STATE_CHANGE")
                )
            )

        # Map state transition to intended action
        action: Action | None = None
        setpoint: int | None = None

        if event.previous == SystemState.IDLE and event.current == SystemState.RUNNING:
            action = Action.START
            setpoint = event.payload.get("setpoint_mms", 100)
        elif event.previous == SystemState.PAUSED and event.current == SystemState.RUNNING:
            action = Action.RESUME
            setpoint = event.payload.get("setpoint_mms", 100)
        elif event.current == SystemState.PAUSED:
            action = Action.PAUSE
        elif event.current == SystemState.IDLE:
            if event.previous == SystemState.RECOVERING:
                action = Action.RECOVER
            else:
                action = Action.STOP
        elif event.current == SystemState.SAFETY_FAULT:
            action = Action.HALT

        if action is None:
            return

        # Evaluate safety invariants before commanding hardware
        decision = await self._arbiter.evaluate(
            action,
            setpoint,
            origin=event.origin,
            payload=event.payload,
        )
        self._last_arbiter_decision = decision

        if decision.permitted:
            await self._bridge.send_action(action, setpoint)
            self._logger.info("Dispatched action %s to MCU (decision=PERMITTED)", action)
        else:
            self._logger.warning(
                "Arbiter INHIBITED action %s: code=%s (%s)",
                action,
                decision.reason_code,
                decision.reason_name,
            )

    @staticmethod
    def _fsm_state_to_int(state: SystemState) -> int:
        mapping = {
            SystemState.IDLE: 1,
            SystemState.RUNNING: 2,
            SystemState.PAUSED: 3,
            SystemState.JAMMED: 4,
            SystemState.SAFETY_FAULT: 5,
            SystemState.RECOVERING: 6,
        }
        return mapping.get(state, 1)

    # ------------------------------------------------------------------
    # Bridge Listeners
    # ------------------------------------------------------------------

    async def _on_bridge_telemetry(self, frame: TelemetryFrame) -> None:
        """Forward telemetry from bridge into the sensor source."""
        reading = self._bridge.latest_sensor_reading()
        await self._sensor_source.feed(reading)

    async def _on_bridge_link_lost(self) -> None:
        """Trigger FSM safety fault when MCU link is lost."""
        self._logger.critical("Bridge reported link lost! Forcing SAFETY_FAULT")
        await self._fsm.process(
            FsmEvent(
                trigger=FsmTrigger.LINK_LOST,
                origin="mcu_bridge",
                payload={},
                timestamp=time.time(),
            )
        )
