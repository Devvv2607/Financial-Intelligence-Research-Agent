"use client";

import { useQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";

import { type Status, StatusChip } from "@/components/ui/status-chip";
import { api } from "@/lib/api";
import type { Thread } from "@/lib/types";

const THREAD_STATUS: Record<Thread["status"], Status | null> = {
  idle: null,
  running: "running",
  awaiting_clarification: "waiting",
  error: "failed",
};

function ago(iso: string): string {
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function ThreadList({
  current,
  onSelect,
  onNew,
}: {
  current: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  const threads = useQuery({ queryKey: ["threads"], queryFn: api.listThreads, refetchInterval: 10_000 });
  const today = new Date().toDateString();
  const groups: [string, Thread[]][] = [
    ["Today", (threads.data ?? []).filter((t) => new Date(t.updated_at).toDateString() === today)],
    ["Earlier", (threads.data ?? []).filter((t) => new Date(t.updated_at).toDateString() !== today)],
  ];
  return (
    <nav className="flex min-h-0 flex-1 flex-col border-r border-border" aria-label="Research threads">
      <button
        type="button"
        onClick={onNew}
        className="m-3 flex items-center justify-center gap-1.5 rounded-md border border-border py-1.5 text-sm hover:border-accent"
      >
        <Plus className="size-4" aria-hidden /> New research
      </button>
      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {threads.isLoading && <p className="px-2 text-sm text-muted">Loading…</p>}
        {threads.isError && <p className="px-2 text-sm text-bad">Couldn&apos;t load threads.</p>}
        {threads.data?.length === 0 && <p className="px-2 text-sm text-muted">No research yet.</p>}
        {groups.map(
          ([label, items]) =>
            items.length > 0 && (
              <section key={label} className="mb-3">
                <h3 className="px-2 pb-1 font-mono text-[11px] uppercase tracking-wide text-faint">{label}</h3>
                {items.map((t) => {
                  const st = THREAD_STATUS[t.status];
                  return (
                    <button
                      key={t.id}
                      type="button"
                      onClick={() => onSelect(t.id)}
                      aria-current={current === t.id}
                      className={`w-full rounded-md px-2 py-1.5 text-left text-sm ${current === t.id ? "bg-surface-2" : "hover:bg-surface"}`}
                    >
                      <span className="line-clamp-2">{t.title || "Untitled"}</span>
                      <span className="mt-0.5 flex items-center gap-2 text-xs text-muted">
                        {st && <StatusChip status={st} compact />}
                        {ago(t.updated_at)}
                      </span>
                    </button>
                  );
                })}
              </section>
            ),
        )}
      </div>
    </nav>
  );
}
