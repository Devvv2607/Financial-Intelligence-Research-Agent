"use client";

import { ExternalLink, FileText, Newspaper, Table2, Zap } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { RunState } from "@/lib/run-state";
import type { Evidence } from "@/lib/types";

const TYPE: Record<string, { label: string; icon: typeof FileText }> = {
  risk_factor: { label: "10-K risk factor", icon: FileText },
  filing_chunk: { label: "Filing excerpt", icon: FileText },
  "8k": { label: "8-K", icon: Zap },
  financial_fact: { label: "XBRL fact", icon: Table2 },
  sql: { label: "Database", icon: Table2 },
  news: { label: "News", icon: Newspaper },
};

/** Cards stream in as evidence arrives; after `final`, cited sources come first, numbered
 * as in the answer, and the rest sit under "Retrieved, not cited". */
export function SourcesTab({ run, highlight }: { run: RunState; highlight: number | null }) {
  const [type, setType] = useState<string | null>(null);
  const [ticker, setTicker] = useState<string | null>(null);
  const all = run.sourceOrder.map((id) => run.sources[id]).filter(Boolean);
  const cited = useMemo(
    () => new Map((run.final?.citations ?? []).map((c) => [c.source_id, c.n])),
    [run.final],
  );
  const keep = (e: Evidence) => (!type || e.source_type === type) && (!ticker || e.ticker === ticker);
  const citedCards = all.filter((e) => cited.has(e.source_id)).sort((a, b) => cited.get(a.source_id)! - cited.get(b.source_id)!);
  const rest = all.filter((e) => !cited.has(e.source_id));

  if (!all.length) return <p className="p-4 text-sm text-muted">Sources appear here as the steps retrieve them.</p>;
  const types = [...new Set(all.map((e) => e.source_type))];
  const tickers = [...new Set(all.map((e) => e.ticker).filter((t): t is string => !!t))];
  return (
    <div className="space-y-3 p-3">
      <div className="flex flex-wrap gap-1.5 text-xs">
        <Filter label="All" on={!type && !ticker} onClick={() => { setType(null); setTicker(null); }} />
        {types.map((t) => <Filter key={t} label={TYPE[t]?.label ?? t} on={type === t} onClick={() => setType(type === t ? null : t)} />)}
        {tickers.map((t) => <Filter key={t} label={t} mono on={ticker === t} onClick={() => setTicker(ticker === t ? null : t)} />)}
      </div>
      {citedCards.filter(keep).map((e) => <SourceCard key={e.source_id} e={e} n={cited.get(e.source_id)} highlight={highlight === cited.get(e.source_id)} />)}
      {run.final && rest.length > 0 && <h3 className="pt-2 font-mono text-xs uppercase tracking-wide text-muted">Retrieved, not cited</h3>}
      {rest.filter(keep).map((e) => <SourceCard key={e.source_id} e={e} />)}
    </div>
  );
}

function Filter({ label, on, onClick, mono }: { label: string; on: boolean; onClick: () => void; mono?: boolean }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={on} className={`rounded border px-2 py-0.5 ${mono ? "font-mono" : ""} ${on ? "border-accent text-foreground" : "border-border text-muted hover:text-foreground"}`}>
      {label}
    </button>
  );
}

function SourceCard({ e, n, highlight }: { e: Evidence; n?: number; highlight?: boolean }) {
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    if (highlight) ref.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlight]);
  const t = TYPE[e.source_type] ?? { label: e.source_type, icon: FileText };
  const Icon = t.icon;
  const meta = [e.ticker, e.form_type, e.fiscal_year ? `FY${e.fiscal_year}` : null, e.section, e.filed_or_published?.slice(0, 10)].filter(Boolean);
  return (
    <article ref={ref} className={`fade-in rounded-md border bg-surface p-2.5 text-sm ${highlight ? "border-link ring-1 ring-link" : "border-border"}`}>
      <div className="mb-1 flex items-center gap-2 font-mono text-xs text-muted">
        {n !== undefined && <span className="rounded bg-surface-2 px-1 text-link">[{n}]</span>}
        <Icon className="size-3.5" aria-hidden />
        <span>{t.label}</span>
        <span className="truncate">{meta.join(" · ")}</span>
      </div>
      <p className="font-medium">{e.title}</p>
      <p className="mt-1 line-clamp-3 text-muted">{e.snippet}</p>
      {e.url && (
        <a href={e.url} target="_blank" rel="noreferrer" className="mt-1 inline-flex items-center gap-1 text-xs text-link hover:underline">
          Open source <ExternalLink className="size-3" aria-hidden />
        </a>
      )}
    </article>
  );
}
