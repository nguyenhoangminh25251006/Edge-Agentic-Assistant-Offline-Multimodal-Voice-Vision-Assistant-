"""Calibration settings, mic test, and staged update/cloud action endpoints."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..commands import CommandChannelUnavailable
from ..deps import Services, get_services
from ..schemas import HmiSettings, SettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])
ServicesDep = Annotated[Services, Depends(get_services)]


class RollbackRequest(BaseModel):
    version: str


@router.get("/settings", response_model=HmiSettings)
async def get_settings(services: ServicesDep) -> HmiSettings:
    return services.state.hmi_settings


@router.put("/settings", response_model=HmiSettings)
async def update_settings(body: SettingsUpdate, services: ServicesDep) -> HmiSettings:
    changes = body.model_dump(exclude_none=True)
    merged = services.state.hmi_settings.model_copy(update={**changes, "updated_at": time.time()})
    services.state.set_settings(merged)
    try:
        await services.commands.send(
            "update_calibration",
            {
                "voice_min_confidence": merged.voice_sensitivity_pct / 100.0,
                "defect_min_confidence": merged.defect_threshold_pct / 100.0,
                "tts_enabled": merged.tts_enabled,
            },
        )
        services.state.set_connection(command_channel_ready=True)
    except CommandChannelUnavailable:
        services.state.set_connection(command_channel_ready=False)
    return merged


@router.post("/settings/test-mic")
async def test_mic(services: ServicesDep) -> dict[str, object]:
    vs = services.state.voice_status
    try:
        cmd_id = await services.commands.send("test_mic", {})
        services.state.set_connection(command_channel_ready=True)
        return {
            "ok": True,
            "command_id": cmd_id,
            "mic_active": vs.mic_active if vs else False,
            "noise_floor_dba": vs.noise_floor_dba if vs else None,
            "detail": "Microphone test requested.",
        }
    except CommandChannelUnavailable as exc:
        services.state.set_connection(command_channel_ready=False)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/updates/check")
async def check_updates(services: ServicesDep) -> dict[str, object]:
    try:
        cmd_id = await services.commands.send("check_updates", {})
        return {"ok": True, "command_id": cmd_id, "detail": "Update check requested."}
    except CommandChannelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/updates/install")
async def install_update(services: ServicesDep) -> dict[str, object]:
    try:
        cmd_id = await services.commands.send("install_update", {})
        return {"ok": True, "command_id": cmd_id, "detail": "Staged update installation proposed."}
    except CommandChannelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/updates/rollback")
async def rollback_update(body: RollbackRequest, services: ServicesDep) -> dict[str, object]:
    try:
        cmd_id = await services.commands.send("rollback_update", {"version": body.version})
        return {"ok": True, "command_id": cmd_id, "detail": f"Rollback to {body.version} proposed."}
    except CommandChannelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/cloud/sync")
async def trigger_cloud_sync(services: ServicesDep) -> dict[str, object]:
    try:
        cmd_id = await services.commands.send("cloud_sync_now", {})
        return {"ok": True, "command_id": cmd_id, "detail": "Cloud sync triggered."}
    except CommandChannelUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
