import { expect, test } from "@playwright/test";

test.setTimeout(60_000);

test("a slow API shows the waking screen, then the app once it answers", async ({ page }) => {
  // The first probes hang past their timeout, as on a cold start (dev StrictMode starts
  // and aborts one extra probe).
  let held = 3;
  await page.route("**/health", async (route) => {
    if (held-- > 0) {
      await new Promise((r) => setTimeout(r, 6_000));
      return route.abort().catch(() => undefined);
    }
    return route.continue();
  });
  await page.goto("/");
  const waking = page.getByRole("status", { name: "Waking the server" });
  await expect(waking).toContainText("Waking the free-tier server, about 30–60s", { timeout: 20_000 });
  await expect(waking).toBeHidden({ timeout: 30_000 });
  await expect(page.getByRole("textbox", { name: "Research question" })).toBeVisible();
});
