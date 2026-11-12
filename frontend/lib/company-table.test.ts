import { describe, expect, it } from "vitest";

import { filterCompanies } from "@/components/companies/company-table";
import type { CompanySummary } from "@/lib/companies-api";

const companies: CompanySummary[] = [
  {
    ticker: "AMD",
    name: "Advanced Micro Devices, Inc.",
    sector: "Information Technology",
    sic_sector: "Information Technology",
    llm_sector: "Information Technology",
    sector_conflict: false,
    fiscal_year_end: "12-31",
    latest_10k_year: 2025,
    risk_factor_count: 44,
  },
  {
    ticker: "JPM",
    name: "JPMorgan Chase & Co.",
    sector: "Financials",
    sic_sector: "Financials",
    llm_sector: "Financials",
    sector_conflict: false,
    fiscal_year_end: "12-31",
    latest_10k_year: 2025,
    risk_factor_count: 41,
  },
];

describe("filterCompanies", () => {
  it("matches ticker and company name case-insensitively", () => {
    expect(filterCompanies(companies, "amd", null).map((company) => company.ticker)).toEqual(["AMD"]);
    expect(filterCompanies(companies, "chase", null).map((company) => company.ticker)).toEqual(["JPM"]);
  });

  it("applies a sector filter alongside the search", () => {
    expect(filterCompanies(companies, "", "Financials").map((company) => company.ticker)).toEqual(["JPM"]);
    expect(filterCompanies(companies, "amd", "Financials")).toEqual([]);
  });

  it("returns all companies for blank filters", () => {
    expect(filterCompanies(companies, "  ", null)).toEqual(companies);
  });
});
