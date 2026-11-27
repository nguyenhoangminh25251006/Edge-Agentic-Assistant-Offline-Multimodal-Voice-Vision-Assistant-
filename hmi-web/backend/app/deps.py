"""Shared service container and FastAPI dependency accessors."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request, WebSocket

from .commands import CommandPublisher
from .config import Settings
from .hub import WebSocketHub
from .ingest import OrchestratorIngest
from .logs import LogService
from .state import HmiState


@dataclass(slots=True)
class Services:
    settings: Settings
    hub: WebSocketHub
    state: HmiState
    ingest: OrchestratorIngest
    commands: CommandPublisher
    logs: LogService


def get_services(request: Request) -> Services:
    return request.app.state.services


def get_ws_services(websocket: WebSocket) -> Services:
    return websocket.app.state.services
