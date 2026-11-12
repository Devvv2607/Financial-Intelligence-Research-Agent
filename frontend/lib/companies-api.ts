import { API_URL, toError } from "./api";
import type { components } from "./generated/api";

export type CompanySummary = components["schemas"]["CompanySummary"];

export async function listCompanies(): Promise<CompanySummary[]> {
  const response = await fetch(`${API_URL}/api/companies`, { cache: "no-store" });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as CompanySummary[];
}
