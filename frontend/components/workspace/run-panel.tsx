"use client";

import type { RunState } from "@/lib/run-state";

import { GraphTab } from "./graph-tab";
import { PlanTab } from "./plan-tab";
import { SourcesTab } from "./sources-tab";

export type Tab = "plan" | "graph" | "sources";

export function RunPanel({
  run,
  tab,
  onTab,
  highlight,
}: {
  run: RunState;
  tab: Tab;
  onTab: (t: Tab) => void;
  highlight: number | null;
}) {
  const steps = Object.values(run.steps);
  const done = steps.filter((s) => s.status === "done").length;
  const badge: Record<Tab, string | null> = {
    plan: steps.length ? `${done}/${steps.length}` : null,
    graph: null,
    sources: run.sourceOrder.length ? String(run.sourceOrder.length) : null,
  };
  return (
    <aside className="flex min-h-0 flex-1 flex-col border-l border-border">
      <div role="tablist" className="flex shrink-0 border-b border-border">
        {(["plan", "graph", "sources"] as const).map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => onTab(t)}
            className={`flex-1 px-3 py-2 text-sm capitalize ${tab === t ? "border-b-2 border-accent text-foreground" : "text-muted hover:text-foreground"}`}
          >
            {t}
            {badge[t] && <span className="ml-1.5 font-mono text-xs text-muted">{badge[t]}</span>}
          </button>
        ))}
      </div>
      <div className={`flex min-h-0 flex-1 flex-col ${tab === "graph" ? "" : "overflow-y-auto"}`} role="tabpanel">
        {tab === "plan" && <PlanTab run={run} />}
        {tab === "graph" && <GraphTab run={run} />}
        {tab === "sources" && <SourcesTab run={run} highlight={highlight} />}
      </div>
    </aside>
  );
}
