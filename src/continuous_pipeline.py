"""continuous_pipeline.py — Continuous pipeline linking event_pipeline and control_pipeline.

Architecture:
  [ Ingress (Router) ] ---> [ ContinuousPipelineSink ] ---> [ FSM + Safety Arbiter ]
                                                                   |
                                                                   v
  [ TelemetryRingBuffer ] <--- [ 20Hz Telemetry ] <--- [ MCU Bridge / Hardware ]
          |
          v
  [ SQLite WAL Database ] <--- [ State Transitions & Safety Audit ]
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
_EVENT_PIPELINE = _SRC_DIR / "event_pipeline"
_CONTROL_PIPELINE = _SRC_DIR / "control_pipeline"

for _p in (str(_SRC_DIR), str(_EVENT_PIPELINE), str(_CONTROL_PIPELINE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asyncio
import logging
import time
import uuid
from typing import Any

from control_pipeline.config import OrchestratorSettings
from control_pipeline.contracts import FsmObserver
from control_pipeline.pipeline import OrchestratorPipeline
from control_pipeline.schemas.bridge import TelemetryFrame
from control_pipeline.schemas.fsm import FsmStateChanged, FsmTrigger, SystemState

from db_client.database import Database
from db_client.repository import HalfSyncHalfAsyncPersistenceWorker, TelemetryRepository
from event_router.event_router import EventRouter
from mock_data.constants.zmq_topics import TOPIC_SENSOR_SNAPSHOT, TOPIC_UI_COMMAND
from mock_data.contracts import SubmitOutcome
from mock_data.schemas.events import (
    CVFramePayload,
    EventEnvelope,
    EventSource,
    EventType,
    IntentPayload,
    SensorSnapshotPayload,
    UICommandPayload,
)
from telemetry_ring.ring_buffer import TelemetryRingBuffer
from slm_pipeline.config import SlmConfig
from slm_pipeline.contracts import UtteranceUnmatched
from slm_pipeline.service import SlmService

LOGGER = logging.getLogger("eaa.continuous_pipeline")

# Mapping from incoming string commands to FsmTrigger
_ACTION_TO_TRIGGER: dict[str, FsmTrigger] = {
    "start": FsmTrigger.START_CMD,
    "start_cmd": FsmTrigger.START_CMD,
    "stop": FsmTrigger.STOP_CMD,
    "stop_cmd": FsmTrigger.STOP_CMD,
    "pause": FsmTrigger.PAUSE_CMD,
    "pause_cmd": FsmTrigger.PAUSE_CMD,
    "resume": FsmTrigger.RESUME_CMD,
    "resume_cmd": FsmTrigger.RESUME_CMD,
    "set_speed": FsmTrigger.SET_SPEED_CMD,
    "set_speed_cmd": FsmTrigger.SET_SPEED_CMD,
    "verify_clearance": FsmTrigger.VERIFY_CLEARANCE,
    "clear_jam": FsmTrigger.VERIFY_CLEARANCE,
    "fault_cleared": FsmTrigger.FAULT_CLEARED,
    "clear_fault": FsmTrigger.FAULT_CLEARED,
    "safety_event": FsmTrigger.SAFETY_EVENT,
    "estop": FsmTrigger.SAFETY_EVENT,
}


class PipelineSpeaker:
    """Speaker interface delegating to application logging or TTS."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self.spoken: list[tuple[str, dict[str, object]]] = []

    async def say(self, key: str, **params: object) -> None:
        self.spoken.append((key, params))
        self._logger.info("SPEAKER | %s %s", key, params if params else "")


