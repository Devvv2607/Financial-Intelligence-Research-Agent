import { expect, test } from "@playwright/test";

import { ask, waitForRun } from "./fixtures/api";

test("threads are private to the browser that created them", async ({ browser }) => {
  const alice = await browser.newPage(); // each newPage() is its own context: own localStorage
  await alice.goto("/");
  await ask(alice, "Should I buy Tesla?");
  await waitForRun(alice);
  await alice.waitForURL(/thread=/);
  const threadUrl = alice.url();
  const sidebar = alice.getByRole("navigation", { name: "Research threads" });
  await expect(sidebar.getByText("Should I buy Tesla?")).toBeVisible();

  const bob = await browser.newPage();
  await bob.goto("/");
  const bobSidebar = bob.getByRole("navigation", { name: "Research threads" });
  await expect(bobSidebar.getByText("No research yet.")).toBeVisible();
  await expect(bobSidebar.getByText("Should I buy Tesla?")).toHaveCount(0);

  // Opening someone else's thread by its URL shows nothing of it.
  await bob.goto(threadUrl);
  await expect(bob.getByText("Couldn't load this thread.")).toBeVisible();
  await expect(bob.getByText(/investment advice/i)).toHaveCount(0);
});
