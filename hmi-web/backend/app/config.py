"""Runtime settings for the HMI gateway, resolved from environment variables.

Endpoint defaults mirror the orchestrator conventions:

* ``EAA_IPC_DIR`` (default ``<tmp>/eaa``) holds the ZeroMQ IPC sockets on Linux.
* The orchestrator ``EventRouter`` **binds** a PULL socket at ``ui.pull``; the HMI
  PUSH-**connects** to it to submit operator commands (``UICommandWire``).
* The orchestrator **binds** a PUB socket at ``hmi.pub``; the HMI SUB-**connects**
  to it to receive live telemetry, state changes, events, frames, etc.

On Windows (development hosts) IPC transport is not used; TCP loopback defaults apply.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_HMI_WEB_DIR = _BACKEND_DIR.parent
_DEFAULT_CONFIG_PATH = _HMI_WEB_DIR / "config.json"


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value).expanduser() if value not in (None, "") else None


def default_ipc_dir() -> Path:
    raw = os.getenv("EAA_IPC_DIR")
    return Path(raw).expanduser() if raw else Path(tempfile.gettempdir()) / "eaa"


def _endpoint(env_name: str, ipc_name: str, tcp_default: str) -> str:
    explicit = os.getenv(env_name)
    if explicit:
        return explicit
    if os.name == "nt":
        return tcp_default
    return f"ipc://{default_ipc_dir() / ipc_name}"


_DEFAULT_SENSORS = (
    ("motor_driver", "Motor Driver"),
    ("rotary_encoder", "Rotary Encoder"),
    ("photoelectric", "Photoelectric Sensor"),
    ("current_sensor", "Current Sensor"),
    ("temperature_sensor", "Temperature Sensor"),
)


def _parse_sensors(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse ``id:Label,id2:Label 2`` (or bare ``Label``) from ``EAA_SENSORS``."""
    if raw is None:
        return _DEFAULT_SENSORS
    items: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        part = chunk.strip()
        if not part:
            continue
        if ":" in part:
            sid, label = part.split(":", 1)
            items.append((sid.strip(), label.strip()))
        else:
            sid = part.lower().replace(" ", "_")
            items.append((sid, part))
    return tuple(items)


def _parse_stations(
    raw: str | None, default_id: str, default_name: str, default_host: str
) -> tuple[tuple[str, str, str], ...]:
    """Parse ``ID:Name:Host,ID2:Name2:Host2`` (or ``ID:Host``) from ``EAA_STATIONS``."""
    if not raw:
        return ((default_id, default_name, default_host),)
    out: list[tuple[str, str, str]] = []
    for chunk in raw.split(","):
        parts = [p.strip() for p in chunk.strip().split(":") if p.strip()]
        if len(parts) == 2:
            out.append((parts[0], parts[0], parts[1]))
        elif len(parts) >= 3:
            out.append((parts[0], parts[1], ":".join(parts[2:])))
    return tuple(out) or ((default_id, default_name, default_host),)


def _load_master_config() -> dict[str, Any]:
    """Load the master JSON config file if present (``EAA_CONFIG_PATH`` or ``hmi-web/config.json``)."""
    path = _env_path("EAA_CONFIG_PATH") or _DEFAULT_CONFIG_PATH
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Master config at {path} must be a JSON object")
    return data


def _sensors_from_config(raw: Any) -> tuple[tuple[str, str], ...] | None:
    if not isinstance(raw, list):
        return None
    items: list[tuple[str, str]] = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("label"):
            sid = str(entry.get("id") or entry["label"]).strip().lower().replace(" ", "_")
            items.append((sid, str(entry["label"]).strip()))
        elif isinstance(entry, str) and entry.strip():
            label = entry.strip()
            items.append((label.lower().replace(" ", "_"), label))
    return tuple(items)


def _stations_from_config(raw: Any) -> tuple[tuple[str, str, str], ...] | None:
    if not isinstance(raw, list):
        return None
    items: list[tuple[str, str, str]] = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("id"):
            sid = str(entry["id"]).strip()
            name = str(entry.get("name") or sid).strip()
            host = str(entry.get("host") or "127.0.0.1").strip()
            items.append((sid, name, host))
    return tuple(items) if items else None


