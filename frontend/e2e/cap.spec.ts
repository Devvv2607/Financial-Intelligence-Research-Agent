import { expect, test } from "@playwright/test";

import { ask, waitForRun } from "./fixtures/api";

test("the fourth live question of the day is refused with the recordings offered", async ({ page }) => {
  await page.goto("/");
  for (let i = 0; i < 3; i++) {
    await ask(page, "Should I buy Tesla?");
    await waitForRun(page);
    await page.getByRole("button", { name: "New research" }).click();
    await page.waitForURL((url) => !url.search.includes("thread="));
  }
  await ask(page, "Should I buy Tesla?");
  const notice = page.getByRole("alert", { name: "Live run limit" });
  await expect(notice).toContainText("live questions");
  await expect(notice).toContainText("00:00 UTC");
  await notice.getByRole("button", { name: /Watch a recording: Should I buy Tesla\?/ }).click();
  await page.waitForURL(/replay=tesla-advice/);
  await expect(page.getByRole("region", { name: "Recorded run" })).toContainText("not live");
  await waitForRun(page);
});
