// Base URL of the FastAPI service as seen from the browser.
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type CheckResult = { ok: boolean; detail: string | null };

export type ReadyResponse = {
  status: "ready" | "not_ready";
  checks: Record<string, CheckResult>;
};

export async function fetchReady(signal?: AbortSignal): Promise<ReadyResponse> {
  const res = await fetch(`${API_URL}/ready`, { signal, cache: "no-store" });
  // /ready returns 503 with a valid body when a dependency is down.
  if (res.status !== 200 && res.status !== 503) {
    throw new Error(`API returned ${res.status}`);
  }
  return (await res.json()) as ReadyResponse;
}
