import { expect, test } from "@playwright/test";
import {
  baseEngineeringOperations,
  createDocument,
  getDocument,
  openDocument,
  resetDocuments,
  selectElements,
  workspaceSnapshot,
} from "./fixtures";

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

test("switches themes without mutating engineering SVG colors and saves a named view", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E views and themes", baseEngineeringOperations(), { width: 4200, height: 2600 });
  await openDocument(page, seeded.id);

  const mainLine = page.locator('[data-element-id="main"] polyline').last();
  const strokeBefore = await mainLine.getAttribute("stroke");
  const revisionBefore = (await workspaceSnapshot(page)).document.revision;

  await page.getByTestId("experience-settings-trigger").click();
  const preferences = page.getByRole("dialog", { name: "编辑偏好" });
  await preferences.getByRole("button", { name: /深色/ }).click();
  await preferences.getByRole("button", { name: "保存偏好" }).click();
  await expect(page.getByTestId("app-shell")).toHaveAttribute("data-theme", "dark");
  expect(await mainLine.getAttribute("stroke")).toBe(strokeBefore);
  expect((await workspaceSnapshot(page)).document.revision).toBe(revisionBefore);

  await page.getByTestId("view-navigator-trigger").click();
  const navigator = page.getByRole("dialog", { name: "大图视图导航" });
  await expect(navigator.getByText("自动分区")).toBeVisible();
  await navigator.getByPlaceholder("视图名称").fill("泵入口区域");
  await navigator.getByRole("button", { name: "保存当前视口" }).click();
  await expect(navigator.getByText("泵入口区域")).toBeVisible();
  await navigator.getByRole("button", { name: /泵入口区域/ }).click();
  await expect(page.getByTestId("canvas-minimap")).toBeVisible();

  await page.reload();
  await page.waitForFunction(() => Boolean(window.__PID_AGENT_E2E__));
  await expect(page.getByTestId("app-shell")).toHaveAttribute("data-theme", "dark");
  await page.getByTestId("view-navigator-trigger").click();
  await expect(page.getByRole("dialog", { name: "大图视图导航" }).getByText("泵入口区域")).toBeVisible();
});

test("previews a deterministic Agent transaction without revision changes, then applies and undoes it", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E agent preview", baseEngineeringOperations());
  await openDocument(page, seeded.id);
  const revisionBefore = (await workspaceSnapshot(page)).document.revision;

  await page.evaluate((revision) => {
    const operation = { op: "update_element", element_id: "tank", patch: { label: "V-101A" } };
    window.__PID_AGENT_E2E__!.setAgentPreview({
      plan: {
        plan_id: "plan_e2e_preview_0001",
        explanation: "Deterministic Playwright preview",
        transaction: {
          expected_revision: revision,
          label: "Rename tank from deterministic test provider",
          operations: [operation],
        },
      },
      compiled_plan: {
        explanation: "Deterministic Playwright preview",
        transaction: {
          expected_revision: revision,
          label: "Rename tank from deterministic test provider",
          operations: [operation],
        },
      },
      assessment: {
        valid: true,
        stage: "validate",
        document_id: window.__PID_AGENT_E2E__!.snapshot().document!.id,
        current_revision: revision,
        next_revision: revision + 1,
        semantic_operation_count: 1,
        compiled_operation_count: 1,
        resulting_element_count: window.__PID_AGENT_E2E__!.snapshot().document!.elements.length,
        affected_element_ids: ["tank"],
        added_element_ids: [],
        updated_element_ids: ["tank"],
        deleted_element_ids: [],
        issues: [],
        // The server always reports the accounting axes; a fixture that omits them is
        // describing a response that no longer exists, and the confirmation surface reads
        // them before it offers apply.
        operation_accounting: "evaluated",
        accepted_operation_count: 1,
        rejected_operation_count: 0,
        completeness: "complete",
        rejected_operations: [],
      },
      attempt: 1,
      parent_plan_id: null,
      annotation_metrics: null,
    } as any);
  }, revisionBefore);

  await expect(page.getByTestId("agent-preview-badge")).toBeVisible();
  await expect(page.getByTestId("agent-ghost-preview")).toBeVisible();
  expect((await workspaceSnapshot(page)).document.revision).toBe(revisionBefore);

  await page.getByRole("tab", { name: "Agent" }).click();
  await page.getByRole("button", { name: "确认应用" }).click();
  await expect.poll(async () => (await workspaceSnapshot(page)).document.revision).toBe(revisionBefore + 1);
  let snapshot = await workspaceSnapshot(page);
  expect(snapshot.document.elements.find((item: any) => item.id === "tank").label).toBe("V-101A");
  await expect(page.getByTestId("agent-ghost-preview")).toHaveCount(0);

  const appliedRevision = snapshot.document.revision;
  await page.getByRole("button", { name: "撤销" }).click();
  await expect.poll(async () => {
    const next = await workspaceSnapshot(page);
    return {
      revision: next.document.revision,
      label: next.document.elements.find((item: any) => item.id === "tank").label,
    };
  }).toEqual({ revision: appliedRevision + 1, label: "V-101" });
  snapshot = await workspaceSnapshot(page);
  const persisted = await getDocument(request, seeded.id);
  expect(persisted.revision).toBe(snapshot.document.revision);
  expect(persisted.elements.find((item: any) => item.id === "tank")?.label).toBe("V-101");
});