class PipelineSlmSink:
    """EventSink publishing SLM results and status to logging and observers."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self.published: list[Any] = []

    async def publish(self, payload: Any) -> None:
        self.published.append(payload)
        self._logger.info("SLM_SINK | %s", getattr(payload, "event_type", type(payload).__name__))


class ContinuousPipeline(FsmObserver):
    """Integrates event_pipeline and control_pipeline into a single, continuous flow."""

    def __init__(
        self,
        settings: OrchestratorSettings | None = None,
        db_path: str = ":memory:",
        ring_capacity: int = 1000,
        slm_config: SlmConfig | None = None,
        slm_client: Any | None = None,
    ) -> None:
        self._settings = settings or OrchestratorSettings()
        self._db = Database(db_path)
        self._repo: TelemetryRepository | None = None
        self._persistence_worker: HalfSyncHalfAsyncPersistenceWorker | None = None
        self._ring_buffer: TelemetryRingBuffer | None = None
        self._router: EventRouter | None = None

        # Control pipeline instance
        self._control = OrchestratorPipeline(self._settings)

        # SLM pipeline service
        self._slm_config = slm_config or SlmConfig.default()
        self._slm_client = slm_client
        self._slm_speaker = PipelineSpeaker(LOGGER.getChild("speaker"))
        self._slm_sink = PipelineSlmSink(LOGGER.getChild("slm_sink"))
        self._slm_service: SlmService | None = None

        self._running = False
        self._event_seq = 0

    @property
    def slm(self) -> SlmService | None:
        return self._slm_service

    @property
    def slm_speaker(self) -> PipelineSpeaker:
        return self._slm_speaker

    @property
    def control(self) -> OrchestratorPipeline:
        return self._control

    @property
    def ring_buffer(self) -> TelemetryRingBuffer | None:
        return self._ring_buffer

    @property
    def router(self) -> EventRouter | None:
        return self._router

    @property
    def persistence_worker(self) -> HalfSyncHalfAsyncPersistenceWorker | None:
        return self._persistence_worker

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start both pipelines and establish bidirectional wiring."""
        if self._running:
            return
        self._running = True

        # 1. Initialize SQLite persistence
        await self._db.connect()
        self._repo = TelemetryRepository(self._db)
        self._persistence_worker = HalfSyncHalfAsyncPersistenceWorker(self._repo)
        await self._persistence_worker.start()

        # 2. Initialize Telemetry Ring Buffer (backed by persistence)
        self._ring_buffer = TelemetryRingBuffer(
            capacity=1000,
            persistence=self._persistence_worker,
        )

        # 3. Wire control pipeline telemetry output directly into the ring buffer (chained)
        orig_listener = self._control.bridge._telemetry_listener
        async def _combined_telemetry(frame: TelemetryFrame) -> None:
            if orig_listener:
                await orig_listener(frame)
            await self._on_control_telemetry(frame)

        self._control.bridge.set_telemetry_listener(_combined_telemetry)

        # 4. Wire FSM state changes to our FsmObserver callback for audit logging
        self._control.fsm.add_observer(self)

        # 5. Initialize EventRouter pointing directly to this pipeline's push()
        self._router = EventRouter(self)

        # 6. Start control pipeline (FSM, Arbiter, MCU Bridge & MockMcu)
        await self._control.start()

        # 7. Start SLM pipeline service
        self._slm_service = SlmService(
            cfg=self._slm_config,
            fsm=self._control,
            bus=self._control.decision_bus,
            speaker=self._slm_speaker,
            sink=self._slm_sink,
            client=self._slm_client,
        )
        await self._slm_service.initialize()

        LOGGER.info("Continuous pipeline started (FSM=%s)", self._control.fsm_state.value)

    async def stop(self) -> None:
        """Gracefully stop both pipelines in reverse order."""
        if not self._running:
            return
        self._running = False

        if self._slm_service:
            await self._slm_service.stop()

        if self._router:
            await self._router.stop()
        await self._control.stop()

        if self._persistence_worker:
            await self._persistence_worker.stop()
        await self._db.close()

        LOGGER.info("Continuous pipeline stopped cleanly")

    # ------------------------------------------------------------------
    # Inbound: EventRouter Sink Protocol Implementation
    # ------------------------------------------------------------------

    async def push(self, envelope: EventEnvelope | UtteranceUnmatched | Any) -> None:
        """Accept canonical EventEnvelope from EventRouter or UtteranceUnmatched and steer to control pipeline."""
        if isinstance(envelope, UtteranceUnmatched):
            if self._slm_service:
                await self._slm_service.on_unmatched(envelope)
            return

        payload = envelope.payload
        source = envelope.source.value if hasattr(envelope.source, "value") else str(envelope.source)
        event_type = envelope.event_type.value if hasattr(envelope.event_type, "value") else str(envelope.event_type)

        # SLM Ingress: stt.unmatched
        if event_type == "stt.unmatched" or isinstance(payload, UtteranceUnmatched):
            if isinstance(payload, UtteranceUnmatched):
                unmatched_ev = payload
            elif isinstance(payload, dict):
                unmatched_ev = UtteranceUnmatched.model_validate(payload)
            else:
                unmatched_ev = UtteranceUnmatched(
                    event_id=envelope.event_id,
                    text=str(getattr(payload, "text", "")),
                    ingested_mono_ns=int(getattr(payload, "ingested_mono_ns", time.monotonic_ns())),
                    wake_confirmed=bool(getattr(payload, "wake_confirmed", True)),
                    asr_confidence=getattr(payload, "asr_confidence", None),
                )
            if self._slm_service:
                await self._slm_service.on_unmatched(unmatched_ev)
            # Rule 8.2: NEVER persist or add stt.unmatched to ring buffer!
            return

        if isinstance(payload, UICommandPayload):
            action_name = payload.action.lower()
            if action_name == "confirm":
                req_id = str(payload.parameters.get("request_id", ""))
                await self._control.confirm_action(req_id, origin="ui")
            else:
                if self._slm_service:
                    self._slm_service.on_matched_command()
                trigger = _ACTION_TO_TRIGGER.get(action_name)
                if trigger:
                    await self._control.submit_event(
                        trigger=trigger,
                        origin=source,
                        payload=dict(payload.parameters),
                    )
                else:
                    LOGGER.warning("Unknown UI action received: %s", payload.action)

        elif isinstance(payload, IntentPayload):
            intent_name = payload.intent.lower()
            if intent_name == "confirm":
                req_id = ""
                parts = payload.transcript.strip().split()
                if len(parts) > 1:
                    req_id = parts[-1]
                await self._control.confirm_action(req_id, origin="voice")
            else:
                if self._slm_service:
                    self._slm_service.on_matched_command()
                trigger = _ACTION_TO_TRIGGER.get(intent_name)
                if trigger:
                    await self._control.submit_event(
                        trigger=trigger,
                        origin="stt",
                        payload={"confidence": payload.confidence, "transcript": payload.transcript},
                    )

        elif isinstance(payload, CVFramePayload):
            if payload.jam_suspected:
                LOGGER.warning("CV detected jam! Triggering JAM_DETECTED")
                await self._control.submit_event(
                    trigger=FsmTrigger.JAM_DETECTED,
                    origin="cv",
                    payload={"confidence": payload.confidence, "camera_id": payload.camera_id},
                )

        elif isinstance(payload, SensorSnapshotPayload):
            # Feed physical sensors from outside directly into the Arbiter's sensor source
            from control_pipeline.schemas.arbiter import SensorReading

            reading = SensorReading(
                estop_closed=payload.estop_circuit_closed,
                pe_clear=payload.optical_curtain_clear,
                hb_valid=True,
                fault_latched=False,
                motor_enabled=True,
                sensors_ok=True,
                motor_current_ma=payload.motor_current_amp * 1000.0,
                conveyor_speed_mms=payload.conveyor_speed_mms,
                infrared_temp_celsius=payload.infrared_temp_celsius,
                sensor_timestamp_ms=payload.sensor_timestamp_ms,
                link_state="UP",
                last_frame_age_ms=0.0,
            )
            await self._control.feed_sensor(reading)

        # Store inbound envelope in the ring buffer for real-time HMI WebSocket queries
        if self._ring_buffer:
            await self._ring_buffer.push(envelope)

    # ------------------------------------------------------------------
    # Outbound: Bridge Telemetry -> Ring Buffer & Persistence
    # ------------------------------------------------------------------

    async def _on_control_telemetry(self, frame: TelemetryFrame) -> None:
        """Convert B-03 TelemetryFrame (20Hz) into canonical EventEnvelope for ring buffer."""
        if not self._ring_buffer:
            return

        self._event_seq += 1
        now = time.time()
        flags = frame.flags

        snapshot = SensorSnapshotPayload(
            motor_current_amp=float(frame.current_ma) / 1000.0,
            infrared_temp_celsius=float(frame.temp_c10) / 10.0,
            conveyor_speed_mms=float(frame.speed_mms),
            optical_curtain_clear=bool(flags & 0x02),
            estop_circuit_closed=bool(flags & 0x01),
            sensor_timestamp_ms=frame.tick_ms,
        )

        envelope = EventEnvelope(
            schema_version=1,
            event_id=uuid.uuid4().hex,
            event_type=EventType.SENSOR_SNAPSHOT,
            source=EventSource.SENSOR,
            topic=TOPIC_SENSOR_SNAPSHOT,
            sequence=self._event_seq,
            event_time=now,
            ingested_at=now,
            payload=snapshot,
        )

        await self._ring_buffer.push(envelope)

    # ------------------------------------------------------------------
    # Outbound: FSM Observer -> SQLite Audit Log
    # ------------------------------------------------------------------

    async def on_state_changed(self, change: FsmStateChanged) -> None:
        """Log state transitions into the persistent SQLite database."""
        if not self._persistence_worker:
            return

        self._event_seq += 1
        now = time.time()

        audit_payload = UICommandPayload(
            action=f"transition:{change.previous.value}->{change.current.value}",
            parameters={
                "trigger": change.trigger.value,
                "origin": change.origin,
                "previous_state": change.previous.value,
                "current_state": change.current.value,
            },
        )

        envelope = EventEnvelope(
            schema_version=1,
            event_id=uuid.uuid4().hex,
            event_type=EventType.UI_COMMAND,
            source=EventSource.SYSTEM,
            topic=TOPIC_UI_COMMAND,
            sequence=self._event_seq,
            event_time=now,
            ingested_at=now,
            payload=audit_payload,
        )

        await self._persistence_worker.submit(envelope)
