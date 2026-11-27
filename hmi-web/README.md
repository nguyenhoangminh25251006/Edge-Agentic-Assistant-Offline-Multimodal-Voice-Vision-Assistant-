# EAA Web HMI (`container-web`)

Offline local Web HMI for the **Edge Agentic Assistant (EAA)** PCB inspection conveyor (`CVY001`).

## Architecture

- **Frontend (`frontend/`)**: React 18 + Vite + TypeScript + Tailwind CSS + bundled `@fontsource/inter` & `@fontsource/ibm-plex-mono` (100% offline, zero external CDN dependencies).
- **Backend (`backend/`)**: FastAPI REST + WebSocket gateway (`app/`):
  - Subscribes to `container-orchestrator` ZeroMQ PUB socket (`hmi.pub`, prefix `hmi.*`: `hmi.telemetry`, `hmi.state`, `hmi.event`, `hmi.inspection`, `hmi.voice`, `hmi.voice_status`, `hmi.diagnostics`, `hmi.frame`, `hmi.snapshot`).
  - Pushes operator commands (`UICommandWire`) to the orchestrator's ZeroMQ PULL socket (`ui.pull`, `ZMQ_IMMEDIATE=1`).
  - Reads the orchestrator's append-only SQLite WAL database (`EAA_DB_PATH`, schema v2) strictly read-only (`?mode=ro` + `PRAGMA query_only=ON`), falling back to the in-memory event ring when `EAA_DB_PATH` is unset.
  - Serves the compiled React SPA (`frontend/dist`) in production so everything runs from one process on port `8000`.

## Running Locally

### 1. Frontend (Build or Dev)
```bash
cd hmi-web/frontend
npm install
npm run build     # builds into frontend/dist (served directly by FastAPI)
# or for hot-reload dev:
npm run dev       # http://127.0.0.1:5173 (proxies /api and /ws -> :8000)
```

### 2. Backend (FastAPI Gateway)
```bash
cd hmi-web/backend
python -m venv .venv
# activate .venv, then:
pip install -r requirements.txt
python -m app     # serves API + built SPA on http://0.0.0.0:8000
```

## Key Environment Variables

| Variable | Default (Linux / Windows) | Description |
| :--- | :--- | :--- |
| `EAA_IPC_DIR` | `/tmp/eaa` | Directory for ZeroMQ IPC sockets (`hmi.pub`, `ui.pull`) on Linux |
| `EAA_HMI_SUB_ENDPOINT` | `ipc://<EAA_IPC_DIR>/hmi.pub` / `tcp://127.0.0.1:5560` | Orchestrator HMI PUB endpoint |
| `EAA_UI_PUSH_ENDPOINT` | `ipc://<EAA_IPC_DIR>/ui.pull` / `tcp://127.0.0.1:5561` | Orchestrator UI PULL endpoint |
| `EAA_DB_PATH` | *(unset -> memory fallback)* | Path to orchestrator SQLite WAL database (`edge_system.db`) |
| `EAA_CONFIG_PATH` | `hmi-web/config.json` | Path to master JSON config file (station, stations, interchangeable `sensors`, `speed_limits`, `ipc`, `server`) |
| `EAA_STATION_ID` | `CVY001` | Active conveyor station ID (overrides `config.json`) |
| `EAA_STATIONS` | *(single station)* | Optional comma-separated station list (`ID:Name:Host,...`) |
| `EAA_SENSORS` | `motor_driver:Motor Driver,rotary_encoder:Rotary Encoder,...` | Comma-separated interchangeable sensor pills (`id:Label,...`), overridden live whenever `hmi.telemetry` sends `sensors` |
| `EAA_OPERATOR_NAME` | `Operator` | Active operator display name |
| `EAA_HMI_PORT` | `8000` | HTTP/WebSocket listening port |
