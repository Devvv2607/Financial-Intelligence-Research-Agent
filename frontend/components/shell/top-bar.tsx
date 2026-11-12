"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { fetchReady } from "@/lib/api";

const NAV = [
  { href: "/", label: "Research" },
  { href: "/companies", label: "Companies" },
  { href: "/insights", label: "Insights" },
];

export function TopBar() {
  const ready = useQuery({
    queryKey: ["ready"],
    queryFn: ({ signal }) => fetchReady(signal),
    refetchInterval: 15_000,
  });
  const state = ready.isError ? "down" : ready.data?.status === "ready" ? "ready" : ready.data ? "degraded" : "checking";
  const detail = ready.data
    ? Object.entries(ready.data.checks)
        .map(([k, c]) => `${k}: ${c.ok ? "ok" : (c.detail ?? "failing")}`)
        .join("\n")
    : ready.isError
      ? "API unreachable"
      : "Checking…";
  const dot = {
    ready: "bg-ok",
    degraded: "bg-accent",
    down: "bg-bad",
    checking: "bg-faint",
  }[state];
  return (
    <header className="flex h-12 shrink-0 items-center gap-6 border-b border-border px-4">
      <Link href="/" className="font-mono text-sm font-semibold tracking-tight">
        FIRA<span className="text-accent">_</span>
      </Link>
      <nav className="flex gap-4 text-sm">
        {NAV.map((n) => (
          <Link key={n.href} href={n.href} className="text-muted hover:text-foreground">
            {n.label}
          </Link>
        ))}
      </nav>
      <div className="ml-auto flex items-center gap-2 text-xs text-muted" title={detail}>
        <span className={`size-2 rounded-full ${dot}`} aria-hidden />
        <span>{{ ready: "All systems ready", degraded: "Degraded", down: "API down", checking: "Checking…" }[state]}</span>
      </div>
    </header>
  );
}
