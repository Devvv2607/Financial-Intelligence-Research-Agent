import type { components } from "@/lib/generated/api";
import { calculateYoY, formatMetricValue, sparklinePath } from "@/lib/company-detail-utils";

type MetricPoint = components["schemas"]["MetricPoint"];

export function MetricSparkline({ name, points }: { name: string; points: MetricPoint[] }) {
  if (points.length === 0) {
    return (
      <article className="border border-border bg-surface p-4">
        <h3 className="text-sm font-medium">{name}</h3>
        <p className="mt-4 text-sm text-muted">Not reported</p>
      </article>
    );
  }

  const sorted = [...points].sort((a, b) => b.fiscal_year - a.fiscal_year);
  const latest = sorted[0];
  const yoy = calculateYoY(points);
  const yoyLabel = yoy === null ? "YoY unavailable" : `${yoy >= 0 ? "+" : ""}${(yoy * 100).toFixed(1)}% YoY`;

  return (
    <article className="border border-border bg-surface p-4">
      <div className="flex items-start justify-between gap-3">
        <h3 className="text-sm font-medium">{name}</h3>
        <span className="font-mono text-xs text-muted">{latest.fiscal_year}</span>
      </div>
      <div className="mt-3 flex items-end justify-between gap-4">
        <div>
          <p className="font-mono text-lg tabular-nums">{formatMetricValue(latest.value, latest.unit)}</p>
          <p className="mt-1 text-xs text-muted">{yoyLabel}</p>
        </div>
        <svg
          viewBox="0 0 180 40"
          className="h-10 w-36 text-accent"
          role="img"
          aria-label={`${name} trend over fiscal years`}
        >
          <path d={sparklinePath(points)} fill="none" stroke="currentColor" strokeWidth="2" />
        </svg>
      </div>
    </article>
  );
}
