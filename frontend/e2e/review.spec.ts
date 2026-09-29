import { expect, test } from "@playwright/test";

import { openDocument } from "./fixtures";

test.describe("M9-WS1 review workflow", () => {
  test("a reviewer can thread, comment, close the loop and decide an approval", async ({ page, request }) => {
    const created = await request.post("/api/v2/documents", {
      data: { name: "review e2e", width: 1600, height: 900 },
    });
    const { id } = await created.json();

    await openDocument(page, id);

    const reviewTab = page.getByRole("tab", { name: "审查" });
    await reviewTab.click();
    await expect(page.getByTestId("review-identity")).toContainText("agent 身份");

    await page.getByRole("button", { name: "解锁人审操作" }).click();
    await expect(page.getByTestId("review-identity")).toContainText("操作者身份");

    await page.getByLabel("新审查意见").fill("泵出口建议加止回阀");
    await page.getByRole("button", { name: "发起线程" }).click();
    await expect(page.getByText("泵出口建议加止回阀")).toBeVisible();

    const reply = page.getByLabel(/回复 rt_/);
    await reply.fill("已补充 CV-101");
    await page.getByRole("button", { name: "回复", exact: true }).click();
    await expect(page.getByText("已补充 CV-101")).toBeVisible();

    await page.getByLabel(/闭环说明/).fill("已加止回阀并复核");
    await page.getByRole("button", { name: "闭环" }).click();
    await expect(page.getByText("resolved").first()).toBeVisible();

    await page.getByRole("button", { name: "申请工程批准" }).click();
    await expect(page.getByText(/ap_/)).toBeVisible();

    // Engineering revision never moved through the whole governance session.
    const document = await request.get(`/api/v2/documents/${id}`);
    expect((await document.json()).revision).toBe(0);
  });
});
