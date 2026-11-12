"use client";

import { ArrowUp } from "lucide-react";
import { useState } from "react";

// The spec's demo queries (BUILD_SPEC §4.6), offered on the empty state.
export const EXAMPLES = [
  "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks. Does recent news support any of them?",
  "How did Apple's risk factors change between its last two 10-Ks?",
  "Which healthcare companies had the fastest revenue growth last fiscal year, and what are their main regulatory risks?",
  "What material events has Boeing reported in 8-Ks this year?",
  "How risky is the bank?",
  "Should I buy Tesla?",
];

export function Composer({
  disabled,
  hint,
  onSubmit,
}: {
  disabled: boolean;
  hint: string | null;
  onSubmit: (q: string) => void;
}) {
  const [q, setQ] = useState("");
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

export function ExampleChips({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="mx-auto max-w-2xl space-y-4 py-12 text-center">
      <h1 className="text-xl font-semibold tracking-tight">Research SEC filings and financial news</h1>
      <p className="text-sm text-muted">
        The agent plans, runs specialists in parallel, checks its own work, asks when a question
        is ambiguous, and cites every claim.
      </p>
      <div className="flex flex-wrap justify-center gap-2 pt-2">
        {EXAMPLES.map((e) => (
          <button
            key={e}
            type="button"
            onClick={() => onPick(e)}
            className="max-w-xs rounded-md border border-border bg-surface px-3 py-1.5 text-left text-sm text-muted hover:border-accent hover:text-foreground"
          >
            {e}
          </button>
        ))}
      </div>
    </div>
  );
}
