import { notFound } from "next/navigation";

import { CompanyDetailView } from "@/components/companies/company-detail";
import { CompanyNotFoundError, getCompanyDetail } from "@/lib/company-detail-api";

export default async function CompanyDetailPage({ params }: { params: Promise<{ ticker: string }> }) {
  const { ticker } = await params;
  const company = await loadCompany(ticker);
  return <CompanyDetailView company={company} />;
}

async function loadCompany(ticker: string) {
  try {
    return await getCompanyDetail(ticker);
  } catch (error) {
    if (error instanceof CompanyNotFoundError) notFound();
    console.error("Failed to load company detail", { ticker, error });
    throw error;
  }
}
