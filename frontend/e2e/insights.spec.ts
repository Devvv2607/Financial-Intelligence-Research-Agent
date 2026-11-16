import { expect, test } from "@playwright/test";

import { openInsights } from "./fixtures/data";

test("toggles the risk heatmap between count and share", async ({ page }) => {
  await openInsights(page);
  const count = page.getByRole("button", { name: "count", exact: true });
  const share = page.getByRole("button", { name: "share", exact: true });
  await expect(count).toHaveAttribute("aria-pressed", "true");
  await share.click();
  await expect(share).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("%").first()).toBeVisible();
  await count.click();
  await expect(count).toHaveAttribute("aria-pressed", "true");
});
