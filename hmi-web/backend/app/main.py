"""FastAPI application factory for the EAA HMI gateway (container-web)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import zmq.asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .commands import CommandPublisher
from .config import Settings
from .deps import Services
from .hub import WebSocketHub
from .ingest import OrchestratorIngest
from .logs import LogService
from .routers import camera, control, events, settings, system
from .schemas import HmiSettings
from .state import HmiState

LOGGER = logging.getLogger("eaa.hmi")


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        context = zmq.asyncio.Context()
        hub = WebSocketHub(cfg.ws_queue_size)
        state = HmiState(cfg, hub)
        cal_cfg = cfg.master_config.get("calibration") if isinstance(cfg.master_config.get("calibration"), dict) else {}
        state.hmi_settings = HmiSettings.model_validate(cal_cfg)
        services = Services(
            settings=cfg,
            hub=hub,
            state=state,
            ingest=OrchestratorIngest(cfg, state, context),
            commands=CommandPublisher(cfg, context),
            logs=LogService(cfg.db_path, state),
        )
        services.commands.start()
        await services.ingest.start()
        state.set_connection(database_available=services.logs.available)
        app.state.services = services
        LOGGER.info(
            "HMI gateway ready (station=%s, db=%s, sub=%s, push=%s)",
            cfg.station_id,
            cfg.db_path or "memory-only",
            cfg.hmi_sub_endpoint,
            cfg.ui_push_endpoint,
        )
        try:
            yield
        finally:
            await services.ingest.stop()
            services.commands.stop()
            await services.logs.close()
            await hub.close()
            context.term()

    app = FastAPI(
        title="EAA HMI Gateway",
        version=__version__,
        description="Offline local Web HMI gateway for the Edge Agentic Assistant (container-web).",
        lifespan=lifespan,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_methods=["GET", "POST", "PUT"],
            allow_headers=["*"],
        )

    for module in (system, control, events, camera, settings):
        app.include_router(module.router)

    _mount_frontend(app, cfg.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built React SPA (``frontend/dist``) with client-side routing fallback."""
    index = dist / "index.html"
    if not index.is_file():
        LOGGER.warning("Frontend build not found at %s; serving API only", dist)
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path.startswith(("api/", "ws")):
            raise HTTPException(status_code=404)
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and dist.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
