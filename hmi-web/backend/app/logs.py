"""Event log access for the ``Event Logs`` screen.

Primary backend: the orchestrator's append-only SQLite WAL database
(``telemetry_events`` table, schema v2), opened strictly **read-only**.
Fallback backend: the in-memory ring of recent ``hmi.event`` messages, used when
``EAA_DB_PATH`` is not configured or the database file does not exist yet.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .schemas import HmiEvent, LogEntry, LogPage, Severity
from .state import HmiState

LOGGER = logging.getLogger("eaa.hmi.logs")

_SEVERITY_SQL = """
CASE
  WHEN json_extract(payload_json, '$.severity') IS NOT NULL
    THEN CASE upper(json_extract(payload_json, '$.severity'))
           WHEN 'WARNING' THEN 'WARN'
           WHEN 'ERROR' THEN 'CRITICAL'
           WHEN 'CRITICAL' THEN 'CRITICAL'
           WHEN 'WARN' THEN 'WARN'
           ELSE 'INFO'
         END
  WHEN event_type = 'sensor.snapshot'
       AND json_extract(payload_json, '$.estop_circuit_closed') = 0 THEN 'CRITICAL'
  WHEN event_type = 'sensor.snapshot'
       AND json_extract(payload_json, '$.optical_curtain_clear') = 0 THEN 'WARN'
  WHEN event_type = 'cv.frame'
       AND json_extract(payload_json, '$.jam_suspected') = 1 THEN 'WARN'
  WHEN event_type = 'ui.command'
       AND json_extract(payload_json, '$.parameters.current_state') = 'safety_fault' THEN 'CRITICAL'
  WHEN event_type = 'ui.command'
       AND json_extract(payload_json, '$.parameters.current_state') = 'jammed' THEN 'WARN'
  ELSE 'INFO'
END
"""

# UI source filter -> SQL predicate on the canonical event_type/source columns.
_SOURCE_SQL: dict[str, str] = {
    "camera": "event_type = 'cv.frame'",
    "voice": "event_type = 'stt.intent'",
    "sensor": "event_type = 'sensor.snapshot'",
    "system": "(event_type = 'ui.command' AND source = 'system')",
    "operator": "(event_type = 'ui.command' AND source = 'ui')",
}

_SOURCE_LABEL: dict[str, str] = {
    "cv.frame": "Camera",
    "stt.intent": "Voice",
    "sensor.snapshot": "Sensor",
}


@dataclass(frozen=True, slots=True)
class LogQuery:
    page: int = 1
    page_size: int = 50
    severities: tuple[Severity, ...] = ()
    source: str | None = None
    search: str | None = None
    since: float | None = None
    until: float | None = None


def _fmt_state(value: Any) -> str:
    return str(value).upper() if value else "?"


def _describe(event_type: str, source: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Return ``(source_label, description)`` for a persisted envelope payload."""
    if isinstance(payload.get("description"), str):
        return str(payload.get("source") or source).title(), payload["description"]

    if event_type == "ui.command":
        action = str(payload.get("action", ""))
        params = payload.get("parameters") or {}
        if action.startswith("transition:"):
            trigger = params.get("trigger", "?")
            origin = params.get("origin", "?")
            return "System", (
                f"State transition {_fmt_state(params.get('previous_state'))} → "
                f"{_fmt_state(params.get('current_state'))} (trigger {trigger}, origin {origin})"
            )
        detail = ", ".join(
            f"{k}={v}"
            for k, v in params.items()
            if k not in {"command_id", "origin", "station_id", "operator", "issued_at"}
        )
        label = "Operator" if source == "ui" else source.title()
        return label, f"Operator command: {action}" + (f" ({detail})" if detail else "")

    if event_type == "stt.intent":
        conf = payload.get("confidence")
        conf_txt = f" — {float(conf) * 100:.0f}%" if isinstance(conf, int | float) else ""
        return "Voice", f'Voice intent {payload.get("intent", "?")}: "{payload.get("transcript", "")}"{conf_txt}'

    if event_type == "cv.frame":
        objects = payload.get("detected_objects") or []
        conf = payload.get("confidence")
        conf_txt = f" ({float(conf) * 100:.1f}%)" if isinstance(conf, int | float) else ""
        if payload.get("jam_suspected"):
            return "Camera", f"Jam suspected on {payload.get('camera_id', 'cam0')}{conf_txt}"
        listed = ", ".join(map(str, objects)) if objects else "no objects"
        return "Camera", f"Inspection frame: {listed}{conf_txt}"

    if event_type == "sensor.snapshot":
        parts: list[str] = []
        if payload.get("estop_circuit_closed") is False:
            parts.append("Hardware E-stop circuit OPEN")
        if payload.get("optical_curtain_clear") is False:
            parts.append("Photoelectric beam blocked")
        speed = payload.get("conveyor_speed_mms")
        current = payload.get("motor_current_amp")
        temp = payload.get("infrared_temp_celsius")
        parts.append(
            "speed="
            + (f"{speed:.0f} mm/s" if isinstance(speed, int | float) else "--")
            + ", current="
            + (f"{current:.2f} A" if isinstance(current, int | float) else "--")
            + ", temp="
            + (f"{temp:.1f} °C" if isinstance(temp, int | float) else "--")
        )
        return "Sensor", "Sensor snapshot: " + "; ".join(parts)

    return _SOURCE_LABEL.get(event_type, source.title()), f"{event_type} event"


