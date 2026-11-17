"use client";

import { History, Play } from "lucide-react";

import type { ReplayMeta } from "@/lib/types";

/** "Recorded live run · 18 Nov 2026 · not live": a replay is never presented as live. */
export function replayLabel(meta: Pick<ReplayMeta, "source" | "recorded_at">): string {
  const day = new Date(meta.recorded_at).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
  const kind = meta.source === "live" ? "Recorded live run" : "Recorded scripted test run";
  return `${kind} · ${day} · not live`;
}

export function ReplayBanner({
  meta,
  onRunLive,
  disabled,
}: {
  meta: ReplayMeta;
  onRunLive: () => void;
  disabled: boolean;
}) {
  return (
    <section
      aria-label="Recorded run"
      className="sticky top-0 z-10 -mx-4 flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-recorded/60 bg-background/95 px-4 py-2 backdrop-blur"
    >
      <span className="flex items-center gap-1.5 rounded border border-recorded px-2 py-0.5 text-sm font-semibold text-recorded">
        <History className="size-4" aria-hidden />
        {replayLabel(meta)}
      </span>
      <span className="text-xs text-muted">
        {meta.source === "live" ? "A replay of a real run" : "A replay of the zero-token test runtime"}
        {meta.tokens ? ` (it used ${meta.tokens.toLocaleString("en-US")} tokens)` : ""}; playing it costs none.
      </span>
      <button
        type="button"
        onClick={onRunLive}
        disabled={disabled}
        className="ml-auto flex items-center gap-1 rounded bg-accent px-2.5 py-1 text-sm font-medium text-background disabled:opacity-40"
      >
        <Play className="size-3.5" aria-hidden /> Run it live
      </button>
    </section>
  );
}
