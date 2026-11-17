"use client";

import { useQueryClient } from "@tanstack/react-query";
import { LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { probeApi, waitForApi } from "@/lib/wake";

/**
 * Cold-start screen: if the API doesn't answer within 5 s (a free-tier server waking up),
 * say so and retry automatically; once it answers, reload whatever failed meanwhile.
 */
export function ServerWake() {
  const qc = useQueryClient();
  const [attempt, setAttempt] = useState(0); // 0: not waking (or not known to be)

  useEffect(() => {
    const ctrl = new AbortController();
    let woke = false;
    void waitForApi({
      probe: (ms) => probeApi(ms, ctrl.signal),
      sleep: (ms) => new Promise((r) => setTimeout(r, ms)),
      onWaking: (n) => {
        woke = true;
        if (!ctrl.signal.aborted) setAttempt(n);
      },
      aborted: () => ctrl.signal.aborted,
    }).then((up) => {
      if (!up || !woke || ctrl.signal.aborted) return;
      setAttempt(0);
      void qc.invalidateQueries(); // retry what failed while it was waking
    });
    return () => ctrl.abort();
  }, [qc]);

  if (attempt === 0) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4 backdrop-blur-sm">
      <section role="status" aria-label="Waking the server" className="max-w-sm rounded-md border border-border bg-surface p-5 text-center">
        <LoaderCircle className="mx-auto mb-3 size-6 animate-spin text-accent" aria-hidden />
        <p className="font-medium">Waking the free-tier server, about 30–60s</p>
        <p className="mt-1 text-sm text-muted">
          A free-tier server can sleep or restart when idle. This page retries on its own
          {attempt > 1 ? ` (attempt ${attempt})` : ""}.
        </p>
      </section>
    </div>
  );
}
