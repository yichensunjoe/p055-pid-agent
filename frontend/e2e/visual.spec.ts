import { expect, test, type Page } from "@playwright/test";
import {
  baseEngineeringOperations,
  createDocument,
  openDocument,
  resetDocuments,
  selectElements,
  style,
  workspaceSnapshot,
} from "./fixtures";

async function expectPageScreenshot(page: Page, name: string): Promise<void> {
  await expect(page.getByTestId("app-shell")).toHaveScreenshot(name, {
    animations: "disabled",
    caret: "hide",
    scale: "css",
  });
}

async function switchTheme(page: Page, theme: "light" | "dark"): Promise<void> {
  await page.getByTestId("experience-settings-trigger").click();
  const dialog = page.getByRole("dialog", { name: "编辑偏好" });
  await dialog.getByRole("button", { name: theme === "dark" ? /深色/ : /浅色/ }).click();
  await dialog.getByRole("button", { name: "保存偏好" }).click();
  await expect(page.getByTestId("app-shell")).toHaveAttribute("data-theme", theme);
}

function largeDiagramOperations(): Array<Record<string, unknown>> {
  const operations = baseEngineeringOperations();
  for (let index = 0; index < 48; index += 1) {
    operations.push({
      op: "add_element",
      element: {
        id: `visual-zone-${index}`,
        type: "rectangle",
        x: 260 + (index % 12) * 340,
        y: 520 + Math.floor(index / 12) * 420,
        width: 90,
        height: 60,
        corner_radius: 4,
        name: `Area ${String(index + 1).padStart(2, "0")}`,
        layer_id: "layer_default",
        system_id: "system_default",
        style: style("#475569", "none"),
        metadata: {},
      },
    });
  }
  return operations;
}

test.beforeEach(async ({ page, request }) => {
  await resetDocuments(request);
  await page.addInitScript(() => {
    localStorage.clear();
    sessionStorage.clear();
    const fixedNow = Date.UTC(2026, 6, 21, 8, 0, 0);
    Date.now = () => fixedNow;
  });
});

test("blank editor light theme", async ({ page, request }) => {
  const document = await createDocument(request, "Visual blank light");
  await openDocument(page, document.id);
  await switchTheme(page, "light");
  await expectPageScreenshot(page, "blank-editor-light.png");
});

test("blank editor dark theme", async ({ page, request }) => {
  const document = await createDocument(request, "Visual blank dark");
  await openDocument(page, document.id);
  await switchTheme(page, "dark");
  await expectPageScreenshot(page, "blank-editor-dark.png");
});

test("engineering drawing with main and branch lines", async ({ page, request }) => {
  const document = await createDocument(request, "Visual engineering drawing", baseEngineeringOperations());
  await openDocument(page, document.id);
  await expectPageScreenshot(page, "engineering-drawing.png");
});

test("multi-selection and floating toolbar", async ({ page, request }) => {
  const document = await createDocument(request, "Visual multi selection", baseEngineeringOperations());
  await openDocument(page, document.id);
  await selectElements(page, ["tank", "pump", "valve"]);
  await expect(page.getByTestId("canvas-floating-toolbar")).toBeVisible();
  await expectPageScreenshot(page, "multi-selection-toolbar.png");
});

test("locked element badges", async ({ page, request }) => {
  const operations = baseEngineeringOperations().map((operation) => {
    const element = (operation as any).element;
    if (!element || !["tank", "pump", "valve"].includes(element.id)) return operation;
    return { ...operation, element: { ...element, metadata: { ...element.metadata, editor_locked: true } } };
  });
  const document = await createDocument(request, "Visual locked badges", operations);
  await openDocument(page, document.id);
  await selectElements(page, ["tank"]);
  await expect(page.getByTestId("element-lock-badge")).toHaveCount(3);
  await expectPageScreenshot(page, "locked-element-badges.png");
});

