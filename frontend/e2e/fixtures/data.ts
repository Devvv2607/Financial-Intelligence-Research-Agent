import { expect, type Page } from "@playwright/test";

export async function openCompanies(page: Page): Promise<void> {
  await page.goto("/companies");
  await expect(page.getByRole("heading", { name: "Companies" })).toBeVisible();
}

export async function openInsights(page: Page): Promise<void> {
  await page.goto("/insights");
  await expect(page.getByRole("heading", { name: "Insights" })).toBeVisible();
}
