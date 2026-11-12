"use client";

import { AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, Search } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import type { CompanySummary } from "@/lib/companies-api";

import { SectorChips } from "./sector-chips";

type SortKey = "ticker" | "name" | "sector" | "latest_10k_year" | "risk_factor_count";
type SortDirection = "asc" | "desc";

export function filterCompanies(
  companies: CompanySummary[],
  query: string,
  sector: string | null,
): CompanySummary[] {
  const normalized = query.trim().toLowerCase();
  return companies.filter((company) => {
    const matchesQuery =
      !normalized ||
      company.ticker.toLowerCase().includes(normalized) ||
      company.name.toLowerCase().includes(normalized);
    return matchesQuery && (sector === null || company.sector === sector);
  });
}

function compareValues(a: CompanySummary, b: CompanySummary, key: SortKey): number {
  const left = a[key] ?? "";
  const right = b[key] ?? "";
  if (typeof left === "number" && typeof right === "number") return left - right;
  return String(left).localeCompare(String(right));
}

function ConflictBadge({ company }: { company: CompanySummary }) {
  if (!company.sector_conflict) return null;
  const detail = [company.sic_sector, company.llm_sector].filter(Boolean).join(" vs ");
  return (
    <span
      className="inline-flex items-center gap-1 text-xs text-accent"
      title={`Sector classifications: ${detail || "different classifications"}`}
    >
      <AlertTriangle className="size-3.5" aria-hidden />
      <span>Conflict</span>
    </span>
  );
}

function SortButton({
  label,
  sortKey,
  activeKey,
  direction,
  onSort,
}: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey;
  direction: SortDirection;
  onSort: (key: SortKey) => void;
}) {
  const active = sortKey === activeKey;
  const Icon = active ? (direction === "asc" ? ArrowUp : ArrowDown) : ArrowUpDown;
  return (
    <button
      type="button"
      onClick={() => onSort(sortKey)}
      className="inline-flex items-center gap-1 text-left hover:text-foreground"
      aria-label={`Sort by ${label}`}
    >
      {label}
      <Icon className="size-3.5" aria-hidden />
    </button>
  );
}

export function CompanyTable({
  companies,
  initialSector = null,
}: {
  companies: CompanySummary[];
  initialSector?: string | null;
}) {
  const [query, setQuery] = useState("");
  const [sector, setSector] = useState<string | null>(initialSector);
  const [sortKey, setSortKey] = useState<SortKey>("ticker");
  const [direction, setDirection] = useState<SortDirection>("asc");
  const sectors = useMemo(
    () =>
      [...new Set(companies.map((company) => company.sector).filter((value): value is string => !!value))].sort(),
    [companies],
  );
  const filtered = useMemo(() => {
    const matches = filterCompanies(companies, query, sector);
    return [...matches].sort((a, b) => {
      const result = compareValues(a, b, sortKey);
      return direction === "asc" ? result : -result;
    });
  }, [companies, direction, query, sector, sortKey]);

  const sort = (key: SortKey) => {
    if (key === sortKey) {
      setDirection((current) => (current === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setDirection("asc");
    }
  };

  return (
    <section className="space-y-4" aria-label="Company directory">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
        <label className="relative block max-w-md flex-1">
          <span className="sr-only">Search companies</span>
          <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted" aria-hidden />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search ticker or company name"
            className="w-full rounded-md border border-border bg-surface py-2 pl-9 pr-3 text-sm placeholder:text-muted focus:border-accent focus:outline-none"
          />
        </label>
        <span className="font-mono text-xs text-muted">
          {filtered.length} of {companies.length} shown
        </span>
      </div>

      <SectorChips sectors={sectors} selected={sector} onSelect={setSector} />

      {filtered.length === 0 ? (
        <div className="border border-border bg-surface p-8 text-center">
          <p className="text-sm">No companies match your filters.</p>
          <p className="mt-1 text-xs text-muted">Try a different ticker, name, or sector.</p>
        </div>
      ) : (
        <div className="overflow-x-auto border border-border">
          <table className="w-full min-w-[720px] border-collapse text-left text-sm">
            <thead className="bg-surface-2 font-mono text-xs text-muted">
              <tr>
                <th className="border-b border-border px-3 py-2">
                  <SortButton label="Ticker" sortKey="ticker" activeKey={sortKey} direction={direction} onSort={sort} />
                </th>
                <th className="border-b border-border px-3 py-2">
                  <SortButton label="Name" sortKey="name" activeKey={sortKey} direction={direction} onSort={sort} />
                </th>
                <th className="border-b border-border px-3 py-2">
                  <SortButton label="Sector" sortKey="sector" activeKey={sortKey} direction={direction} onSort={sort} />
                </th>
                <th className="border-b border-border px-3 py-2">FY end</th>
                <th className="border-b border-border px-3 py-2">
                  <SortButton label="Latest 10-K" sortKey="latest_10k_year" activeKey={sortKey} direction={direction} onSort={sort} />
                </th>
                <th className="border-b border-border px-3 py-2">
                  <SortButton label="Risk factors" sortKey="risk_factor_count" activeKey={sortKey} direction={direction} onSort={sort} />
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((company) => (
                <tr key={company.ticker} className="border-b border-border last:border-b-0 hover:bg-surface">
                  <td className="px-3 py-3">
                    <Link href={`/companies/${company.ticker}`} className="font-mono text-link hover:underline">
                      {company.ticker}
                    </Link>
                  </td>
                  <td className="px-3 py-3">{company.name}</td>
                  <td className="px-3 py-3">
                    <div className="flex flex-col gap-1">
                      <span>{company.sector ?? "Unclassified"}</span>
                      <ConflictBadge company={company} />
                    </div>
                  </td>
                  <td className="px-3 py-3 font-mono text-muted">{company.fiscal_year_end ?? "—"}</td>
                  <td className="px-3 py-3 font-mono">{company.latest_10k_year ?? "—"}</td>
                  <td className="px-3 py-3 font-mono tabular-nums">{company.risk_factor_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
