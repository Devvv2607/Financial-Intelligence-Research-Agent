import { InsightsDashboard } from "@/components/insights/insights-dashboard";
import { getRiskHeatmap } from "@/lib/insights-api";

export default async function InsightsPage() {
  const data = await getRiskHeatmap();
  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
        <header className="border-b border-border pb-5">
          <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted">Pipeline observatory</p>
          <h1 className="mt-2 text-2xl font-semibold">Insights</h1>
          <p className="mt-1 text-sm text-muted">Risk exposure and ingestion coverage from read-only pipeline data.</p>
        </header>
        <InsightsDashboard data={data} />
      </div>
    </main>
  );
}
