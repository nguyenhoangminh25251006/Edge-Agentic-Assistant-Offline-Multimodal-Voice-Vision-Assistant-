import type {
  CommandRequest,
  CommandResponse,
  HmiSettings,
  LogEntry,
  LogFilters,
  LogPage,
  StateSnapshot,
  SystemInfo,
} from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
      else if (Array.isArray(body?.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

function logParams(filters: Partial<LogFilters>): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.pageSize) params.set("page_size", String(filters.pageSize));
  filters.severity?.forEach((s) => params.append("severity", s));
  if (filters.source) params.set("source", filters.source);
  if (filters.search) params.set("search", filters.search);
  return params;
}

export const api = {
  system: () => request<SystemInfo>("/api/system"),
  state: () => request<StateSnapshot>("/api/state"),
  sendCommand: (body: CommandRequest) =>
    request<CommandResponse>("/api/commands", { method: "POST", body: JSON.stringify(body) }),
  logs: (filters: LogFilters) => request<LogPage>(`/api/logs?${logParams(filters)}`),
  log: (id: string) => request<LogEntry>(`/api/logs/${encodeURIComponent(id)}`),
  logsCsvUrl: (filters: Partial<LogFilters>) => {
    const params = logParams({ ...filters, page: undefined, pageSize: undefined });
    return `/api/logs/export.csv?${params}`;
  },
  snapshotUrl: (id: string) => `/api/logs/${encodeURIComponent(id)}/snapshot.jpg`,
  settings: () => request<HmiSettings>("/api/settings"),
  updateSettings: (patch: Partial<HmiSettings>) =>
    request<HmiSettings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),
  testMic: () => request<{ ok: boolean; detail: string }>("/api/settings/test-mic", { method: "POST" }),
  checkUpdates: () => request<{ ok: boolean; detail: string }>("/api/updates/check", { method: "POST" }),
  installUpdate: () => request<{ ok: boolean; detail: string }>("/api/updates/install", { method: "POST" }),
  rollbackUpdate: (version: string) =>
    request<{ ok: boolean; detail: string }>("/api/updates/rollback", {
      method: "POST",
      body: JSON.stringify({ version }),
    }),
  cloudSyncNow: () => request<{ ok: boolean; detail: string }>("/api/cloud/sync", { method: "POST" }),
  cameraStreamUrl: "/api/camera/stream.mjpg",
};
