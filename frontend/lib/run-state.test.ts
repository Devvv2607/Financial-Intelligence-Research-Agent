// Reducer tests on event sequences recorded from the scripted runtime
// (scripts/gen_frontend_fixtures.py): the real wire format, zero tokens.
import { describe, expect, it } from "vitest";

import apple from "./__fixtures__/apple-replan.json";
import bankPaused from "./__fixtures__/bank-paused.json";
import bankResumed from "./__fixtures__/bank-resumed.json";
import compare from "./__fixtures__/compare.json";
import { applyEvent, initialRun, type RunState, statusLine } from "./run-state";
import type { AgentEvent } from "./types";

type Rec = [number, AgentEvent][];
const replay = (events: Rec, from: RunState = initialRun) =>
  events.reduce((s, [id, e]) => applyEvent(s, e, id), from);

describe("run reducer", () => {
  it("builds a finished run: parallel steps, sources, verified answer", () => {
    const s = replay(compare as Rec);
    expect(s.phase).toBe("done");
    expect(Object.values(s.steps).map((x) => [x.status, x.mode])).toEqual(
      Array(4).fill(["done", "direct"]),
    );
    expect(s.sourceOrder.length).toBeGreaterThan(5);
    // the streamed preview differs from the verified answer: the UI swaps them
    expect(s.preview).not.toContain("[unverified]");
    expect(s.final?.answer).toContain("[unverified]");
    expect(s.nodes["finalize"]).toBe("done");
    expect(Object.values(s.activeSteps).every((n) => n === 0)).toBe(true);
    expect(statusLine(s)).toMatch(/^Done · 4 steps/);
  });

  it("is idempotent under replays (reconnect after=n re-sends nothing new)", () => {
    const events = compare as Rec;
    const once = replay(events);
    const half = replay(events.slice(0, 120));
    expect(replay(events, half)).toEqual(once); // overlapping ids are ignored
  });

  it("labels a replan round with the critic's gaps and records agentic tool calls", () => {
    const s = replay(apple as Rec);
    expect(s.rounds.map((r) => r.round)).toEqual([1, 2]);
    expect(s.rounds[1].reason).toEqual(["Recent news on Apple's AI risk"]);
    const agentic = s.steps["r2s1"];
    expect(agentic.mode).toBe("agentic");
    expect(agentic.tools.map((t) => t.tool)).toEqual(["search_news"]);
  });

  it("pauses for a clarification, then a resume continues the same run", () => {
    const paused = replay(bankPaused as Rec);
    expect(paused.phase).toBe("paused");
    expect(paused.clarification?.options).toContain("JPMorgan Chase (JPM)");
    expect(paused.nodes["ask_clarification"]).toBe("paused");
    const done = replay(bankResumed as Rec, paused); // new server log: ids restart at 1
    expect(done.runId).toBe(paused.runId);
    expect(done.clarification).toBeNull();
    expect(done.phase).toBe("done");
    expect(done.final?.answer).toContain("JPMorgan Chase");
  });

  it("phase follows the graph: finalize start means verifying", () => {
    const events = compare as Rec;
    const i = events.findIndex(([, e]) => e.event === "node" && e.node === "finalize");
    expect(replay(events.slice(0, i + 1)).phase).toBe("verifying");
  });
});
