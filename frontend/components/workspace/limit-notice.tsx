"use client";

import { History } from "lucide-react";

import type { ReplayMeta } from "@/lib/types";

/** A live run was refused by a demo limit: say which, and offer the recorded runs. */
export function LimitNotice({
  message,
  replays,
  onReplay,
}: {
  message: string;
  replays: ReplayMeta[];
  onReplay: (slug: string) => void;
}) {
  return (
    <section role="alert" aria-label="Live run limit" className="mx-3 mb-2 rounded-md border border-accent/60 bg-surface p-3 text-sm">
      <p>{message}</p>
      {replays.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {replays.map((r) => (
            <button
              key={r.slug}
              type="button"
              onClick={() => onReplay(r.slug)}
              className="rounded border border-recorded/60 px-2 py-1 text-left text-xs text-muted hover:border-recorded hover:text-foreground"
            >
              <History className="mr-1 inline size-3.5 text-recorded" aria-hidden />
              Watch a recording: {r.question}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
