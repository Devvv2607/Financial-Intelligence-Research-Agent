import { describe, expect, it } from "vitest";

import graph from "./__fixtures__/graph.json";
import { collapse } from "./graph-collapse";
import type { GraphJSON } from "./types";

describe("collapse", () => {
  const c = collapse(graph as GraphJSON);

  it("turns 33 nodes into 15 readable ones: 11 main + 4 specialists", () => {
    expect((graph as GraphJSON).nodes).toHaveLength(33);
    expect(c.nodes).toHaveLength(15);
    expect(c.nodes.filter((n) => n.kind === "agent").map((n) => n.label).sort()).toEqual([
      "Filings",
      "Financials",
      "News",
      "Screener",
    ]);
    expect(c.nodes.filter((n) => n.group === "intake")).toHaveLength(5);
  });

  it("keeps the fan-out, join and the loops, and each specialist's inner loop on demand", () => {
    const has = (s: string, t: string) => c.edges.some((e) => e.source === s && e.target === t);
    expect(has("dispatcher", "filings_agent") && has("filings_agent", "join")).toBe(true);
    expect(c.edges.filter((e) => e.loop).map((e) => `${e.source}->${e.target}`).sort()).toEqual([
      "ask_clarification->entity_resolver",
      "ask_clarification->query_analyzer",
      "critic->planner",
      "join->dispatcher",
    ]);
    expect(c.inner["news_agent"].nodes.map((n) => n.label).sort()).toEqual([
      "direct",
      "finish",
      "llm",
      "prepare",
      "tools",
    ]);
  });
});
