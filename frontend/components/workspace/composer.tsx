"use client";

import { ArrowUp, History } from "lucide-react";
import { useState } from "react";

import type { ReplayMeta } from "@/lib/types";

// The spec's demo queries (BUILD_SPEC §4.6), offered on the empty state.
export const EXAMPLES = [
  "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks. Does recent news support any of them?",
  "How did Apple's risk factors change between its last two 10-Ks?",
  "Which healthcare companies had the fastest revenue growth last fiscal year, and what are their main regulatory risks?",
  "What material events has Boeing reported in 8-Ks this year?",
  "How risky is the bank?",
  "Should I buy Tesla?",
];

/** `initial`: text to start with (an example picked from the chips; remount with a new
 * `key` to set it again). */
export function Composer({
  disabled,
  hint,
  initial = "",
  onSubmit,
}: {
  disabled: boolean;
  hint: string | null;
  initial?: string;
  onSubmit: (q: string) => void;
}) {
  const [q, setQ] = useState(initial);
  const send = () => {
    if (!q.trim() || disabled) return;
    onSubmit(q.trim());
    setQ("");
  };
  return (
    <form
      className="border-t border-border p-3"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      {hint && <p className="mb-1.5 text-xs text-muted">{hint}</p>}
      <div className="flex items-end gap-2 rounded-md border border-border bg-surface p-2 focus-within:border-accent">
        <textarea
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={2}
          maxLength={1000}
          disabled={disabled}
          placeholder="Ask about SEC filings, financials or recent news…"
          aria-label="Research question"
          className="min-h-0 flex-1 resize-none bg-transparent text-sm outline-none placeholder:text-faint disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={disabled || !q.trim()}
          aria-label="Ask"
          className="rounded bg-accent p-1.5 text-background disabled:opacity-40"
        >
          <ArrowUp className="size-4" aria-hidden />
        </button>
      </div>
    </form>
  );
}

/**
 * Recorded runs play instantly at zero tokens; examples without a recording only fill
 * the question box, so a live run (with its daily cap) is always a deliberate choice.
 */
export function ExampleChips({
  replays,
  onReplay,
  onPick,
}: {
  replays: ReplayMeta[];
  onReplay: (slug: string) => void;
  onPick: (q: string) => void;
}) {
  const recorded = new Set(replays.map((r) => r.question));
  const chip =
    "max-w-xs rounded-md border bg-surface px-3 py-1.5 text-left text-sm text-muted hover:text-foreground";
  return (
    <div className="mx-auto max-w-2xl space-y-4 py-12 text-center">
      <h1 className="text-xl font-semibold tracking-tight">Research SEC filings and financial news</h1>
      <p className="text-sm text-muted">
        The agent plans, runs specialists in parallel, checks its own work, asks when a question
        is ambiguous, and cites every claim.
      </p>
      {replays.length > 0 && (
        <p className="text-xs text-muted">
          <History className="mr-1 inline size-3.5 text-recorded" aria-hidden />
          Marked examples replay a recorded run instantly; you can then run them live.
        </p>
      )}
      <div className="flex flex-wrap justify-center gap-2 pt-2">
        {replays.map((r) => (
          <button
            key={r.slug}
            type="button"
            onClick={() => onReplay(r.slug)}
            aria-label={`Replay recorded run: ${r.question}`}
            className={`${chip} border-recorded/60 hover:border-recorded`}
          >
            <History className="mr-1.5 inline size-3.5 text-recorded" aria-hidden />
            {r.question}
          </button>
        ))}
        {EXAMPLES.filter((e) => !recorded.has(e)).map((e) => (
          <button key={e} type="button" onClick={() => onPick(e)} className={`${chip} border-border hover:border-accent`}>
            {e}
          </button>
        ))}
      </div>
    </div>
  );
}
