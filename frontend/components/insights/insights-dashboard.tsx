import type { RiskHeatmap } from "@/lib/insights-api";

import { IngestionStats } from "./ingestion-stats";
import { RiskHeatmapGrid } from "./risk-heatmap";

export function InsightsDashboard({ data }: { data: RiskHeatmap }) {
  return (
    <div className="space-y-8">
      <RiskHeatmapGrid data={data} />
      <IngestionStats stats={data.stats} />
    </div>
  );
}
