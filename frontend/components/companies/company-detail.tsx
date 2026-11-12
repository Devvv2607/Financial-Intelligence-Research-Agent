"use client";

import {
  AlertTriangle,
  ArrowLeft,
  CalendarDays,
  CheckCircle2,
  ExternalLink,
  Flag,
  FileWarning,
} from "lucide-react";
import Link from "next/link";

import type { CompanyDetail } from "@/lib/company-detail-api";
import { groupRiskCategories } from "@/lib/company-detail-utils";

import { MetricSparkline } from "./metric-sparkline";

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="text-lg font-semibold">{children}</h2>;
}

export function CompanyDetailView({ company }: { company: CompanyDetail }) {
  const categories = groupRiskCategories(company.risk_breakdown);
  const maximumRiskCount = Math.max(...categories.map((category) => category.count), 1);
  const metrics = Object.entries(company.metrics);

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-8 p-4 md:p-6">
        <header className="border-b border-border pb-5">
          <Link href="/companies" className="inline-flex items-center gap-1 text-sm text-link hover:underline">
            <ArrowLeft className="size-4" aria-hidden />
            Back to companies
          </Link>
          <div className="mt-5 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted">{company.ticker}</p>
              <h1 className="mt-2 text-2xl font-semibold">{company.name}</h1>
              <p className="mt-1 text-sm text-muted">
                {company.sector ?? "Unclassified"} · FY end {company.fiscal_year_end ?? "not reported"}
              </p>
            </div>
            <p className="font-mono text-xs text-muted">CIK {company.cik}</p>
          </div>
        </header>

        <section className="space-y-4" aria-labelledby="sector-heading">
          <div className="flex items-center gap-2">
            <SectionTitle><span id="sector-heading">Sector classification</span></SectionTitle>
            {company.sector_conflict && (
              <span className="inline-flex items-center gap-1 text-xs text-accent">
                <AlertTriangle className="size-3.5" aria-hidden />
                Conflict
              </span>
            )}
          </div>
          <div className="grid gap-px border border-border bg-border md:grid-cols-2">
            <div className="bg-surface p-4">
              <p className="font-mono text-xs uppercase tracking-wide text-muted">SIC-based</p>
              <p className="mt-2 font-medium">{company.sic_sector ?? "Not reported"}</p>
              <p className="mt-1 text-sm text-muted">
                {company.sic_description ?? (company.sic ? `SIC ${company.sic}` : "No SIC classification")}
              </p>
            </div>
            <div className="bg-surface p-4">
              <p className="font-mono text-xs uppercase tracking-wide text-muted">LLM-based</p>
              <p className="mt-2 font-medium">{company.llm_sector ?? "Not classified"}</p>
              <p className="mt-1 text-sm text-muted">{company.llm_sub_industry ?? "Sub-industry not reported"}</p>
              {company.llm_sector_rationale && <p className="mt-3 text-sm text-muted">{company.llm_sector_rationale}</p>}
            </div>
          </div>
        </section>

        <section className="space-y-4" aria-labelledby="risk-heading">
          <div className="flex items-center justify-between gap-3">
            <SectionTitle><span id="risk-heading">Risk factors</span></SectionTitle>
            <span className="font-mono text-xs text-muted">
              FY{company.risk_fiscal_year ?? "—"} · {categories.reduce((sum, category) => sum + category.count, 0)} total
            </span>
          </div>
          {categories.length === 0 ? (
            <div className="border border-border bg-surface p-6 text-sm text-muted">No risk factors reported.</div>
          ) : (
            <div className="space-y-3">
              {categories.map((category) => (
                <details key={category.category} className="border border-border bg-surface" open>
                  <summary className="cursor-pointer list-none p-4">
                    <div className="flex items-center justify-between gap-4">
                      <span className="font-medium">{category.category}</span>
                      <span className="font-mono text-xs text-muted">{category.count}</span>
                    </div>
                    <div className="mt-3 h-1.5 bg-surface-2" aria-hidden>
                      <div className="h-full bg-accent" style={{ width: `${(category.count / maximumRiskCount) * 100}%` }} />
                    </div>
                  </summary>
                  <div className="border-t border-border px-4 py-2">
                    {category.risk_factors.map((risk) => (
                      <div key={risk.id} className="border-b border-border py-3 last:border-b-0">
                        <div className="flex items-start gap-2">
                          {risk.needs_review ? (
                            <span className="inline-flex shrink-0 items-center gap-1 text-xs text-accent" title="Needs review">
                              <Flag className="mt-0.5 size-3.5" aria-hidden />
                              <span>Review</span>
                            </span>
                          ) : (
                            <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-ok" aria-label="Reviewed" />
                          )}
                          <div>
                            <p className="text-sm">{risk.heading}</p>
                            {risk.summary && <p className="mt-1 text-xs text-muted">{risk.summary}</p>}
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </details>
              ))}
            </div>
          )}
        </section>

        <section className="space-y-4" aria-labelledby="events-heading">
          <SectionTitle><span id="events-heading">Recent 8-K events</span></SectionTitle>
          {company.recent_8ks.length === 0 ? (
            <div className="border border-border bg-surface p-6 text-sm text-muted">No recent 8-K events reported.</div>
          ) : (
            <ol className="border-l border-border">
              {company.recent_8ks.map((event) => (
                <li key={event.accession_no} className="relative ml-4 border-b border-border py-4 pl-4 last:border-b-0">
                  <span className="absolute -left-[5px] top-5 size-2 rounded-full bg-accent" aria-hidden />
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                    <span className="inline-flex items-center gap-1 font-mono"><CalendarDays className="size-3.5" aria-hidden />{event.filing_date}</span>
                    <span className="font-mono">{event.items.join(", ") || "Item not reported"}</span>
                  </div>
                  <p className="mt-2 text-sm text-muted">Description not exposed by API</p>
                  <a href={event.url} target="_blank" rel="noreferrer" className="mt-2 inline-flex items-center gap-1 text-xs text-link hover:underline">
                    SEC filing <ExternalLink className="size-3.5" aria-hidden />
                  </a>
                </li>
              ))}
            </ol>
          )}
        </section>

        <section className="space-y-4" aria-labelledby="metrics-heading">
          <SectionTitle><span id="metrics-heading">Key metrics</span></SectionTitle>
          {metrics.length === 0 ? (
            <div className="border border-border bg-surface p-6 text-sm text-muted">No financial metrics reported.</div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {metrics.map(([name, points]) => <MetricSparkline key={name} name={name} points={points} />)}
            </div>
          )}
          <p className="inline-flex items-center gap-1 text-xs text-muted">
            <FileWarning className="size-3.5" aria-hidden />
            Missing metrics are shown as not reported; they are not treated as zero.
          </p>
        </section>
      </div>
    </main>
  );
}