test("uses minimap and automatic zones to navigate a large diagram", async ({ page, request }) => {
  const operations = baseEngineeringOperations();
  for (let index = 0; index < 40; index += 1) {
    operations.push({
      op: "add_element",
      element: {
        id: `zone-${index}`,
        type: "rectangle",
        x: 300 + (index % 10) * 360,
        y: 500 + Math.floor(index / 10) * 420,
        width: 90,
        height: 60,
        corner_radius: 4,
        name: `Zone ${index}`,
        layer_id: "layer_default",
        system_id: "system_default",
        style: { stroke: "#334155", fill: "none", stroke_width: 1.5, opacity: 1, dash: [] },
        metadata: {},
      },
    });
  }
  const seeded = await createDocument(request, "E2E large navigation", operations, { width: 5200, height: 3200 });
  await openDocument(page, seeded.id);

  const canvas = page.getByTestId("editor-canvas");
  const viewBefore = await canvas.getAttribute("viewBox");
  const minimap = page.getByTestId("canvas-minimap").getByRole("img", { name: "画布缩略导航" });
  const box = await minimap.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.click(box!.x + box!.width * 0.85, box!.y + box!.height * 0.8);
  await expect.poll(async () => canvas.getAttribute("viewBox")).not.toBe(viewBefore);

  await page.getByTestId("view-navigator-trigger").click();
  const navigator = page.getByRole("dialog", { name: "大图视图导航" });
  const zoneButtons = navigator.locator(".zone-grid button");
  expect(await zoneButtons.count()).toBeGreaterThan(1);
  await zoneButtons.last().click();
  await expect.poll(async () => canvas.getAttribute("viewBox")).not.toBe(viewBefore);

  await selectElements(page, ["zone-39"]);
  await page.getByTestId("canvas-status-bar").getByRole("button", { name: "适应选择" }).click();
  await expect(page.getByTestId("canvas-status-bar")).toContainText("1 selected");
});

test("configures the OpenAI compatible preset with standard URL", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E provider preset");
  await openDocument(page, seeded.id);
  const revisionBefore = (await workspaceSnapshot(page)).document.revision;

  await page.getByRole("tab", { name: "Agent" }).click();
  await page.locator(".agent-provider-settings").getByText(/模型服务与高级设置/).click();
  await page.getByRole("combobox", { name: "服务预设" }).selectOption("openai-compatible");

  await expect(page.getByRole("textbox", { name: "Base URL" })).toHaveValue("https://api.openai.com/v1");
  await expect(page.getByRole("textbox", { name: /Model name/ })).toHaveValue("");
  expect((await workspaceSnapshot(page)).document.revision).toBe(revisionBefore);
});

test("local deployment loads without a service token", async ({ page, request }) => {
  const seeded = await createDocument(request, "E2E local security baseline");
  await openDocument(page, seeded.id);

  await expect(page.getByTestId("project-summary")).toContainText("1 个文档");
  expect(await page.evaluate(() => window.sessionStorage.getItem("pid-agent-service-token"))).toBeNull();
  expect(page.url()).not.toContain("token=");
});
