import { expect, test } from "@playwright/test";

import { ask, waitForRun } from "./fixtures/api";

test.describe("research workspace", () => {
  test("runs the parallel comparison, verifies citations, and marks one claim unverified", async ({ page }) => {
    await page.goto("/");
    await ask(
      page,
      "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks. Does recent news support any of them?",
    );
    await waitForRun(page);
    await expect(page.getByText("unverified", { exact: true }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Source 1" }).first()).toBeVisible();
    await page.getByRole("button", { name: "Source 1" }).first().click();
    await expect(page.getByRole("tab", { name: /sources/i })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText(/scripted/).first()).toBeVisible();
  });

  test("clarifies the bank question and resumes for JPMorgan", async ({ page }) => {
    await page.goto("/");
    await ask(page, "How risky is the bank?");
    await expect(page.getByRole("region", { name: "Clarification needed" })).toBeVisible();
    await page.getByRole("button", { name: "JPMorgan Chase (JPM)" }).click();
    await waitForRun(page);
    await expect(page.getByText("JPMorgan Chase", { exact: false }).first()).toBeVisible();
  });

  test("refuses investment advice", async ({ page }) => {
    await page.goto("/");
    await ask(page, "Should I buy Tesla?");
    await waitForRun(page);
    await expect(page.getByText(/investment advice/i).first()).toBeVisible();
  });

  test("reloads during a run and resumes the same thread", async ({ page }) => {
    await page.goto("/");
    await ask(page, "Compare the top risk factors NVIDIA and AMD disclosed in their latest 10-Ks.");
    await page.waitForURL(/thread=/);
    await page.reload();
    await waitForRun(page);
    await expect(page.getByText("NVIDIA", { exact: false }).last()).toBeVisible();
  });
});
