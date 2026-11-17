import { expect, test } from "@playwright/test";

import { waitForRun } from "./fixtures/api";

test.describe("recorded runs", () => {
  test("a chip replays a recording, labeled as not live, without creating a thread", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Replay recorded run: Should I buy Tesla?" }).click();
    await page.waitForURL(/replay=tesla-advice/);
    const banner = page.getByRole("region", { name: "Recorded run" });
    await expect(banner).toContainText(/Recorded scripted test run · \d{1,2} \w{3} \d{4} · not live/);
    await waitForRun(page);
    await expect(page.getByText(/investment advice/i).first()).toBeVisible();
    await expect(page.getByRole("complementary").getByText(/· not live/)).toHaveCount(1); // run panel too
    const sidebar = page.getByRole("navigation", { name: "Research threads" });
    await expect(sidebar.getByText("No research yet.")).toBeVisible();
  });

  test("a recording with a clarification plays the recorded answer and finishes", async ({ page }) => {
    await page.goto("/?replay=bank-clarification");
    await expect(page.getByRole("region", { name: "Clarification needed" })).toBeVisible();
    await expect(page.getByText("(answer given in the recording)")).toBeVisible();
    await waitForRun(page);
    await expect(page.getByText("JPMorgan Chase", { exact: false }).first()).toBeVisible();
  });

  test("Run it live leaves the replay and starts a real (here: scripted) run", async ({ page }) => {
    await page.goto("/?replay=tesla-advice");
    await waitForRun(page);
    await page.getByRole("button", { name: "Run it live" }).click();
    await page.waitForURL(/thread=/);
    await expect(page.getByRole("region", { name: "Recorded run" })).toHaveCount(0);
    await waitForRun(page);
    const sidebar = page.getByRole("navigation", { name: "Research threads" });
    await expect(sidebar.getByText("Should I buy Tesla?")).toBeVisible();
  });
});
