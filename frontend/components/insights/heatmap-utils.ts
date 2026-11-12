import type { RiskHeatmap } from "@/lib/insights-api";

export type HeatmapMode = "count" | "share";
export type HeatmapCell = RiskHeatmap["cells"][number];

export function normalizeHeatmap(data: RiskHeatmap): {
  sectors: string[];
  categories: string[];
  cells: Map<string, HeatmapCell>;
  maxCount: number;
} {
  const sectors = [...new Set(data.sectors)].sort();
  const categories = [...new Set(data.categories)].sort();
  const cells = new Map(data.cells.map((cell) => [`${cell.sector}:${cell.category}`, cell]));
  const maxCount = Math.max(0, ...data.cells.map((cell) => cell.count));
  return { sectors, categories, cells, maxCount };
}

export function cellValue(cell: HeatmapCell | undefined, mode: HeatmapMode, sectorTotal: number): number {
  if (!cell) return 0;
  return mode === "count" ? cell.count : sectorTotal === 0 ? 0 : (cell.count / sectorTotal) * 100;
}

export function cellLabel(cell: HeatmapCell | undefined, mode: HeatmapMode, sectorTotal: number): string {
  if (!cell) return "No data";
  const value = cellValue(cell, mode, sectorTotal);
  const formatted = mode === "count" ? `${cell.count}` : `${value.toFixed(1)}%`;
  return `${formatted} risk factors across ${cell.companies} ${cell.companies === 1 ? "company" : "companies"}`;
}
