import { expect, test } from "@playwright/test";
import { createDocument, openDocument, resetDocuments } from "./fixtures";

// The TypeSafe block is where a user configures the key that turns a sentence into judgments, so
// the browser suite has to cover three things that no unit test can: that the fields are addressable
// (an accessible name shared with another field breaks every selector by name), that the panel says
// where a key would come from, and that flipping the toggle really routes the request to the
// judgment endpoint instead of the model one. No credential is configured here and no live call is
// made: the key-missing path is the backend's own answer, and the routing test intercepts the call.

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

async function openProviderSettings(page: import("@playwright/test").Page) {
  await page.getByRole("tab", { name: "Agent" }).click();
  await page.locator(".agent-provider-settings").getByText(/模型服务与高级设置/).click();
  await expect(page.locator(".typesafe-settings")).toBeVisible();
}

test("the TypeSafe fields keep their own accessible names", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E TypeSafe panel naming");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  // Each of these was ambiguous while a TypeSafe label contained another field's name, and
  // Playwright matches a name by substring.
  await expect(page.getByRole("textbox", { name: "Base URL" })).toHaveCount(1);
  await expect(page.getByRole("textbox", { name: /Model name/ })).toHaveCount(1);
  // Three TypeSafe fields (Key / 服务地址 / 判读模型) plus the natural-language sentence
  // field the draw block below them adds.
  await expect(page.locator(".typesafe-settings").getByRole("textbox")).toHaveCount(4);
  await expect(page.getByRole("textbox", { name: "TypeSafe Key" })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "测试 TypeSafe Key" })).toBeVisible();
});

test("the panel reports where a TypeSafe key would come from", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E TypeSafe key source");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  // This run has no TYPESAFE_API_KEY in the server's environment, so the honest answer is amber:
  // the field below is the only way in. In a run where the server does hold one, the same element
  // turns green and the field gains a "leave it empty" hint.
  const source = page.locator(".typesafe-key-source");
  await expect(source).toHaveClass(/typesafe-key-source-(ok|warning)/);
  const text = await source.innerText();
  expect(text).not.toContain("正在询问");
  if (text.includes("TYPESAFE_API_KEY")) {
    await expect(page.getByRole("textbox", { name: "TypeSafe Key" })).toHaveAttribute(
      "placeholder",
      "留空即用服务端环境变量的 Key",
    );
  } else {
    expect(text).toContain("服务端环境变量里没有 TypeSafe Key");
  }
});

test("testing a key that is not there fails loudly instead of silently", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E TypeSafe missing key");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  await page.getByRole("button", { name: "测试 TypeSafe Key" }).click();
  const status = page.locator(".typesafe-settings .provider-model-status");
  await expect(status).toHaveClass(/error/);
  await expect(status).toContainText(/TypeSafe|key/i);
});

test("the toggle routes a drawing request to the judgment endpoint, keyless and unmodified", async ({
  page,
  request,
}) => {
  const seeded = await createDocument(request, "E2E TypeSafe routing");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  const captured: Array<Record<string, unknown>> = [];
  await page.route("**/agent/typesafe-plan", async (route) => {
    captured.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 400,
      contentType: "application/json",
      body: JSON.stringify({ detail: { code: "test_capture", message: "request captured" } }),
    });
  });

  await page.locator(".typesafe-settings").getByRole("checkbox").check();
  await page.getByLabel("自然语言指令").fill("新增一台燃料盐泵，把缓冲罐接到分离塔");
  await page.getByRole("button", { name: "仅生成事务预览（手动模式）" }).click();

  await expect.poll(() => captured.length).toBe(1);
  expect(captured[0].prompt).toBe("新增一台燃料盐泵，把缓冲罐接到分离塔");
  // The field was left empty, so the request carries no key at all: the server's environment is
  // the fallback, and a blank string would have been a key the provider rejects.
  expect(captured[0].api_key).toBeUndefined();
  expect(captured[0].confidence_floor).toBeUndefined();
  await expect(page.locator(".error-box")).toBeVisible();
});
