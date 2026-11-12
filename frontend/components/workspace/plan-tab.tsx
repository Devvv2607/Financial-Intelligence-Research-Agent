"use client";

import { ChevronRight, Repeat } from "lucide-react";
import { useState } from "react";

import { type Status, StatusChip } from "@/components/ui/status-chip";
import type { RunState, StepStatus, StepView } from "@/lib/run-state";

export const AGENT_COLOR: Record<string, string> = {
  filings: "var(--agent-filings)",
  financials: "var(--agent-financials)",
  news: "var(--agent-news)",
  screener: "var(--agent-screener)",
};

const STEP_STATUS: Record<StepStatus, Status> = {
  pending: "pending",
  running: "running",
  done: "done",
  failed: "failed",
  skipped: "skipped",
};

export function PlanTab({ run }: { run: RunState }) {
  if (!run.rounds.length) {
    return <p className="p-4 text-sm text-muted">The plan appears here as soon as the planner emits it.</p>;
  }
  return (
    <div className="space-y-4 p-3">
      {run.rounds.map((r) => (
        <section key={r.round} className="space-y-2">
          <h3 className="font-mono text-xs uppercase tracking-wide text-muted">
            Round {r.round}
            {r.round > 1 && r.reason.length > 0 && (
              <span className="normal-case tracking-normal"> · added because: {r.reason.join("; ")}</span>
            )}
          </h3>
          {r.stepIds.map((id) => run.steps[id] && <StepCard key={id} step={run.steps[id]} />)}
        </section>
      ))}
      {run.critique && (
        <p className="border-t border-border pt-3 text-xs text-muted">
          Critic: {run.critique.verdict === "ok" ? "enough to answer" : `gaps: ${run.critique.gaps.join("; ")}`}
        </p>
      )}
    </div>
  );
}

function StepCard({ step }: { step: StepView }) {
  const [open, setOpen] = useState(false);
  const why = step.error ?? step.reason;
  return (
    <article className="rounded-md border border-border bg-surface p-2.5 text-sm">
      <button
        type="button"
        className="flex w-full items-start gap-2 text-left"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <ChevronRight className={`mt-0.5 size-4 shrink-0 text-faint transition-transform ${open ? "rotate-90" : ""}`} aria-hidden />
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2 font-mono text-xs">
            <span className="text-muted">{step.id}</span>
            <span className="inline-flex items-center gap-1">
              <span className="size-2 rounded-full" style={{ background: AGENT_COLOR[step.agent] }} aria-hidden />
              {step.agent}
            </span>
            <span className="rounded bg-surface-2 px-1 text-muted">{step.mode}</span>
            {step.dependsOn.length > 0 && <span className="text-faint">after {step.dependsOn.join(", ")}</span>}
            <span className="ml-auto flex items-center gap-2">
              {step.durationMs !== null && <span className="tabular-nums text-muted">{(step.durationMs / 1000).toFixed(1)}s</span>}
              <StatusChip status={STEP_STATUS[step.status]} />
            </span>
          </div>
          <p>{step.goal}</p>
          {why && step.status !== "done" && <p className="text-xs text-muted">{why}</p>}
        </div>
      </button>
      {open && (
        <div className="mt-2 space-y-1 border-t border-border pt-2 font-mono text-xs">
          {step.tools.length === 0 && <p className="text-faint">No tool calls yet.</p>}
          {step.tools.map((t, i) => (
            <p key={i} className="flex flex-wrap items-center gap-2">
              <span className={t.ok ? "text-foreground" : "text-bad"}>
                {t.tool}({Object.entries(t.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ")})
              </span>
              <span className="text-muted">{t.ok ? "ok" : "failed"} · {t.evidence} sources</span>
              {t.reused && (
                <span className="inline-flex items-center gap-1 text-muted" title="Identical to an earlier call in this step: result reused, not re-run">
                  <Repeat className="size-3" aria-hidden /> reused
                </span>
              )}
            </p>
          ))}
          {step.summary && <p className="whitespace-pre-wrap font-sans text-muted">{step.summary}</p>}
        </div>
      )}
    </article>
  );
}
