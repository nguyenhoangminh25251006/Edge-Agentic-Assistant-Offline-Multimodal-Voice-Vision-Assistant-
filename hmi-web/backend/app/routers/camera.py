"""Live camera endpoints: MJPEG stream + latest still (frames come from container-cv)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from ..deps import Services, get_services
from ..schemas import CameraStatus

router = APIRouter(prefix="/api/camera", tags=["camera"])
ServicesDep = Annotated[Services, Depends(get_services)]

_BOUNDARY = "eaaframe"


@router.get("/status", response_model=CameraStatus)
async def camera_status(services: ServicesDep) -> CameraStatus:
    return services.state.camera_status()


@router.get("/snapshot.jpg")
async def camera_snapshot(services: ServicesDep) -> Response:
    frame = services.state.frames.latest
    if frame is None:
        raise HTTPException(status_code=404, detail="No camera frame received yet")
    return Response(content=frame.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/stream.mjpg")
async def camera_stream(request: Request, services: ServicesDep) -> StreamingResponse:
    frames = services.state.frames

    async def generate() -> AsyncIterator[bytes]:
        version = -1
        while not await request.is_disconnected():
            frame = await frames.wait_newer(version, timeout=2.0)
            if frame is None:
                continue
            version = frames.version
            yield (
                f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame.jpeg)}\r\n\r\n"
            ).encode() + frame.jpeg + b"\r\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store"},
    )
