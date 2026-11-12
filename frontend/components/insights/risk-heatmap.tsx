"use client";

import { BarChart3, Info } from "lucide-react";
import { useMemo, useState } from "react";

import type { RiskHeatmap } from "@/lib/insights-api";

import { cellLabel, cellValue, normalizeHeatmap, type HeatmapMode } from "./heatmap-utils";

export function RiskHeatmapGrid({ data }: { data: RiskHeatmap }) {
  const [mode, setMode] = useState<HeatmapMode>("count");
  const normalized = useMemo(() => normalizeHeatmap(data), [data]);
  const sectorTotals = useMemo(() => {
    const totals = new Map<string, number>();
    for (const cell of data.cells) totals.set(cell.sector, (totals.get(cell.sector) ?? 0) + cell.count);
    return totals;
  }, [data.cells]);

  if (normalized.sectors.length === 0 || normalized.categories.length === 0) {
    return (
      <div className="border border-border bg-surface p-8 text-center">
        <BarChart3 className="mx-auto size-5 text-muted" aria-hidden />
        <p className="mt-3 text-sm">No risk heatmap data is available.</p>
        <p className="mt-1 text-xs text-muted">The heatmap will populate after filings are ingested.</p>
      </div>
    );
  }

  return (
    <section className="space-y-4" aria-labelledby="risk-heatmap-heading">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 id="risk-heatmap-heading" className="text-lg font-semibold">Risk factor heatmap</h2>
          <p className="mt-1 text-xs text-muted">Latest 10-K risk factors by sector and category.</p>
        </div>
        <div className="inline-flex border border-border bg-surface" aria-label="Heatmap display mode">
          {(["count", "share"] as const).map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => setMode(value)}
              aria-pressed={mode === value}
              className={`px-3 py-1.5 text-xs capitalize ${
                mode === value ? "bg-surface-2 text-foreground" : "text-muted hover:text-foreground"
              }`}
            >
              {value}
            </button>
          ))}
        </div>
      </div>
      <div className="overflow-x-auto border border-border">
        <table className="w-full min-w-[640px] border-collapse text-left text-sm">
          <thead className="bg-surface-2 font-mono text-xs text-muted">
            <tr>
              <th className="border-b border-border px-3 py-2">Sector</th>
              {normalized.categories.map((category) => (
                <th key={category} className="border-b border-border px-3 py-2">{category}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {normalized.sectors.map((sector) => (
              <tr key={sector} className="border-b border-border last:border-b-0">
                <th className="px-3 py-3 font-medium">{sector}</th>
                {normalized.categories.map((category) => {
                  const cell = normalized.cells.get(`${sector}:${category}`);
                  const total = sectorTotals.get(sector) ?? 0;
                  const value = cellValue(cell, mode, total);
                  const intensity = normalized.maxCount === 0 ? 0 : (cell?.count ?? 0) / normalized.maxCount;
                  return (
                    <td key={category} className="px-2 py-2">
                      <div
                        className="group relative min-h-12 border border-border px-2 py-2 font-mono tabular-nums"
                        style={{ backgroundColor: `color-mix(in srgb, var(--accent) ${Math.round(intensity * 36)}%, var(--surface))` }}
                        title={cellLabel(cell, mode, total)}
                      >
                        <span>{mode === "count" ? value : `${value.toFixed(1)}%`}</span>
                        <span className="sr-only">: {cellLabel(cell, mode, total)}</span>
                        {cell && (
                          <span className="ml-2 inline-flex items-center gap-1 text-[10px] text-muted">
                            <Info className="size-3" aria-hidden />
                            {cell.companies} {cell.companies === 1 ? "company" : "companies"}
                          </span>
                        )}
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-muted">
        <Info className="mr-1 inline size-3.5 align-[-2px]" aria-hidden />
        Hover or focus a cell for its risk-factor and represented-company counts.
      </p>
    </section>
  );
}
