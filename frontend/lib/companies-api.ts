import { API_URL, toError } from "./api";
import type { components } from "./generated/api";

export type CompanySummary = components["schemas"]["CompanySummary"];

export function getCompaniesApiUrl(
  internalApiUrl: string | undefined = process.env.INTERNAL_API_URL,
  publicApiUrl = API_URL,
): string {
  const baseUrl = internalApiUrl?.trim() || publicApiUrl;
  return baseUrl.replace(/\/+$/, "");
}

export async function listCompanies(): Promise<CompanySummary[]> {
  const response = await fetch(`${getCompaniesApiUrl()}/api/companies`, { cache: "no-store" });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as CompanySummary[];
}
