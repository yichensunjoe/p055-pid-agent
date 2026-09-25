import { expect, test } from "@playwright/test";
import { createDocument, openDocument, resetDocuments, symbol, workspaceSnapshot } from "./fixtures";

// The natural-language draw block turns one sentence into one committed drawing through the
// governed M7 chain. The browser suite covers what unit tests cannot: that the controls are
// addressable next to the TypeSafe fields, that 预览 really sends dry_run and 生成图纸 does not,
// and that a committed result refreshes the canvas while a refused one is said out loud. No
// credential is configured and no judgment call is made -- every request is intercepted.

const SENTENCE = "添加一个缓冲罐 V-101，添加一台燃料盐泵 P-101，把 V-101 接到 P-101";

const DRAW_RESULT = {
  document_id: "pending",
  committed: true,
  revision: 1,
  spec: {
    label: "自然语言生成的图纸",
    entities: [
      { engineering_id: "el_V_101", tag: "V-101", name: "缓冲罐", symbol_key: "buffer_tank" },
      { engineering_id: "el_P_101", tag: "P-101", name: "燃料盐泵", symbol_key: "fuel_salt_pump" },
    ],
    connections: [
      { engineering_id: "cn_1", source_engineering_id: "el_V_101", target_engineering_id: "el_P_101" },
    ],
  },
  canonical_layout_digest: "c" + "0".repeat(63),
  materialization_digest: "d" + "0".repeat(63),
  notes: ["TypeSafe 判读：2 个判断（2 个设备、1 条连接），代码查找决定 0 个设备。完整度：complete。"],
  skipped: [],
  unknown_tags: [],
  completeness: "complete",
  undelivered: [],
  catalog_gaps: [],
  model: "judge-stub",
  latency_ms: 3,
  question_count: 2,
  judgment_count: 2,
};

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

async function openProviderSettings(page: import("@playwright/test").Page) {
  await page.getByRole("tab", { name: "Agent" }).click();
  await page.locator(".agent-provider-settings").getByText(/模型服务与高级设置/).click();
  await expect(page.locator(".typesafe-settings")).toBeVisible();
}

test("the NL draw controls keep their own accessible names", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E NL draw naming");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  await expect(page.getByRole("textbox", { name: "自然语言一句话出图" })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "预览", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "生成图纸" })).toBeVisible();
  // The sentence must not collide with the agent prompt field above.
  await expect(page.getByRole("textbox", { name: "自然语言指令" })).toHaveCount(1);
});

test("preview sends the sentence with dry_run and shows nothing was written", async ({
  page,
  request,
}) => {
  const seeded = await createDocument(request, "E2E NL draw preview");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  const captured: Array<Record<string, unknown>> = [];
  await page.route("**/agent/text-plan", async (route) => {
    captured.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...DRAW_RESULT, document_id: seeded.id, committed: false, revision: null }),
    });
  });

  await page.getByRole("textbox", { name: "自然语言一句话出图" }).fill(SENTENCE);
  await page.getByRole("button", { name: "预览", exact: true }).click();

  await expect.poll(() => captured.length).toBe(1);
  expect(captured[0].sentence).toBe(SENTENCE);
  expect(captured[0].dry_run).toBe(true);
  expect(captured[0].api_key).toBeUndefined();

  const result = page.locator(".nl-draw-result");
  await expect(result).toContainText("预览（未写入图纸）");
  await expect(result).toContainText("2 台设备");
  await expect(result).toContainText("1 条连接");
});

test("a committed draw refreshes the canvas to the written revision", async ({
  page,
  request,
}) => {
  const seeded = await createDocument(request, "E2E NL draw commit");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  await page.route("**/agent/text-plan", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...DRAW_RESULT, document_id: seeded.id }),
    });
  });
  await page.route(`**/documents/${seeded.id}`, async (route) => {
    if (route.request().method() !== "GET") {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...seeded,
        revision: 1,
        elements: [
          symbol("el_v_101", "buffer_tank", { x: 200, y: 200 }, 90, 140, "V-101"),
          symbol("el_p_101", "fuel_salt_pump", { x: 600, y: 220 }, 80, 70, "P-101"),
        ],
      }),
    });
  });

  await page.getByRole("textbox", { name: "自然语言一句话出图" }).fill(SENTENCE);
  await page.getByRole("button", { name: "生成图纸" }).click();

  const result = page.locator(".nl-draw-result");
  await expect(result).toContainText("已画入 r1");
  await expect(page.locator(".sync-badge")).toContainText("已同步至 r1");
  await expect.poll(async () => (await workspaceSnapshot(page)).document?.elements?.length).toBe(2);
});

test("a partial plan is refused with its receipt and changes nothing", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E NL draw partial");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  await page.route("**/agent/text-plan", async (route) => {
    await route.fulfill({
      status: 422,
      contentType: "application/json",
      body: JSON.stringify({
        detail: {
          code: "typesafe_spec_partial",
          completeness: "partial",
          sentence: SENTENCE,
          skipped: [],
          unknown_tags: [],
          undelivered: [
            "「燃料盐泵」（位号 P-101）没有被兑现。",
            "「把 V-101 接到 P-101」没有被兑现。",
          ],
        },
      }),
    });
  });

  await page.getByRole("textbox", { name: "自然语言一句话出图" }).fill(SENTENCE);
  await page.getByRole("button", { name: "生成图纸" }).click();

  const error = page.locator(".nl-draw .provider-model-status.error");
  await expect(error).toContainText("只兑现了一部分");
  await expect(error).toContainText("P-101");
  await expect(page.locator(".nl-draw-result")).toHaveCount(0);
  await expect(page.locator(".sync-badge")).not.toContainText("已同步至 r1");
});

test("a refused draw says why and changes nothing", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E NL draw refused");
  await openDocument(page, seeded.id);
  await openProviderSettings(page);

  await page.route("**/agent/text-plan", async (route) => {
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        detail:
          "document already holds 4 element(s) and this layout did not decide them: " +
          "an add-only materialization appends a whole drawing, so a non-empty target " +
          "would be a merge that only shows up later",
      }),
    });
  });

  await page.getByRole("textbox", { name: "自然语言一句话出图" }).fill(SENTENCE);
  await page.getByRole("button", { name: "生成图纸" }).click();

  const error = page.locator(".nl-draw .provider-model-status.error");
  await expect(error).toContainText("non-empty target");
  await expect(page.locator(".nl-draw-result")).toHaveCount(0);
});
