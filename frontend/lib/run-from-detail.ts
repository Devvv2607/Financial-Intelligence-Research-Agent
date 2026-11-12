import { initialRun, type RunState, type StepView } from "./run-state";
import type { ThreadDetail } from "./types";

/**
 * Rebuild a run view from GET /api/threads/{id} (no event log): for threads opened later,
 * or after the live log expired / the API restarted. Token-by-token history is gone; the
 * plan, steps, sources, citations and answer are not.
 */
export function runFromDetail(d: ThreadDetail): RunState | null {
  const l = d.latest;
  if (!l) return null;
  const steps: Record<string, StepView> = {};
  const rounds = new Map<number, string[]>();
  for (const p of l.plan) {
    const r = l.step_results[p.id];
    const round = Number(/^r(\d+)/.exec(p.id)?.[1] ?? 1);
    rounds.set(round, [...(rounds.get(round) ?? []), p.id]);
    const started = r?.started_at ? Date.parse(r.started_at) : null;
    const finished = r?.finished_at ? Date.parse(r.finished_at) : null;
    steps[p.id] = {
      id: p.id,
      round,
      goal: p.goal,
      agent: p.agent,
      mode: r?.mode === "direct" || r?.mode === "agentic" ? r.mode : p.tool_calls?.length ? "direct" : "agentic",
      dependsOn: p.depends_on ?? [],
      status: r ? r.status : "pending",
      durationMs: started && finished ? finished - started : null,
      summary: r?.summary ?? null,
      error: r?.error ?? null,
      reason: r?.skip_reason ?? null,
      tools: (r?.tool_calls ?? []).map((t) => ({
        tool: t.tool,
        args: t.args,
        ok: t.ok,
        evidence: t.evidence ?? 0,
        reused: t.reused ?? false,
      })),
      sources: r?.evidence_ids?.length ?? 0,
    };
  }
  const sources = Object.fromEntries(l.evidence.map((e) => [e.source_id, e]));
  // No node events for a rebuilt run: mark the path it must have taken, so the graph
  // still shows the finished run (intake -> plan -> the specialists used -> answer).
  const nodes: RunState["nodes"] = {};
  if (d.thread.status === "idle" && l.plan.length) {
    for (const n of ["start_turn", "guardrail", "entity_resolver", "query_analyzer", "planner", "dispatcher", "join", "critic", "synthesizer", "finalize"]) {
      nodes[n] = "done";
    }
    for (const p of l.plan) nodes[`${p.agent}_agent`] = "done";
  }
  if (d.thread.status === "awaiting_clarification") nodes["ask_clarification"] = "paused";
  const lastAnswer = [...d.messages].reverse().find((m) => m.role === "assistant")?.content;
  const status = d.thread.status;
  const pending = d.pending_clarification;
  return {
    ...initialRun,
    runId: l.run_id ?? null,
    threadId: d.thread.id,
    phase:
      status === "awaiting_clarification"
        ? "paused"
        : status === "running"
          ? "running"
          : status === "error"
            ? "error"
            : "done",
    rounds: [...rounds.entries()].map(([round, stepIds]) => ({ round, reason: [], stepIds })),
    steps,
    nodes,
    sources,
    sourceOrder: l.evidence.map((e) => e.source_id),
    clarification: pending
      ? { question: pending.question, options: pending.options, source: pending.source ?? null }
      : null,
    final:
      status === "idle" && lastAnswer
        ? { answer: lastAnswer, citations: l.citations, check: l.citation_check ?? null }
        : null,
    error: status === "error" ? "The last run failed." : null,
  };
}
