import { describe, expect, it } from "vitest";

import { calculateYoY, formatMetricValue, groupRiskCategories, sparklinePath } from "@/lib/company-detail-utils";

const point = (fiscal_year: number, value: number, unit = "USD") => ({
  fiscal_year,
  period_end: `${fiscal_year}-12-31`,
  unit,
  value,
});

describe("company detail utilities", () => {
  it("generates a path for sorted sparkline points and an empty path for missing data", () => {
    expect(sparklinePath([point(2024, 20), point(2022, 10), point(2023, 15)])).toMatch(/^M/);
    expect(sparklinePath([])).toBe("");
  });

  it("formats USD in billions and millions", () => {
    expect(formatMetricValue(2_500_000_000, "USD")).toBe("$2.5B");
    expect(formatMetricValue(12_000_000, "USD")).toBe("$12.0M");
    expect(formatMetricValue(42, "shares")).toBe("42");
  });

  it("calculates year-over-year change and handles missing/prior zero", () => {
    expect(calculateYoY([point(2023, 100), point(2024, 125)])).toBe(0.25);
    expect(calculateYoY([point(2024, 125)])).toBeNull();
    expect(calculateYoY([point(2024, 125), point(2023, 0)])).toBeNull();
  });

  it("groups risk categories by descending count", () => {
    const grouped = groupRiskCategories([
      { category: "ip", count: 2, risk_factors: [] },
      { category: "cybersecurity", count: 5, risk_factors: [] },
    ]);
    expect(grouped.map((item) => item.category)).toEqual(["cybersecurity", "ip"]);
  });
});
