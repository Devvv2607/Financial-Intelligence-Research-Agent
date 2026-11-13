import type { Page } from "@playwright/test";

export async function waitForRun(page: Page): Promise<void> {
  await page.locator('[aria-live="polite"]').getByText(/^Done ·/).waitFor({ timeout: 30_000 });
}

export async function ask(page: Page, question: string): Promise<void> {
  await page.getByRole("textbox", { name: "Research question" }).fill(question);
  await page.getByRole("button", { name: "Ask" }).click();
}
