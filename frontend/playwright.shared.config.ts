import { defineConfig } from "@playwright/test";
import path from "node:path";

const sharedToken = process.env.PID_AGENT_E2E_SHARED_TOKEN ?? "pid-agent-shared-e2e-token";
const databasePath = path.resolve("test-results", `pid-agent-shared-e2e-${process.pid}.db`);
const diagnosticsPath = path.resolve("test-results", `pid-agent-shared-e2e-${process.pid}.diagnostics.jsonl`);

// The same override the local config documents, for the same reason: the shared-mode suite has to
// be runnable on a machine where the default backend or preview port is already held by another
// checkout. CI uses the defaults, and the spec reads the API root from the same variables.
const apiPort = Number(process.env.PID_AGENT_E2E_API_PORT ?? 8000);
const previewPort = Number(process.env.PID_AGENT_E2E_PREVIEW_PORT ?? 4173);
const apiOrigin = `http://127.0.0.1:${apiPort}`;
const previewOrigin = `http://127.0.0.1:${previewPort}`;
// The preview server proxied /api to the default backend regardless of the port override, so the
// browser and the spec would have talked to a different server than the one this run started.
process.env.PID_AGENT_API_TARGET ??= apiOrigin;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "security.shared.spec.ts",
  outputDir: "test-results/shared-playwright",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 45_000,
  expect: { timeout: 8_000 },
  reporter: process.env.CI
    ? [["line"], ["html", { outputFolder: "test-results/shared-playwright-report", open: "never" }]]
    : [["list"]],
  use: {
    baseURL: previewOrigin,
    viewport: { width: 1440, height: 960 },
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    trace: "off",
    screenshot: "off",
    video: "off",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : undefined,
  },
  projects: [{ name: "chromium-shared", use: { browserName: "chromium" } }],
  webServer: [
    {
      command: "node e2e/mock-provider.mjs",
      cwd: ".",
      url: "http://127.0.0.1:8999/health",
      timeout: 30_000,
      reuseExistingServer: false,
    },
    {
      command: `python -m uvicorn agentcad.main:app --host 127.0.0.1 --port ${apiPort}`,
      cwd: "..",
      env: {
        ...process.env,
        PYTHONPATH: path.resolve("../backend"),
        PID_AGENT_DATABASE_PATH: databasePath,
        PID_AGENT_DIAGNOSTICS_PATH: diagnosticsPath,
        PID_AGENT_FRONTEND_DIST: path.resolve("dist"),
        PID_AGENT_DEPLOYMENT_MODE: "shared",
        PID_AGENT_API_TOKEN: sharedToken,
        PID_AGENT_E2E_SHARED_TOKEN: sharedToken,
        PID_AGENT_CORS_ORIGINS: previewOrigin,
        PID_AGENT_PROVIDER_ALLOW_HOSTS: "127.0.0.1",
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
