import { API_URL, toError } from "./api";
import type { components } from "./generated/api";

export type CompanyDetail = components["schemas"]["CompanyDetail"];

function getApiBaseUrl(): string {
  return (process.env.INTERNAL_API_URL?.trim() || API_URL).replace(/\/+$/, "");
}

export class CompanyNotFoundError extends Error {
  constructor(ticker: string) {
    super(`Company not found: ${ticker}`);
    this.name = "CompanyNotFoundError";
  }
}

export async function getCompanyDetail(ticker: string): Promise<CompanyDetail> {
  const normalizedTicker = ticker.trim().toUpperCase();
  const response = await fetch(
    `${getApiBaseUrl()}/api/companies/${encodeURIComponent(normalizedTicker)}`,
    { cache: "no-store" },
  );
  if (response.status === 404) throw new CompanyNotFoundError(normalizedTicker);
  if (!response.ok) throw await toError(response);
  return (await response.json()) as CompanyDetail;
}
