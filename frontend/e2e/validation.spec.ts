import { expect, test } from "@playwright/test";
import {
  API_ROOT,
  createDocument,
  getDocument,
  openDocument,
  resetDocuments,
  symbol,
} from "./fixtures";

/**
 * The M4 validation surface, end to end.
 *
 * The panel is a viewer, so what is asserted here is that the *server's* canonical result
 * reaches a reviewer intact — code, severity, locators, expected vs actual, rule source,
 * waiver state, provenance and the readiness verdict — and that reading it changes
 * nothing: the document revision and history are identical afterwards, and no approval
 * field is anywhere on screen.
 *
 * This spec takes no screenshots, so it does not participate in the visual baseline sets.
 */

function duplicateTagOperations(): Array<Record<string, unknown>> {
  return [
    { op: "add_layer", layer: { id: "layer_process", name: "Process", visible: true, locked: false } },
    { op: "add_element", element: symbol("valve_a", "gate_valve", { x: 240, y: 300 }, 60, 50, "HV-101") },
    { op: "add_element", element: symbol("valve_b", "gate_valve", { x: 640, y: 300 }, 60, 50, "HV-101") },
  ];
}

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

test("reviews the canonical validation result without changing the drawing", async ({ page, request }) => {
  const document = await createDocument(request, "Validation acceptance", duplicateTagOperations());
  await openDocument(page, document.id);

  const historyBefore = await request.get(`${API_ROOT}/documents/${document.id}/history`);
  expect(historyBefore.ok()).toBeTruthy();
  const before = await historyBefore.json();

  await page.getByRole("tab", { name: /报表/ }).click();

  const panel = page.getByTestId("validation-panel");
  await expect(panel).toBeVisible();

  // The canonical issue, with everything a reviewer needs to judge it.
  const issue = panel.getByTestId("validation-issue").filter({ hasText: "TAG_DUPLICATE" }).first();
  await expect(issue).toBeVisible();
  await expect(issue).toContainText("错误");
  await expect(issue).toContainText("图元");
  await expect(issue).toContainText("valve_a");
  await expect(issue).toContainText("规则来源");
  await expect(issue).toContainText("built-in");
  await expect(issue).toContainText("豁免状态");
  await expect(issue).toContainText("未豁免");

  // Provenance: the result is bound to what produced it, and to when it was judged.
  const provenance = panel.getByTestId("validation-provenance");
  await expect(provenance).toContainText("规则包指纹");
  await expect(provenance).toContainText("符号目录指纹");
  await expect(provenance).toContainText("评估时刻");
  await expect(provenance).toContainText("已运行校验器");
  await expect(provenance).toContainText("engineering-report");

  // Readiness is evidence, not approval.
  const readiness = panel.getByTestId("validation-readiness");
  await expect(readiness).toHaveAttribute("data-state", /eligible|not_eligible/);
  await expect(panel.getByTestId("validation-disclaimer")).toContainText("不会批准");

  // Filtering is a view concern: it narrows what is shown and never changes the verdict.
  const total = await panel.getByTestId("validation-issue").count();
  expect(total).toBeGreaterThan(0);
  await panel.getByLabel("校验级别过滤").selectOption("blocker");
  await expect(panel.getByTestId("validation-issue")).toHaveCount(0);
  await panel.getByLabel("校验级别过滤").selectOption("error");
  const errors = panel.getByTestId("validation-issue");
  await expect(errors.first()).toBeVisible();
  for (const severity of await errors.evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-severity")))) {
    expect(severity).toBe("error");
  }

  // Locating an issue selects the drawing elements it names.
  await issue.getByRole("button").first().click();
  const after = await getDocument(request, document.id);
  expect(after.revision).toBe(document.revision);

  const historyAfter = await request.get(`${API_ROOT}/documents/${document.id}/history`);
  expect(await historyAfter.json()).toEqual(before);

  const body = await page.locator("body").innerText();
  expect(body).not.toMatch(/已批准|Approved|IFC|AFC/);
});
