import { describe, expect, it } from "vitest";

import { RETRY_MS, SLOW_MS, waitForApi } from "./wake";

function script(answers: boolean[]) {
  const probes: number[] = [];
  const waking: number[] = [];
  return {
    probes,
    waking,
    deps: {
      probe: async (timeoutMs: number) => {
        probes.push(timeoutMs);
        return answers.shift() ?? true;
      },
      sleep: async () => undefined,
      onWaking: (n: number) => waking.push(n),
      aborted: () => false,
    },
  };
}

describe("waking a sleeping API", () => {
  it("says nothing when the API answers within 5 seconds", async () => {
    const s = script([true]);
    expect(await waitForApi(s.deps)).toBe(true);
    expect([s.probes, s.waking]).toEqual([[SLOW_MS], []]);
  });

  it("shows the waking state and retries until the API answers", async () => {
    const s = script([false, false, false, true]);
    expect(await waitForApi(s.deps)).toBe(true);
    expect(s.probes).toEqual([SLOW_MS, RETRY_MS, RETRY_MS, RETRY_MS]);
    expect(s.waking).toEqual([1, 2, 3]);
  });

  it("stops when the page goes away", async () => {
    const s = script([false, false]);
    expect(await waitForApi({ ...s.deps, aborted: () => true })).toBe(false);
  });
});
