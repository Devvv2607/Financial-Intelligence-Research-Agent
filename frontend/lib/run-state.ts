import type { AgentEvent, Citation, EventOf, Evidence } from "./types";

// Live state of one run, built only from its SSE events (never from timers), so a
// replay of the same events (reconnect, reload) rebuilds exactly the same state.

export type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export type ToolCallView = {
  tool: string;
  args: Record<string, unknown>;
  ok: boolean;
  evidence: number;
  reused: boolean;
};

export type StepView = {
  id: string;
  round: number;
  goal: string;
  agent: string;
  mode: "direct" | "agentic";
  dependsOn: string[];
  status: StepStatus;
  durationMs: number | null;
  summary: string | null;
  error: string | null;
  reason: string | null;
  tools: ToolCallView[];
  sources: number;
};

export type Round = { round: number; reason: string[]; stepIds: string[] };

export type NodeStatus = "active" | "done" | "paused" | "failed";

export type RunPhase =
  | "starting"
  | "planning"
  | "running"
  | "reviewing"
  | "replanning"
  | "writing"
  | "verifying"
  | "paused"
  | "done"
  | "error";

export type RunState = {
  runId: string | null;
  threadId: string | null;
  phase: RunPhase;
  rounds: Round[];
  steps: Record<string, StepView>;
  sources: Record<string, Evidence>;
  sourceOrder: string[]; // arrival order
  critique: { verdict: "ok" | "gaps"; gaps: string[]; replanning: boolean } | null;
  clarification: { question: string; options: string[]; source: string | null } | null;
  preview: string; // streamed tokens: a preview until `final` arrives
  final: { answer: string; citations: Citation[]; check: Record<string, unknown> | null } | null;
  error: string | null;
  nodes: Record<string, NodeStatus>; // graph node id -> status (subgraph nodes "agent:node")
  activeSteps: Record<string, number>; // specialist node -> running instances
  lastEventId: number;
  // Arrival times (ms) passed in by the caller: the reducer stays pure. They let the UI
  // hold "Verifying citations…" for a minimum time before revealing the final text.
  verifyingSince: number | null;
  finalAt: number | null;
};

export const initialRun: RunState = {
  runId: null,
  threadId: null,
  phase: "starting",
  rounds: [],
  steps: {},
  sources: {},
  sourceOrder: [],
  critique: null,
  clarification: null,
  preview: "",
  final: null,
  error: null,
  nodes: {},
  activeSteps: {},
  lastEventId: 0,
  verifyingSince: null,
  finalAt: null,
};

/** Apply one event. Ids at or below the last applied one are ignored (replays). */
export function applyEvent(s: RunState, e: AgentEvent, id: number, now = 0): RunState {
  // run_started opens a new server-side event log (a resume included), whose ids restart
  // at 1: it always applies and resets the counter.
  if (e.event !== "run_started" && id > 0 && id <= s.lastEventId) return s;
  const next = reduce(s, e, now);
  return id > 0 ? { ...next, lastEventId: id } : next;
}

function reduce(s: RunState, e: AgentEvent, now: number): RunState {
  switch (e.event) {
    case "run_started":
      // A resume continues the paused run: keep what it built so far.
      return {
        ...(e.resumed ? s : initialRun),
        runId: e.run_id,
        threadId: e.thread_id,
        phase: e.resumed ? s.phase : "starting",
        clarification: null,
      };
    case "plan_created":
    case "replan":
      return addRound(s, e);
    case "step_started":
      return updateStep({ ...s, phase: "running" }, e.step_id, { status: "running", mode: e.mode });
    case "tool_called":
      return updateStep(s, e.step_id, (st) => ({
        tools: [
          ...st.tools,
          { tool: e.tool, args: e.args, ok: e.ok, evidence: e.evidence, reused: e.reused },
        ],
      }));
    case "evidence_added": {
      const sources = { ...s.sources };
      const order = [...s.sourceOrder];
      for (const ev of e.sources) {
        if (!(ev.source_id in sources)) order.push(ev.source_id);
        sources[ev.source_id] = ev;
      }
      return updateStep({ ...s, sources, sourceOrder: order }, e.step_id, (st) => ({
        sources: st.sources + e.sources.length,
      }));
    }
    case "step_completed":
      return updateStep(s, e.step_id, {
        status: e.status,
        durationMs: e.duration_ms,
        summary: e.summary,
        error: e.error,
        reason: e.reason,
      });
    case "critique":
      return {
        ...s,
        critique: { verdict: e.verdict, gaps: e.gaps, replanning: e.replanning },
        phase: e.replanning ? "replanning" : "writing",
      };
    case "clarification_needed":
      return {
        ...s,
        phase: "paused",
        clarification: { question: e.question, options: e.options, source: e.source },
      };
    case "node":
      return onNode(s, e, now);
    case "token":
      return { ...s, phase: "writing", preview: s.preview + e.text };
    case "final":
      return {
        ...s,
        phase: "done",
        final: { answer: e.answer, citations: e.citations, check: e.citation_check },
        finalAt: now,
        steps: settle(s.steps),
        activeSteps: {},
      };
    case "error":
      return { ...s, phase: "error", error: e.message, steps: settle(s.steps, "failed") };
  }
}

