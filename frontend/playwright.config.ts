import { defineConfig } from "@playwright/test";
import path from "node:path";

const databasePath = path.resolve("test-results", `pid-agent-e2e-${process.pid}.db`);
const diagnosticsPath = path.resolve("test-results", `pid-agent-e2e-${process.pid}.diagnostics.jsonl`);

// Ports are overridable so the suite can run on a machine where the default backend
// port is already held by another checkout; CI uses the defaults.
const apiPort = Number(process.env.PID_AGENT_E2E_API_PORT ?? 8000);
const previewPort = Number(process.env.PID_AGENT_E2E_PREVIEW_PORT ?? 4173);
const apiOrigin = `http://127.0.0.1:${apiPort}`;
const previewOrigin = `http://127.0.0.1:${previewPort}`;
// Keep the fixtures' direct API calls pointed at the same backend as the proxy.
process.env.PID_AGENT_E2E_API_ROOT ??= `${apiOrigin}/api/v2`;
process.env.PID_AGENT_API_TARGET ??= apiOrigin;

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "security.shared.spec.ts",
  outputDir: "test-results/playwright",
  snapshotPathTemplate: "{testDir}/{testFilePath}-snapshots/{arg}{ext}",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: {
    timeout: 8_000,
    toHaveScreenshot: {
      animations: "disabled",
      caret: "hide",
      maxDiffPixelRatio: 0.015,
    },
  },
  reporter: process.env.CI
    ? [["line"], ["html", { outputFolder: "test-results/playwright-report", open: "never" }]]
    : [["list"], ["html", { outputFolder: "test-results/playwright-report", open: "never" }]],
  use: {
    baseURL: previewOrigin,
    viewport: { width: 1440, height: 960 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    colorScheme: "light",
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: process.env.PID_AGENT_E2E_NO_VIDEO ? "off" : "retain-on-failure",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : undefined,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
  webServer: [
    {
      command: `python -m uvicorn agentcad.main:app --host 127.0.0.1 --port ${apiPort}`,
      cwd: "..",
      env: {
        ...process.env,
        PYTHONPATH: path.resolve("../backend"),
        PID_AGENT_DATABASE_PATH: databasePath,
        PID_AGENT_DIAGNOSTICS_PATH: diagnosticsPath,
        PID_AGENT_FRONTEND_DIST: path.resolve("dist"),
        PID_AGENT_CORS_ORIGINS: previewOrigin,
        PID_AGENT_AGENT_TIMEOUT_SECONDS: "180",
      },
      url: `${apiOrigin}/health`,
      timeout: 30_000,
      reuseExistingServer: false,
    },
    {
      command: `npm run preview -- --host 127.0.0.1 --port ${previewPort}`,
      cwd: ".",
      url: previewOrigin,
      timeout: 30_000,
      reuseExistingServer: false,
    },
  ],
});
