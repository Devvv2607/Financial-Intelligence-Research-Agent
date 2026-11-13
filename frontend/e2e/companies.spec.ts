import { expect, test } from "@playwright/test";

import { openCompanies } from "./fixtures/data";

test.describe("company pages", () => {
  test("filters and searches the company directory", async ({ page }) => {
    await openCompanies(page);
    await expect(page.getByRole("link", { name: "NVDA" })).toBeVisible();
    await page.getByRole("searchbox", { name: "Search companies" }).fill("NVIDIA");
    await expect(page.getByRole("link", { name: "NVDA" })).toBeVisible();
    await expect(page.getByRole("link", { name: "AAPL" })).not.toBeVisible();
    await page.getByRole("button", { name: /Information Technology/ }).first().click();
    await expect(page.getByText(/shown/)).toBeVisible();
  });

  test("shows the NVDA detail and a proper unknown-ticker state", async ({ page }) => {
    await page.goto("/companies/NVDA");
    await expect(page.getByRole("heading", { name: /NVIDIA/i })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Sector classification" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Risk factors" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Recent 8-K events" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Key metrics" })).toBeVisible();

    await page.goto("/companies/XYZ");
    await expect(page.getByText(/not found|could not find|404/i).first()).toBeVisible();
  });
});
