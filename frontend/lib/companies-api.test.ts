import { describe, expect, it } from "vitest";

import { getCompaniesApiUrl } from "@/lib/companies-api";

describe("getCompaniesApiUrl", () => {
  it("uses the internal API URL when it is set", () => {
    expect(getCompaniesApiUrl("http://api:8000", "http://localhost:8000")).toBe("http://api:8000");
  });

  it("falls back to the public API URL when the internal URL is unset", () => {
    expect(getCompaniesApiUrl(undefined, "http://localhost:8000")).toBe("http://localhost:8000");
    expect(getCompaniesApiUrl("  ", "http://localhost:8000")).toBe("http://localhost:8000");
  });

  it("removes trailing slashes from the selected URL", () => {
    expect(getCompaniesApiUrl("http://api:8000///", "http://localhost:8000/")).toBe(
      "http://api:8000",
    );
    expect(getCompaniesApiUrl(undefined, "http://localhost:8000///")).toBe("http://localhost:8000");
  });
});
