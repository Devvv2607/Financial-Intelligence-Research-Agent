import { describe, expect, it } from "vitest";

import { cellLabel, cellValue, normalizeHeatmap } from "@/components/insights/heatmap-utils";
import type { RiskHeatmap } from "@/lib/insights-api";

const data: RiskHeatmap = {
  sectors: ["Technology", "Financials"],
  categories: ["Market", "Legal"],
  cells: [
    { sector: "Technology", category: "Market", count: 6, companies: 2 },
    { sector: "Technology", category: "Legal", count: 4, companies: 1 },
  ],
  stats: {
    companies: 2,
    filings: 2,
    risk_factors: 10,
    eight_k_events: 0,
    financial_facts: 0,
    news_articles: 0,
    chunks: null,
  },
};

describe("heatmap utilities", () => {
  it("normalizes axes and indexes cells", () => {
    const normalized = normalizeHeatmap(data);
    expect(normalized.sectors).toEqual(["Financials", "Technology"]);
    expect(normalized.categories).toEqual(["Legal", "Market"]);
    expect(normalized.cells.get("Technology:Market")?.count).toBe(6);
    expect(normalized.maxCount).toBe(6);
  });

  it("supports count and share values", () => {
    const cell = data.cells[0];
    expect(cellValue(cell, "count", 10)).toBe(6);
    expect(cellValue(cell, "share", 10)).toBe(60);
    expect(cellLabel(cell, "share", 10)).toContain("60.0%");
  });

  it("handles null chunks and empty heatmap data", () => {
    expect(data.stats.chunks).toBeNull();
    const empty = normalizeHeatmap({ ...data, sectors: [], categories: [], cells: [] });
    expect(empty.sectors).toEqual([]);
    expect(empty.categories).toEqual([]);
    expect(empty.maxCount).toBe(0);
  });
});
