import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import { CompanyNotFoundError, getCompanyDetail } from "@/lib/company-detail-api";

const response = (status: number, body: unknown) =>
  new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

describe("getCompanyDetail", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("maps an API 404 to CompanyNotFoundError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(404, { detail: "Not found" })));

    await expect(getCompanyDetail("AAPL")).rejects.toBeInstanceOf(CompanyNotFoundError);
  });

  it("keeps an API 500 as a normal ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(500, { detail: "Database unavailable" })));

    const error = await getCompanyDetail("AAPL").catch((value: unknown) => value);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(CompanyNotFoundError);
    expect((error as ApiError).status).toBe(500);
  });

  it("keeps malformed JSON as a parse error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, "{not-json")));

    const error = await getCompanyDetail("AAPL").catch((value: unknown) => value);
    expect(error).toBeInstanceOf(SyntaxError);
    expect(error).not.toBeInstanceOf(CompanyNotFoundError);
  });

  it("trims, uppercases, and URL-encodes the ticker", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(200, { ticker: "A/B" }));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("INTERNAL_API_URL", "http://api:8000///");

    await getCompanyDetail("  a/b  ");

    expect(fetchMock).toHaveBeenCalledWith("http://api:8000/api/companies/A%2FB", {
      cache: "no-store",
    });
  });
});
