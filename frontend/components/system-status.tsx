"use client";

import { useEffect, useState } from "react";

import { API_URL, fetchReady, type ReadyResponse } from "@/lib/api";

type State =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "loaded"; data: ReadyResponse };

const POLL_MS = 5000;

export function SystemStatus() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    const load = () =>
      fetchReady(controller.signal)
        .then((data) => setState({ kind: "loaded", data }))
        .catch((err: unknown) => {
          if (controller.signal.aborted) return;
          setState({ kind: "error", message: err instanceof Error ? err.message : String(err) });
        });
    load();
    const id = setInterval(load, POLL_MS);
    return () => {
      controller.abort();
      clearInterval(id);
    };
  }, []);

  return (
    <section className="rounded-lg border border-border p-4" aria-live="polite">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium uppercase tracking-wide text-muted">System status</h2>
        <span className="font-mono text-xs text-muted">{API_URL}/ready</span>
      </div>
      {state.kind === "loading" && <p className="text-muted">Checking…</p>}
      {state.kind === "error" && (
        <p className="text-bad">API unreachable: {state.message}</p>
      )}
      {state.kind === "loaded" && (
        <ul className="space-y-2">
          {Object.entries(state.data.checks).map(([name, check]) => (
            <li key={name} className="flex items-center justify-between font-mono text-sm">
              <span>{name}</span>
              <span className={check.ok ? "text-ok" : "text-bad"}>
                {check.ok ? "ok" : `fail${check.detail ? ` — ${check.detail}` : ""}`}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