def _telemetry_from(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(payload.get("telemetry"), dict):
        return payload["telemetry"]
    if event_type == "sensor.snapshot":
        return {
            "belt_speed_mms": payload.get("conveyor_speed_mms"),
            "motor_current_a": payload.get("motor_current_amp"),
            "motor_temp_c": payload.get("infrared_temp_celsius"),
            "pe_clear": payload.get("optical_curtain_clear"),
            "estop_closed": payload.get("estop_circuit_closed"),
        }
    return None


class LogService:
    def __init__(self, db_path: Path | None, state: HmiState) -> None:
        self._db_path = db_path
        self._state = state
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._db_path is not None and self._db_path.exists()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _connection(self) -> aiosqlite.Connection | None:
        if not self.available:
            return None
        if self._conn is None:
            uri = f"file:{self._db_path.as_posix()}?mode=ro"  # type: ignore[union-attr]
            self._conn = await aiosqlite.connect(uri, uri=True)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA busy_timeout=2000")
            await self._conn.execute("PRAGMA query_only=ON")
        return self._conn

    # ------------------------------------------------------------------
    # SQLite backend
    # ------------------------------------------------------------------

    def _where(self, query: LogQuery) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.since is not None:
            clauses.append("event_time >= ?")
            params.append(query.since)
        if query.until is not None:
            clauses.append("event_time <= ?")
            params.append(query.until)
        if query.source and query.source in _SOURCE_SQL:
            clauses.append(_SOURCE_SQL[query.source])
        if query.search:
            clauses.append("(payload_json LIKE ? OR event_type LIKE ? OR event_id = ?)")
            like = f"%{query.search}%"
            params.extend([like, like, query.search])
        if query.severities:
            clauses.append(f"severity IN ({','.join('?' for _ in query.severities)})")
            params.extend(s.value for s in query.severities)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _base_sql(self) -> str:
        return (
            "SELECT event_id, event_type, source, event_time, payload_json, "
            f"{_SEVERITY_SQL} AS severity FROM telemetry_events"
        )

    def _row_to_entry(self, row: aiosqlite.Row) -> LogEntry:
        try:
            payload = json.loads(row["payload_json"])
        except ValueError:
            payload = {}
        label, description = _describe(row["event_type"], row["source"], payload)
        event_id = row["event_id"]
        return LogEntry(
            id=event_id,
            ts=row["event_time"],
            severity=Severity(row["severity"]),
            source=label,
            description=description,
            event_type=row["event_type"],
            station_id=payload.get("station_id") or self._state.station.id,
            pcb_id=payload.get("pcb_id"),
            telemetry=_telemetry_from(row["event_type"], payload),
            payload=payload,
            has_snapshot=event_id in self._state.snapshots,
        )

    async def _sqlite_page(self, conn: aiosqlite.Connection, query: LogQuery) -> LogPage:
        where, params = self._where(query)
        inner = f"SELECT * FROM ({self._base_sql()}){where}"
        async with self._lock:
            cur = await conn.execute(f"SELECT COUNT(*) FROM ({inner})", params)
            total_row = await cur.fetchone()
            await cur.close()
            offset = (query.page - 1) * query.page_size
            cur = await conn.execute(
                f"{inner} ORDER BY event_time DESC LIMIT ? OFFSET ?",
                [*params, query.page_size, offset],
            )
            rows = await cur.fetchall()
            await cur.close()
        return LogPage(
            items=[self._row_to_entry(r) for r in rows],
            total=int(total_row[0]) if total_row else 0,
            page=query.page,
            page_size=query.page_size,
            backend="sqlite",
        )

    # ------------------------------------------------------------------
    # Memory backend
    # ------------------------------------------------------------------

    @staticmethod
    def _event_to_entry(event: HmiEvent, snapshots: Any) -> LogEntry:
        return LogEntry(
            id=event.event_id,
            ts=event.ts,
            severity=event.severity,
            source=event.source.title(),
            description=event.description,
            event_type="hmi.event",
            station_id=event.station_id,
            pcb_id=event.pcb_id,
            telemetry=event.telemetry,
            payload={**(event.payload or {}), "reason_code": event.reason_code}
            if event.reason_code
            else event.payload,
            has_snapshot=event.event_id in snapshots,
        )

    def _memory_filter(self, query: LogQuery) -> list[HmiEvent]:
        needle = (query.search or "").lower()
        result: list[HmiEvent] = []
        for event in reversed(self._state.events):
            if query.severities and event.severity not in query.severities:
                continue
            if query.source and event.source.lower() != query.source:
                continue
            if query.since is not None and event.ts < query.since:
                continue
            if query.until is not None and event.ts > query.until:
                continue
            if needle and not (
                needle in event.description.lower()
                or needle in (event.pcb_id or "").lower()
                or needle == event.event_id.lower()
            ):
                continue
            result.append(event)
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def page(self, query: LogQuery) -> LogPage:
        try:
            conn = await self._connection()
            if conn is not None:
                return await self._sqlite_page(conn, query)
        except Exception:
            LOGGER.exception("SQLite log query failed; falling back to memory")
            await self.close()
        events = self._memory_filter(query)
        start = (query.page - 1) * query.page_size
        chunk = events[start : start + query.page_size]
        return LogPage(
            items=[self._event_to_entry(e, self._state.snapshots) for e in chunk],
            total=len(events),
            page=query.page,
            page_size=query.page_size,
            backend="memory",
        )

    async def get(self, event_id: str) -> LogEntry | None:
        for event in reversed(self._state.events):
            if event.event_id == event_id:
                return self._event_to_entry(event, self._state.snapshots)
        try:
            conn = await self._connection()
            if conn is None:
                return None
            async with self._lock:
                cur = await conn.execute(
                    f"SELECT * FROM ({self._base_sql()}) WHERE event_id = ?", [event_id]
                )
                row = await cur.fetchone()
                await cur.close()
            return self._row_to_entry(row) if row else None
        except Exception:
            LOGGER.exception("SQLite lookup failed")
            await self.close()
            return None

    async def export_csv(self, query: LogQuery, max_rows: int = 50_000) -> AsyncIterator[str]:
        header = ["timestamp_unix", "severity", "source", "description", "event_type", "station_id", "pcb_id", "event_id"]
        yield _csv_line(header)
        page_size = 1000
        sent = 0
        page_no = 1
        while sent < max_rows:
            page = await self.page(
                LogQuery(
                    page=page_no,
                    page_size=page_size,
                    severities=query.severities,
                    source=query.source,
                    search=query.search,
                    since=query.since,
                    until=query.until,
                )
            )
            for item in page.items:
                yield _csv_line(
                    [
                        f"{item.ts:.3f}",
                        item.severity.value,
                        item.source,
                        item.description,
                        item.event_type or "",
                        item.station_id or "",
                        item.pcb_id or "",
                        item.id,
                    ]
                )
                sent += 1
                if sent >= max_rows:
                    return
            if page_no * page_size >= page.total or not page.items:
                return
            page_no += 1


def _csv_line(values: Iterable[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerow(values)
    return buf.getvalue()
