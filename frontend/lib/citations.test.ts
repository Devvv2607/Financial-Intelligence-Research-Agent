import { describe, expect, it } from "vitest";

import { changedMarkers, markers } from "@/components/workspace/answer-markdown";

import compare from "./__fixtures__/compare.json";
import { applyEvent, initialRun, type RunState } from "./run-state";
import type { AgentEvent } from "./types";

type Rec = [number, AgentEvent][];

describe("citation numbering", () => {
  it("the verified answer cites 1, 2, 3 by first appearance, matching its citation list", () => {
    const s = (compare as Rec).reduce((acc: RunState, [id, e]) => applyEvent(acc, e, id), initialRun);
    const nums = markers(s.final!.answer).filter((m) => m !== "unverified").map(Number);
    const firstSeen = [...new Set(nums)];
    expect(firstSeen).toEqual(firstSeen.map((_, i) => i + 1));
    expect(s.final!.citations.map((c) => c.n)).toEqual(firstSeen);
  });

  it("compares the draft with the answer in the answer's numbering", () => {
    // draft cited [4] [9] [4] [12]; verification kept 4 and 9 (renumbered 1, 2), flagged
    // the third claim and dropped 12's source
    const changed = changedMarkers("a [4] b [9] c [4] d [12]", "a [1] b [2] c [unverified] d [1]", {
      "4": 1,
      "9": 2,
    });
    expect([...changed.entries()]).toEqual([
      [2, "1"], // was [4] (now 1), became unverified
      [3, "x12"], // the draft's source 12 isn't kept
    ]);
  });
});
