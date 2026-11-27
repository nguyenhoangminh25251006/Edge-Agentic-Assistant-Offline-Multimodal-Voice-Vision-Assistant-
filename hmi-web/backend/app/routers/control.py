"""Operator command endpoint (proposals only; the Safety Arbiter/MCU decide)."""

from __future__ import annotations

import time
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from ..commands import CommandChannelUnavailable
from ..deps import Services, get_services
from ..schemas import CommandRequest, CommandResponse, HmiEvent, Severity

router = APIRouter(prefix="/api", tags=["control"])
ServicesDep = Annotated[Services, Depends(get_services)]

# Energising commands need explicit two-step operator confirmation (FR-06).
_REQUIRES_CONFIRMATION = {"start", "resume"}


@router.post("/commands", response_model=CommandResponse)
async def submit_command(body: CommandRequest, services: ServicesDep) -> CommandResponse:
    cfg = services.settings
    parameters: dict[str, int | float | str | bool] = {}

    if body.action in _REQUIRES_CONFIRMATION and not body.confirmed:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail=f"'{body.action}' requires operator confirmation (FR-06).",
        )

    if body.action == "set_speed" or (body.action in {"start", "resume"} and body.setpoint_mms):
        setpoint = body.setpoint_mms
        if setpoint is None:
            raise HTTPException(status_code=422, detail="setpoint_mms is required for set_speed.")
        if not cfg.speed_min_mms <= setpoint <= cfg.speed_max_mms:
            raise HTTPException(
                status_code=422,
                detail=f"setpoint_mms must be within {cfg.speed_min_mms}–{cfg.speed_max_mms} mm/s.",
            )
        parameters["setpoint_mms"] = setpoint

    if body.confirmed:
        parameters["operator_confirmed"] = True

    try:
        command_id = await services.commands.send(body.action, parameters)
    except CommandChannelUnavailable as exc:
        services.state.set_connection(command_channel_ready=False)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc

    services.state.set_connection(command_channel_ready=True)
    label = body.action.replace("_", " ").upper()
    detail = f"{label} proposed to orchestrator" + (
        f" (setpoint {parameters['setpoint_mms']} mm/s)" if "setpoint_mms" in parameters else ""
    )
    services.state.add_event(
        HmiEvent(
            event_id=f"hmi-{uuid.uuid4().hex}",
            ts=time.time(),
            severity=Severity.INFO,
            source="operator",
            description=f"Operator command: {detail}",
            payload={"command_id": command_id, "action": body.action, **parameters},
        )
    )
    return CommandResponse(accepted=True, command_id=command_id, action=body.action, detail=detail)