test("connector route anchors", async ({ page, request }) => {
  const operations = baseEngineeringOperations().map((operation) => {
    const element = (operation as any).element;
    if (element?.id !== "main") return operation;
    return {
      ...operation,
      element: {
        ...element,
        metadata: { ...element.metadata, locked_route_points: [1] },
      },
    };
  });
  const document = await createDocument(request, "Visual route anchors", operations);
  await openDocument(page, document.id);
  await selectElements(page, ["main"]);
  await expect(page.locator('[data-connector-id="main"].connector-route-anchor')).toHaveCount(2);
  await expectPageScreenshot(page, "connector-route-anchors.png");
});

test("Agent ghost preview", async ({ page, request }) => {
  const document = await createDocument(request, "Visual Agent preview", baseEngineeringOperations());
  await openDocument(page, document.id);
  const revision = (await workspaceSnapshot(page)).document.revision;
  await page.evaluate((currentRevision) => {
    const operation = { op: "update_element", element_id: "tank", patch: { position: { x: 300, y: 330 }, label: "V-101A" } };
    const snapshot = window.__PID_AGENT_E2E__!.snapshot();
    window.__PID_AGENT_E2E__!.setAgentPreview({
      plan: {
        plan_id: "plan_visual_0001",
        explanation: "Deterministic visual preview",
        transaction: { expected_revision: currentRevision, label: "Visual preview", operations: [operation] },
      },
      compiled_plan: {
        explanation: "Deterministic visual preview",
        transaction: { expected_revision: currentRevision, label: "Visual preview", operations: [operation] },
      },
      assessment: {
        valid: true,
        stage: "validate",
        document_id: snapshot.document!.id,
        current_revision: currentRevision,
        next_revision: currentRevision + 1,
        semantic_operation_count: 1,
        compiled_operation_count: 1,
        resulting_element_count: snapshot.document!.elements.length,
        affected_element_ids: ["tank"],
        added_element_ids: [],
        updated_element_ids: ["tank"],
        deleted_element_ids: [],
        issues: [],
        // Real server responses carry the two-axis accounting; the ghost preview is gated on
        // a valid *and* complete proposal, so a fixture without it is not previewable.
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
  }, revision);
  await expect(page.getByTestId("agent-ghost-preview")).toBeVisible();
  await expectPageScreenshot(page, "agent-ghost-preview.png");
});

test("minimap and large-diagram zones", async ({ page, request }) => {
  const document = await createDocument(request, "Visual large diagram", largeDiagramOperations(), { width: 5200, height: 3200 });
  await openDocument(page, document.id);
  await page.getByTestId("view-navigator-trigger").click();
  await expect(page.getByRole("dialog", { name: "大图视图导航" })).toBeVisible();
  await expect(page.getByTestId("canvas-minimap")).toBeVisible();
  await expectPageScreenshot(page, "minimap-large-zones.png");
});

test("bulk property mixed-value state", async ({ page, request }) => {
  const operations = baseEngineeringOperations().map((operation) => {
    const element = (operation as any).element;
    if (element?.id !== "valve") return operation;
    return { ...operation, element: { ...element, style: style("#dc2626", "#fee2e2") } };
  });
  const document = await createDocument(request, "Visual bulk mixed", operations);
  await openDocument(page, document.id);
  await selectElements(page, ["tank", "valve"]);
  await expect(page.locator('.inspector-panel input[name="stroke"]')).toHaveAttribute("placeholder", "混合；留空不修改");
  await expectPageScreenshot(page, "bulk-properties-mixed.png");
});

test("command palette", async ({ page, request }) => {
  const document = await createDocument(request, "Visual command palette", baseEngineeringOperations());
  await openDocument(page, document.id);
  await selectElements(page, ["tank", "pump"]);
  await page.getByTestId("command-palette-trigger").click();
  await expect(page.getByRole("dialog", { name: "命令面板" })).toBeVisible();
  await expectPageScreenshot(page, "command-palette.png");
});
