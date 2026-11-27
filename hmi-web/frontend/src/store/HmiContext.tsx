import { createContext, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from "react";
import { api } from "../api/client";
import type {
  CameraStatus,
  ConnectionStatus,
  DiagnosticsSnapshot,
  HmiEvent,
  HmiSettings,
  InspectionResult,
  StateChange,
  StationInfo,
  SystemInfo,
  TelemetryPoint,
  TelemetrySnapshot,
  VoiceEntry,
  VoiceStatus,
  WsMessage,
} from "../api/types";

const MAX_EVENTS = 200;
const MAX_VOICE = 50;
const MAX_HISTORY = 120;
const HISTORY_MIN_GAP_S = 0.5;

export type WsStatus = "connecting" | "open" | "closed";

export interface HmiStore {
  ws: WsStatus;
  system: SystemInfo | null;
  station: StationInfo | null;
  fsm: StateChange | null;
  telemetry: TelemetrySnapshot | null;
  history: TelemetryPoint[];
  events: HmiEvent[];
  inspection: InspectionResult | null;
  voiceStatus: VoiceStatus | null;
  voice: VoiceEntry[];
  diagnostics: DiagnosticsSnapshot | null;
  camera: CameraStatus;
  connection: ConnectionStatus;
  settings: HmiSettings | null;
  snapshotsReady: Record<string, true>;
}

const initialState: HmiStore = {
  ws: "connecting",
  system: null,
  station: null,
  fsm: null,
  telemetry: null,
  history: [],
  events: [],
  inspection: null,
  voiceStatus: null,
  voice: [],
  diagnostics: null,
  camera: {
    online: false,
    camera_id: null,
    width: null,
    height: null,
    fps: null,
    latency_ms: null,
    led_on: null,
    last_frame_at: null,
  },
  connection: {
    orchestrator_connected: false,
    last_message_at: null,
    command_channel_ready: false,
    database_available: false,
  },
  settings: null,
  snapshotsReady: {},
};

type Action =
  | { kind: "ws"; status: WsStatus }
  | { kind: "system"; data: SystemInfo }
  | { kind: "message"; msg: WsMessage };

function appendCapped<T>(list: T[], item: T, cap: number): T[] {
  const next = list.length >= cap ? list.slice(list.length - cap + 1) : list.slice();
  next.push(item);
  return next;
}

function reduce(state: HmiStore, action: Action): HmiStore {
  switch (action.kind) {
    case "ws":
      return { ...state, ws: action.status };
    case "system":
      return { ...state, system: action.data, station: state.station ?? action.data.station };
    case "message":
      return reduceMessage(state, action.msg);
  }
}

function reduceMessage(state: HmiStore, msg: WsMessage): HmiStore {
  switch (msg.type) {
    case "snapshot": {
      const s = msg.data;
      return {
        ...state,
        station: s.station,
        fsm: s.fsm,
        telemetry: s.telemetry,
        history: s.history.slice(-MAX_HISTORY),
        events: s.events.slice(-MAX_EVENTS),
        inspection: s.inspection,
        voiceStatus: s.voice_status,
        voice: s.voice.slice(-MAX_VOICE),
        diagnostics: s.diagnostics,
        camera: s.camera,
        connection: s.connection,
        settings: s.settings,
      };
    }
    case "telemetry": {
      const t = msg.data;
      const last = state.history[state.history.length - 1];
      const history =
        !last || t.ts - last.ts >= HISTORY_MIN_GAP_S
          ? appendCapped(
              state.history,
              { ts: t.ts, speed: t.belt_speed_mms, current: t.motor_current_a, temp: t.motor_temp_c },
              MAX_HISTORY,
            )
          : state.history;
      return { ...state, telemetry: t, history };
    }
    case "state":
      return { ...state, fsm: msg.data };
    case "event":
      return { ...state, events: appendCapped(state.events, msg.data, MAX_EVENTS) };
    case "inspection":
      return { ...state, inspection: msg.data };
    case "voice":
      return { ...state, voice: appendCapped(state.voice, msg.data, MAX_VOICE) };
    case "voice_status":
      return { ...state, voiceStatus: msg.data };
    case "diagnostics":
      return { ...state, diagnostics: msg.data };
    case "camera":
      return { ...state, camera: msg.data };
    case "connection":
      return { ...state, connection: msg.data };
    case "settings":
      return { ...state, settings: msg.data };
    case "snapshot_ready":
      return { ...state, snapshotsReady: { ...state.snapshotsReady, [msg.data.event_id]: true } };
    default:
      return state;
  }
}

const HmiContext = createContext<HmiStore>(initialState);

function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws`;
}

export function HmiProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reduce, initialState);
  const retryRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    const loadSystem = () =>
      api
        .system()
        .then((data) => !cancelled && dispatch({ kind: "system", data }))
        .catch(() => !cancelled && window.setTimeout(loadSystem, 3000));
    loadSystem();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;
    let pingTimer: number | undefined;
    let disposed = false;

    const connect = () => {
      dispatch({ kind: "ws", status: "connecting" });
      socket = new WebSocket(wsUrl());
      socket.onopen = () => {
        retryRef.current = 0;
        dispatch({ kind: "ws", status: "open" });
        pingTimer = window.setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send("ping"), 15000);
      };
      socket.onmessage = (ev) => {
        try {
          dispatch({ kind: "message", msg: JSON.parse(ev.data) as WsMessage });
        } catch {
          /* ignore malformed frame */
        }
      };
      socket.onclose = () => {
        window.clearInterval(pingTimer);
        if (disposed) return;
        dispatch({ kind: "ws", status: "closed" });
        const delay = Math.min(10000, 500 * 2 ** retryRef.current++);
        reconnectTimer = window.setTimeout(connect, delay);
      };
      socket.onerror = () => socket?.close();
    };

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(reconnectTimer);
      window.clearInterval(pingTimer);
      socket?.close();
    };
  }, []);

  return <HmiContext.Provider value={state}>{children}</HmiContext.Provider>;
}

export function useHmi(): HmiStore {
  return useContext(HmiContext);
}

/** True when live data is flowing end-to-end (browser <-> gateway <-> orchestrator). */
export function useLive(): boolean {
  const { ws, connection } = useHmi();
  return useMemo(() => ws === "open" && connection.orchestrator_connected, [ws, connection]);
}
