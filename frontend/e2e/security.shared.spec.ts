import { expect, test, type APIRequestContext } from "@playwright/test";

const TOKEN = process.env.PID_AGENT_E2E_SHARED_TOKEN ?? "pid-agent-shared-e2e-token";
// The config's port override (PID_AGENT_E2E_API_PORT) has to reach the direct API calls too, or
// the suite talks to a different server than the browser does.
const API = `http://127.0.0.1:${process.env.PID_AGENT_E2E_API_PORT ?? 8000}/api/v2`;
const authorization = { Authorization: `Bearer ${TOKEN}` };

async function resetDocuments(request: APIRequestContext) {
  const listed = await request.get(`${API}/documents`, { headers: authorization });
  expect(listed.ok()).toBeTruthy();
  for (const document of (await listed.json()) as Array<{ id: string; revision: number }>) {
    const removed = await request.delete(
      `${API}/documents/${document.id}?expected_revision=${encodeURIComponent(document.revision)}`,
      { headers: authorization },
    );
    expect(removed.ok(), await removed.text()).toBeTruthy();
  }
}

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

test("shared deployment requires a token and keeps it tab-session scoped", async ({ page, request }) => {
  const unauthenticated = await request.get(`${API}/documents`);
  expect(unauthenticated.status()).toBe(401);
  expect((await unauthenticated.json()).detail.error).toBe("authentication_required");

  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("需要服务访问令牌");
  await page.locator(".service-access-settings").getByText("共享部署访问令牌").click();

  await page.getByTestId("service-token-input").fill("wrong-token");
  await page.getByTestId("service-token-apply").click();
  await expect(page.getByRole("alert")).toContainText("服务访问令牌错误");

  await page.getByTestId("service-token-input").fill(TOKEN);
  await page.getByTestId("service-token-apply").click();
  await expect(page.getByTestId("project-summary")).toContainText("0 个文档");

  expect(page.url()).not.toContain(TOKEN);
  expect(await page.evaluate(() => window.localStorage.getItem("pid-agent-service-token"))).toBeNull();
  expect(await page.evaluate(() => window.sessionStorage.getItem("pid-agent-service-token"))).toBe(TOKEN);

  await page.reload();
  await expect(page.getByTestId("project-summary")).toContainText("0 个文档");
  expect(await page.getByTestId("service-token-input").inputValue()).toBe(TOKEN);
  expect(page.url()).not.toContain(TOKEN);
});

test("shared provider policy blocks localhost, permits an allowlisted target, and preserves revision", async ({ page, request }) => {
  const created = await request.post(`${API}/documents`, {
    headers: authorization,
    data: { name: "Shared security E2E" },
  });
  expect(created.ok()).toBeTruthy();
  const document = (await created.json()) as { id: string; revision: number };

  await page.goto("/");
  await page.locator(".service-access-settings").getByText("共享部署访问令牌").click();
  await page.getByTestId("service-token-input").fill(TOKEN);
  await page.getByTestId("service-token-apply").click();
  await page.locator(`.document-open[data-document-id="${document.id}"]`).click();
  await page.waitForFunction(() => Boolean(window.__PID_AGENT_E2E__?.snapshot().document));

  await page.getByRole("tab", { name: "Agent" }).click();
  await page.locator(".agent-provider-settings").getByText(/模型服务与高级设置/).click();
  const baseUrl = page.getByRole("textbox", { name: "Base URL" });
  const model = page.getByRole("textbox", { name: /Model name/ });

  await baseUrl.fill("http://localhost:8999/v1");
  await model.fill("test-model");
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.locator(".provider-test-error").last()).toContainText("网络安全策略阻止");
  await expect(page.locator(".provider-test-error").last()).toContainText("loopback");

  await baseUrl.fill("http://127.0.0.1:8999/v1");
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.locator(".provider-test-success")).toContainText("连接成功");
  await expect(page.locator(".provider-test-success")).toContainText("test-model");

  await page.getByRole("combobox", { name: "服务预设" }).selectOption("openai-compatible");
  await expect(baseUrl).toHaveValue("https://api.openai.com/v1");
  await expect(model).toHaveValue("");

  const after = await request.get(`${API}/documents/${document.id}`, { headers: authorization });
  expect(after.ok()).toBeTruthy();
  expect((await after.json()).revision).toBe(document.revision);
});