function addRound(s: RunState, e: EventOf<"plan_created"> | EventOf<"replan">): RunState {
  const steps = { ...s.steps };
  for (const p of e.steps) {
    steps[p.id] = {
      id: p.id,
      round: e.round,
      goal: p.goal,
      agent: p.agent,
      mode: p.tool_calls?.length ? "direct" : "agentic",
      dependsOn: p.depends_on ?? [],
      status: "pending",
      durationMs: null,
      summary: null,
      error: null,
      reason: null,
      tools: [],
      sources: 0,
    };
  }
  // A replan round is labelled with the critic's gaps that caused it.
  const reason = e.event === "replan" ? (s.critique?.gaps ?? []) : [];
  return {
    ...s,
    phase: "planning",
    steps,
    rounds: [...s.rounds, { round: e.round, reason, stepIds: e.steps.map((p) => p.id) }],
  };
}

function updateStep(
  s: RunState,
  id: string,
  patch: Partial<StepView> | ((st: StepView) => Partial<StepView>),
): RunState {
  const st = s.steps[id];
  if (!st) return s; // an event for a step we never saw planned: ignore, don't crash
  const p = typeof patch === "function" ? patch(st) : patch;
  return { ...s, steps: { ...s.steps, [id]: { ...st, ...p } } };
}

function onNode(s: RunState, e: EventOf<"node">, now: number): RunState {
  const nodes = { ...s.nodes };
  const active = { ...s.activeSteps };
  const top = e.node.split(":")[0];
  const isSpecialist = top.endsWith("_agent");
  if (e.phase === "start") {
    nodes[e.node] = "active";
    if (isSpecialist && e.node === top) active[top] = (active[top] ?? 0) + 1;
  } else {
    nodes[e.node] = e.error ? "failed" : e.interrupted ? "paused" : "done";
    if (isSpecialist && e.node === top) active[top] = Math.max(0, (active[top] ?? 1) - 1);
    // A specialist with parallel steps stays active until its last instance ends.
    if (isSpecialist && e.node === top && active[top] > 0) nodes[top] = "active";
  }
  let phase = s.phase;
  if (e.node === "planner" && e.phase === "start") phase = "planning";
  if (e.node === "critic" && e.phase === "start") phase = "reviewing";
  const verifying = e.node === "finalize" && e.phase === "start";
  if (verifying) phase = "verifying";
  return {
    ...s,
    nodes,
    activeSteps: active,
    phase,
    verifyingSince: verifying && s.verifyingSince === null ? now : s.verifyingSince,
  };
}

function settle(steps: Record<string, StepView>, to: StepStatus = "skipped") {
  const out: Record<string, StepView> = {};
  for (const [k, v] of Object.entries(steps)) {
    out[k] = v.status === "running" || v.status === "pending" ? { ...v, status: to } : v;
  }
  return out;
}

/** The status line under the question: always derived from state, never guessed. */
export function statusLine(s: RunState): string {
  const steps = Object.values(s.steps);
  const running = steps.filter((x) => x.status === "running").length;
  switch (s.phase) {
    case "starting":
      return "Starting…";
    case "planning":
      return steps.length ? `Planning done: ${steps.length} steps` : "Planning…";
    case "running":
      return running > 1 ? `Running ${running} steps in parallel` : "Running steps…";
    case "reviewing":
      return "Reviewing the results…";
    case "replanning":
      return `Gaps found, replanning (round ${s.rounds.length + 1})`;
    case "writing":
      return "Writing the answer…";
    case "verifying":
      return "Verifying citations…";
    case "paused":
      return "Needs your answer";
    case "done":
      return `Done · ${steps.length} steps · ${s.sourceOrder.length} sources`;
    case "error":
      return "The run failed";
  }
}
