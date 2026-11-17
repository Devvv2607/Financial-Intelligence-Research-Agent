import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

const apiUrl = "http://localhost:8011";
const webUrl = "http://localhost:3005";
const e2eEnv = {
  AGENT_RUNTIME: "scripted",
  API_RUNS_PER_MINUTE: "100",
  // Every spec shares 127.0.0.1, so only the per-visitor cap (default 3) applies here.
  LIVE_RUNS_PER_IP_PER_DAY: "1000",
  APP_ENV: "test",
  CORS_ORIGINS: JSON.stringify(["http://127.0.0.1:3005", "http://localhost:3005"]),
  GROQ_API_KEY: "scripted-e2e",
  NEXT_PUBLIC_API_URL: apiUrl,
  // Recordings of the scripted runtime (labeled "scripted"), never production replays.
  REPLAYS_DIR: path.join(__dirname, "fixtures", "replays"),
};

export default defineConfig({
  testDir: ".",
  globalSetup: "./safety-check.ts",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: webUrl,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: [
    {
      command:
        "uv run uvicorn api.main:app --host 127.0.0.1 --port 8011 --loop core.aio:selector_loop",
      cwd: "..",
      env: e2eEnv,
      url: `${apiUrl}/ready`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "npm run dev -- --port 3005",
      cwd: ".",
      env: e2eEnv,
      url: webUrl,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
