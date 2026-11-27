"""System info, health, live state snapshot and the multiplexed WebSocket stream."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket

from .. import __version__
from ..deps import Services, get_services, get_ws_services
from ..schemas import (
    CalibrationBounds,
    CloudStatus,
    DiagnosticsDefaults,
    SensorItem,
    SpeedLimits,
    StateSnapshot,
    StationInfo,
    SystemInfo,
    TelemetryThresholds,
    UpdateStatus,
    WsMessage,
)

router = APIRouter(tags=["system"])
ServicesDep = Annotated[Services, Depends(get_services)]


def _refresh_connection(services: Services) -> None:
    services.state.set_connection(database_available=services.logs.available)


@router.get("/api/health")
async def health(services: ServicesDep) -> dict[str, object]:
    conn = services.state.connection
    return {
        "status": "ok",
        "version": __version__,
        "orchestrator_connected": conn.orchestrator_connected,
        "last_message_at": conn.last_message_at,
        "database_available": services.logs.available,
        "websocket_clients": services.hub.client_count,
        "ingest": services.ingest.stats,
        "server_time": time.time(),
    }


@router.get("/api/system", response_model=SystemInfo)
async def system_info(services: ServicesDep) -> SystemInfo:
    cfg = services.settings
    raw = cfg.master_config
    station = services.state.station
    stations = [StationInfo(id=sid, name=name, host=host) for sid, name, host in cfg.stations]
    sensors = [SensorItem(id=sid, label=label) for sid, label in cfg.sensors]
    cloud_cfg = raw.get("cloud") if isinstance(raw.get("cloud"), dict) else {}
    updates_cfg = raw.get("updates") if isinstance(raw.get("updates"), dict) else {}
    return SystemInfo(
        station=station,
        stations=stations or [station],
        sensors=sensors,
        operator=cfg.operator_name,
        speed_limits=SpeedLimits(
            min_mms=cfg.speed_min_mms, max_mms=cfg.speed_max_mms, step_mms=cfg.speed_step_mms
        ),
        thresholds=TelemetryThresholds.model_validate(raw.get("thresholds") or {}),
        calibration_bounds=CalibrationBounds.model_validate(raw.get("calibration_bounds") or {}),
        diagnostics_defaults=DiagnosticsDefaults.model_validate(raw.get("diagnostics_defaults") or {}),
        cloud=CloudStatus.model_validate(cloud_cfg),
        updates=UpdateStatus(
            current_version=cfg.app_version,
            build=cfg.app_build,
            **{k: v for k, v in updates_cfg.items() if k not in {"current_version", "build"}},
        ),
    )


@router.get("/api/state", response_model=StateSnapshot)
async def state_snapshot(services: ServicesDep) -> StateSnapshot:
    _refresh_connection(services)
    return services.state.snapshot()


@router.websocket("/ws")
async def stream(websocket: WebSocket) -> None:
    services = get_ws_services(websocket)
    _refresh_connection(services)
    initial = WsMessage(type="snapshot", data=services.state.snapshot().model_dump(mode="json"))
    await services.hub.serve(websocket, initial)
