import { CompanyTable } from "@/components/companies/company-table";
import { listCompanies } from "@/lib/companies-api";

export default async function CompaniesPage({
  searchParams,
}: {
  searchParams: Promise<{ sector?: string }>;
}) {
  const companies = await listCompanies();
  const { sector } = await searchParams;
  const riskFactors = companies.reduce((total, company) => total + company.risk_factor_count, 0);

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
        <header className="flex flex-col gap-2 border-b border-border pb-5 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="font-mono text-xs uppercase tracking-[0.18em] text-muted">Coverage directory</p>
            <h1 className="mt-2 text-2xl font-semibold">Companies</h1>
            <p className="mt-1 text-sm text-muted">
              {companies.length} {companies.length === 1 ? "company" : "companies"} · {riskFactors.toLocaleString()} risk factors
            </p>
          </div>
          <p className="font-mono text-xs text-muted">Read-only pipeline data</p>
        </header>
        <CompanyTable companies={companies} initialSector={sector ?? null} />
      </div>
    </main>
  );
}
