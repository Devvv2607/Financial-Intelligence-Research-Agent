import type { components } from "./generated/api";

type MetricPoint = components["schemas"]["MetricPoint"];
type RiskCategory = components["schemas"]["RiskCategory"];

export function groupRiskCategories(categories: RiskCategory[]) {
  return [...categories].sort((a, b) => b.count - a.count || a.category.localeCompare(b.category));
}

export function formatMetricValue(value: number, unit: string): string {
  if (!Number.isFinite(value)) return "not reported";
  if (unit.toLowerCase().includes("usd") || unit.includes("$")) {
    const absolute = Math.abs(value);
    const suffix = absolute >= 1_000_000_000 ? "B" : absolute >= 1_000_000 ? "M" : "";
    const divisor = suffix === "B" ? 1_000_000_000 : suffix === "M" ? 1_000_000 : 1;
    return `$${(value / divisor).toFixed(suffix ? 1 : 0)}${suffix}`;
  }
  return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function calculateYoY(points: MetricPoint[]): number | null {
  const sorted = [...points].sort((a, b) => b.fiscal_year - a.fiscal_year);
  const latest = sorted[0]?.value;
  const prior = sorted[1]?.value;
  if (latest === undefined || prior === undefined || prior === 0) return null;
  return (latest - prior) / Math.abs(prior);
}

export function sparklinePath(
  points: MetricPoint[],
  width = 180,
  height = 40,
  padding = 3,
): string {
  if (points.length === 0) return "";
  const values = points.map((point) => point.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  return [...points]
    .sort((a, b) => a.fiscal_year - b.fiscal_year)
    .map((point, index, sorted) => {
      const x = padding + (index / Math.max(sorted.length - 1, 1)) * (width - padding * 2);
      const y = height - padding - ((point.value - min) / range) * (height - padding * 2);
      return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}
