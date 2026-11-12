import { API_URL, toError } from "./api";
import type { components } from "./generated/api";

export type RiskHeatmap = components["schemas"]["RiskHeatmap"];

export function getInsightsApiUrl(
  internalApiUrl: string | undefined = process.env.INTERNAL_API_URL,
  publicApiUrl = API_URL,
): string {
  const baseUrl = internalApiUrl?.trim() || publicApiUrl;
  return baseUrl.replace(/\/+$/, "");
}

export async function getRiskHeatmap(): Promise<RiskHeatmap> {
  const response = await fetch(`${getInsightsApiUrl()}/api/insights/risk-heatmap`, {
    cache: "no-store",
  });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as RiskHeatmap;
}
