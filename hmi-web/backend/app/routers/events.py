"""Event Logs API: paginated query, detail, evidence snapshot and CSV export."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from ..deps import Services, get_services
from ..logs import LogQuery
from ..schemas import LogEntry, LogPage, Severity

router = APIRouter(prefix="/api/logs", tags=["logs"])
ServicesDep = Annotated[Services, Depends(get_services)]


def _query(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    severity: list[Severity] = Query(default=[]),
    source: str | None = Query(None, max_length=32),
    search: str | None = Query(None, max_length=128),
    since: float | None = Query(None, ge=0),
    until: float | None = Query(None, ge=0),
) -> LogQuery:
    return LogQuery(
        page=page,
        page_size=page_size,
        severities=tuple(severity),
        source=source.lower() if source else None,
        search=search.strip() if search and search.strip() else None,
        since=since,
        until=until,
    )


QueryDep = Annotated[LogQuery, Depends(_query)]


@router.get("", response_model=LogPage)
async def list_logs(query: QueryDep, services: ServicesDep) -> LogPage:
    return await services.logs.page(query)


@router.get("/export.csv")
async def export_logs(query: QueryDep, services: ServicesDep) -> StreamingResponse:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    filename = f"eaa-{services.state.station.id}-events-{stamp}.csv"
    return StreamingResponse(
        services.logs.export_csv(query),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{event_id}", response_model=LogEntry)
async def get_log(event_id: str, services: ServicesDep) -> LogEntry:
    entry = await services.logs.get(event_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return entry


@router.get("/{event_id}/snapshot.jpg")
async def get_snapshot(event_id: str, services: ServicesDep) -> Response:
    jpeg = services.state.snapshots.get(event_id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="No evidence snapshot for this event")
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
