"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "./api";
import { runFromDetail } from "./run-from-detail";
import { applyEvent, initialRun, type RunState } from "./run-state";
import { followRun, type OnEvent, resumeRun, startRun } from "./sse";

const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
const POLL_MS = 2000;
const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const t = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    });
  });

export type Connection = "live" | "reconnecting" | "polling" | "idle";

/**
 * One thread's run, driven by SSE. A run lives on the server (D-041), so a dropped
 * connection never loses it: reconnect with backoff from the last event id (D-043); if
 * the server no longer holds the log (404: expired or API restarted), poll the thread
 * until it settles and rebuild the view from it.
 */
export function useRun() {
  const qc = useQueryClient();
  const [run, setRun] = useState<RunState>(initialRun);
  const [connection, setConnection] = useState<Connection>("idle");
  const [failure, setFailure] = useState<string | null>(null);
  const ref = useRef(run);
  const abort = useRef<AbortController | null>(null);
  // The thread being driven. Set by start/load directly: for a brand-new thread the URL
  // (and anything derived from it) catches up only after the run has started.
  const thread = useRef<string | null>(null);

  const set = useCallback((s: RunState) => {
    ref.current = s;
    setRun(s);
  }, []);
  const onEvent: OnEvent = useCallback(
    (e, id) => set(applyEvent(ref.current, e, id, Date.now())),
    [set],
  );

  const refresh = useCallback(() => {
    void qc.invalidateQueries({ queryKey: ["threads"] });
    if (thread.current) void qc.invalidateQueries({ queryKey: ["thread", thread.current] });
  }, [qc]);

  const recover = useCallback(
    async (signal: AbortSignal) => {
      const id = thread.current;
      if (!id) return;
      for (const wait of BACKOFF_MS) {
        setConnection("reconnecting");
        await sleep(wait, signal);
        if (signal.aborted) return;
        try {
          const { lastEventId, runId } = ref.current;
          await followRun(id, lastEventId, runId ?? undefined, onEvent, signal);
          return; // replayed what was missed and followed to the end
        } catch (err) {
          if (signal.aborted) return;
          if (err instanceof ApiError && err.status === 404) break; // log gone: poll
        }
      }
      setConnection("polling");
      for (;;) {
        try {
          const d = await api.getThread(id);
          if (d.thread.status !== "running") {
            const rebuilt = runFromDetail(d);
            if (rebuilt) set(rebuilt);
            return;
          }
        } catch {
          // API unreachable: keep polling
        }
        await sleep(POLL_MS, signal);
        if (signal.aborted) return;
      }
    },
    [onEvent, set],
  );

  const drive = useCallback(
    async (open: (signal: AbortSignal) => Promise<void>) => {
      abort.current?.abort();
      const ctrl = new AbortController();
      abort.current = ctrl;
      setFailure(null);
      setConnection("live");
      try {
        await open(ctrl.signal);
      } catch (err) {
        if (ctrl.signal.aborted) return;
        if (err instanceof ApiError) {
          // Refused before it started (409 busy, 422 bad input, 429 rate limit): no run.
          setFailure(err.message);
          setConnection("idle");
          return;
        }
        await recover(ctrl.signal);
      }
      if (!ctrl.signal.aborted) {
        setConnection("idle");
        refresh();
      }
    },
    [recover, refresh],
  );

  const start = useCallback(
    (query: string, id: string) => {
      thread.current = id;
      return drive((signal) => startRun(id, query, onEvent, signal));
    },
    [drive, onEvent],
  );

  const resume = useCallback(
    (answer: string) => {
      const id = thread.current;
      return id ? drive((signal) => resumeRun(id, answer, onEvent, signal)) : Promise.resolve();
    },
    [drive, onEvent],
  );

  /** Open a thread: follow a run that is still going (reload mid-run), else show its last
   * state. */
  const load = useCallback(
    async (id: string) => {
      abort.current?.abort();
      thread.current = id;
      set(initialRun);
      setFailure(null);
      const d = await api.getThread(id);
      if (thread.current !== id) return; // another thread was opened meanwhile
      if (d.thread.status === "running") {
        void drive((signal) => followRun(id, 0, undefined, onEvent, signal));
      } else {
        set(runFromDetail(d) ?? initialRun);
      }
    },
    [drive, onEvent, set],
  );

  const reset = useCallback(() => {
    abort.current?.abort();
    thread.current = null;
    setFailure(null);
    setConnection("idle");
    set(initialRun);
  }, [set]);

  useEffect(() => () => abort.current?.abort(), []);

  return { run, connection, failure, start, resume, load, reset };
}
