import { Archive, BarChart3, Database, FileText, Newspaper, Radio, Server } from "lucide-react";

import type { RiskHeatmap } from "@/lib/insights-api";

const statDefinitions = [
  ["companies", "Companies", Database],
  ["filings", "Filings", FileText],
  ["risk_factors", "Risk factors", BarChart3],
  ["eight_k_events", "8-K events", Radio],
  ["financial_facts", "Financial facts", Archive],
  ["news_articles", "News articles", Newspaper],
  ["chunks", "Chunks", Server],
] as const;

export function IngestionStats({ stats }: { stats: RiskHeatmap["stats"] }) {
  return (
    <section aria-labelledby="ingestion-stats-heading" className="space-y-4">
      <div>
        <h2 id="ingestion-stats-heading" className="text-lg font-semibold">Ingestion coverage</h2>
        <p className="mt-1 text-xs text-muted">Read-only counts from the filing and enrichment pipeline.</p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {statDefinitions.map(([key, label, Icon]) => {
          const value = stats[key];
          return (
            <div key={key} className="border border-border bg-surface p-4">
              <div className="flex items-center gap-2 text-xs text-muted">
                <Icon className="size-4" aria-hidden />
                <span>{label}</span>
              </div>
              <p className="mt-3 font-mono text-2xl tabular-nums">
                {value === null ? "unavailable" : value.toLocaleString()}
              </p>
              {value === null && <p className="mt-1 text-xs text-muted">Vector store unavailable</p>}
            </div>
          );
        })}
      </div>
    </section>
  );
}