@dataclass(frozen=True, slots=True)
class Settings:
    # Station identity & configurable sensor strip
    station_id: str
    station_name: str
    station_host: str
    stations: tuple[tuple[str, str, str], ...]
    sensors: tuple[tuple[str, str], ...]
    operator_name: str

    # Orchestrator integration
    hmi_sub_endpoint: str
    ui_push_endpoint: str
    db_path: Path | None
    stale_after_s: float

    # Conveyor command limits (UI hints; the Safety Arbiter remains authoritative)
    speed_min_mms: int
    speed_max_mms: int
    speed_step_mms: int
    master_config: dict[str, Any]

    # Buffers
    recent_events: int
    voice_entries: int
    history_points: int
    snapshot_cache: int
    ws_queue_size: int

    # Persistence / serving
    settings_path: Path
    frontend_dist: Path
    cors_origins: tuple[str, ...]
    host: str
    port: int
    log_level: str
    app_version: str | None
    app_build: str | None

    @classmethod
    def from_env(cls) -> Settings:
        raw = _load_master_config()
        station_cfg = raw.get("station") if isinstance(raw.get("station"), dict) else {}
        speed_cfg = raw.get("speed_limits") if isinstance(raw.get("speed_limits"), dict) else {}
        ipc_cfg = raw.get("ipc") if isinstance(raw.get("ipc"), dict) else {}
        server_cfg = raw.get("server") if isinstance(raw.get("server"), dict) else {}
        app_cfg = raw.get("app") if isinstance(raw.get("app"), dict) else {}

        cors_raw = _env_str("EAA_HMI_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
        station_id = _env_str("EAA_STATION_ID", str(station_cfg.get("id") or "CVY001"))
        station_name = _env_str("EAA_STATION_NAME", str(station_cfg.get("name") or "PCB Inspection Conveyor"))
        station_host = _env_str("EAA_STATION_HOST", str(station_cfg.get("host") or "127.0.0.1"))

        if os.getenv("EAA_STATIONS"):
            stations = _parse_stations(os.getenv("EAA_STATIONS"), station_id, station_name, station_host)
        else:
            stations = _stations_from_config(raw.get("stations")) or ((station_id, station_name, station_host),)

        if os.getenv("EAA_SENSORS") is not None:
            sensors = _parse_sensors(os.getenv("EAA_SENSORS"))
        else:
            cfg_sensors = _sensors_from_config(raw.get("sensors"))
            sensors = cfg_sensors if cfg_sensors is not None else _DEFAULT_SENSORS

        db_raw = ipc_cfg.get("db_path")
        default_db = Path(db_raw).expanduser() if isinstance(db_raw, str) and db_raw else None

        return cls(
            station_id=station_id,
            station_name=station_name,
            station_host=station_host,
            stations=stations,
            sensors=sensors,
            operator_name=_env_str("EAA_OPERATOR_NAME", str(raw.get("operator") or "Operator")),
            hmi_sub_endpoint=_endpoint(
                "EAA_HMI_SUB_ENDPOINT",
                "hmi.pub",
                str(ipc_cfg.get("hmi_sub_endpoint") or "tcp://127.0.0.1:5560"),
            ),
            ui_push_endpoint=_endpoint(
                "EAA_UI_PUSH_ENDPOINT",
                "ui.pull",
                str(ipc_cfg.get("ui_push_endpoint") or "tcp://127.0.0.1:5561"),
            ),
            db_path=_env_path("EAA_DB_PATH") or default_db,
            stale_after_s=_env_float("EAA_HMI_STALE_AFTER_S", float(ipc_cfg.get("stale_after_s") or 3.0)),
            speed_min_mms=_env_int("EAA_SPEED_MIN_MMS", int(speed_cfg.get("min_mms") or 50)),
            speed_max_mms=_env_int("EAA_SPEED_MAX_MMS", int(speed_cfg.get("max_mms") or 250)),
            speed_step_mms=_env_int("EAA_SPEED_STEP_MMS", int(speed_cfg.get("step_mms") or 10)),
            master_config=raw,
            recent_events=_env_int("EAA_HMI_RECENT_EVENTS", 500),
            voice_entries=_env_int("EAA_HMI_VOICE_ENTRIES", 50),
            history_points=_env_int("EAA_HMI_HISTORY_POINTS", 120),
            snapshot_cache=_env_int("EAA_HMI_SNAPSHOT_CACHE", 64),
            ws_queue_size=_env_int("EAA_HMI_WS_QUEUE", 512),
            settings_path=_env_path("EAA_HMI_SETTINGS_PATH")
            or (_BACKEND_DIR / "data" / "hmi_settings.json"),
            frontend_dist=_env_path("EAA_HMI_FRONTEND_DIST") or (_HMI_WEB_DIR / "frontend" / "dist"),
            cors_origins=tuple(o.strip() for o in cors_raw.split(",") if o.strip()),
            host=_env_str("EAA_HMI_HOST", str(server_cfg.get("host") or "0.0.0.0")),
            port=_env_int("EAA_HMI_PORT", int(server_cfg.get("port") or 8000)),
            log_level=_env_str("EAA_LOG_LEVEL", str(server_cfg.get("log_level") or "INFO")).upper(),
            app_version=os.getenv("EAA_APP_VERSION") or app_cfg.get("version"),
            app_build=os.getenv("EAA_APP_BUILD") or app_cfg.get("build"),
        )
