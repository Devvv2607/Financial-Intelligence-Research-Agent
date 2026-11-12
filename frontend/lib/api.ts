import type { GraphJSON, Thread, ThreadDetail } from "./types";

// Base URL of the FastAPI service as seen from the browser.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type CheckResult = { ok: boolean; detail: string | null };

export type ReadyResponse = {
  status: "ready" | "not_ready";
  checks: Record<string, CheckResult>;
};

/** A non-2xx answer from the API, with its `detail` message when it sent one. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    cache: "no-store",
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

export async function toError(res: Response): Promise<ApiError> {
  let detail = `API returned ${res.status}`;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    // not JSON: keep the status message
  }
  return new ApiError(res.status, detail);
}

export async function fetchReady(signal?: AbortSignal): Promise<ReadyResponse> {
  const res = await fetch(`${API_URL}/ready`, { signal, cache: "no-store" });
  // /ready returns 503 with a valid body when a dependency is down.
  if (res.status !== 200 && res.status !== 503) {
    throw new Error(`API returned ${res.status}`);
  }
  return (await res.json()) as ReadyResponse;
}

export const api = {
  listThreads: () => request<Thread[]>("/api/threads"),
  createThread: () => request<Thread>("/api/threads", { method: "POST", body: "{}" }),
  getThread: (id: string) => request<ThreadDetail>(`/api/threads/${id}`),
  getGraph: () => request<GraphJSON>("/api/graph"),
};
