import { expect, test } from "@playwright/test";
import {
  API_ROOT,
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

test("previews a deterministic drafting run and applies it through the governed channel", async ({ page, request }) => {
  const document = await createDocument(request, "Drafting acceptance", baseEngineeringOperations());
  await openDocument(page, document.id);

  await page.getByRole("tab", { name: "整理" }).click();
  await expect(page.getByTestId("drafting-panel")).toBeVisible();

  // A read-only check explains the gate instead of only colouring it.
  await page.getByTestId("drafting-analyse").click();
  await expect(page.getByTestId("drafting-report")).toBeVisible();
  await expect(page.getByTestId("drafting-scope-kind")).toContainText("整张图");
  await expect(page.getByTestId("drafting-report-summary")).toContainText(`r${document.revision}`);
  const gateText = await page.getByTestId("drafting-gate").innerText();
  expect(gateText).toMatch(/评分 \d+/);

  // The preview states what it will change, and never writes on its own.
  await page.getByTestId("drafting-preview").click();
  await expect(page.getByTestId("drafting-preview-result")).toBeVisible();
  await expect(page.getByTestId("drafting-preview-summary")).toContainText("将修改");
  await expect(page.getByTestId("drafting-preview-digest")).toContainText("摘要");
  await expect(page.getByTestId("drafting-metrics")).toBeVisible();
  await expect(page.getByTestId("drafting-regressions")).toHaveCount(0);

  const untouched = await getDocument(request, document.id);
  expect(untouched.revision).toBe(document.revision);

  // Applying is an ordinary document transaction: the revision advances and the
  // drawing history records it like any other edit.
  await page.getByTestId("drafting-apply").click();
  await expect(page.getByTestId("drafting-preview-result")).toHaveCount(0);
  await expect(page.locator(".document-bar")).toContainText(`revision ${document.revision + 1}`);

  const applied = await getDocument(request, document.id);
  expect(applied.revision).toBe(document.revision + 1);
  const history = await request.get(`${API_ROOT}/documents/${document.id}/history?limit=5`);
  expect(history.ok()).toBeTruthy();
  const revisions = await history.json() as Array<{ label?: string }>;
  expect(revisions.length).toBeGreaterThan(0);

  // Re-running on its own result settles: the engine has nothing left to change.
  await page.getByTestId("drafting-preview").click();
  await expect(page.getByTestId("drafting-preview-summary")).toContainText("无需改动");
  await expect(page.getByTestId("drafting-apply")).toBeDisabled();
});

test("honours a manual lock written as an ordinary governed transaction", async ({ page, request }) => {
  const document = await createDocument(request, "Drafting lock", baseEngineeringOperations());
  await openDocument(page, document.id);

  // Selecting on the canvas is the natural way to choose what to pin; the drafting tab
  // is then reopened to act on that selection.
  await selectElements(page, ["pump"]);
  await page.getByRole("tab", { name: "整理" }).click();
  await expect(page.getByTestId("drafting-panel")).toBeVisible();
  await page.getByTestId("drafting-lock").click();
  await expect(page.getByTestId("drafting-locked-count")).toContainText("已锁定 1");
  // Locking is an ordinary edit, and an ordinary edit must not eject the engineer from
  // the panel they were working in (the selection array is re-created by every mutation).
  await expect(page.getByTestId("drafting-panel")).toBeVisible();

  // The lock is document data, not panel state: it is visible in the stored document.
  const lockedDocument = await getDocument(request, document.id);
  const pump = lockedDocument.elements.find((element) => element.id === "pump");
  expect(pump?.metadata?.drafting_lock).toBe(true);

  // And the engine reports it as a source of the freeze rather than silently skipping it.
  const report = await request.post(`${API_ROOT}/documents/${document.id}/drafting/report`, {
    data: { expected_revision: lockedDocument.revision },
  });
  expect(report.ok(), await report.text()).toBeTruthy();
  const body = await report.json() as {
    locks: { metadata_element_ids: string[]; locked_element_ids: string[] };
    findings: Array<{ code: string }>;
  };
  expect(body.locks.metadata_element_ids).toContain("pump");
  expect(body.locks.locked_element_ids).toContain("pump");

  await page.getByTestId("drafting-preview").click();
  await expect(page.getByTestId("drafting-preview-result")).toBeVisible();
  const snapshot = await workspaceSnapshot(page);
  expect(Array.isArray(snapshot.selectedElementIds)).toBe(true);

  await page.getByTestId("drafting-discard").click();
  await expect(page.getByTestId("drafting-preview-result")).toHaveCount(0);

  // Unlocking is the same governed channel, in the other direction.
  await page.getByTestId("drafting-unlock").click();
  await expect(page.getByTestId("drafting-locked-count")).toContainText("已锁定 0");
  const unlocked = await getDocument(request, document.id);
  expect(unlocked.elements.find((element) => element.id === "pump")?.metadata?.drafting_lock).toBe(false);
});

test("drafting APIs are read-only and reproducible", async ({ request }) => {
  const document = await createDocument(request, "Drafting API", baseEngineeringOperations());

  const report = await request.post(`${API_ROOT}/documents/${document.id}/drafting/report`, { data: {} });
  expect(report.ok(), await report.text()).toBeTruthy();
  const reportBody = await report.json() as {
    schema: string;
    engine_version: number;
    revision: number;
    ports: Array<{ element_id: string; port_id: string }>;
    gate: { passed: boolean; checked_codes: string[]; score: number; target_score: number };
    junctions: Array<{ element_id: string; degree: number; kind: string }>;
    crossings: Array<{ bridged: boolean }>;
  };
  expect(reportBody.schema).toBe("pid-agent.drafting-report");
  expect(reportBody.engine_version).toBeGreaterThanOrEqual(1);
  expect(reportBody.revision).toBe(document.revision);
  expect(reportBody.gate.checked_codes.length).toBeGreaterThan(0);
  expect(reportBody.ports.length).toBeGreaterThan(0);

  const first = await request.post(`${API_ROOT}/documents/${document.id}/drafting/preview`, {
    data: { expected_revision: document.revision },
  });
  expect(first.ok(), await first.text()).toBeTruthy();
  const preview = await first.json() as {
    transaction: { operations: unknown[]; expected_revision: number } | null;
    reproducibility: { transaction_digest: string; engine_version: number; operation_count: number };
    metrics: { before: { score: number }; after: { score: number }; regressions: string[] };
    gate: { passed: boolean };
    moved_element_ids: string[];
    locked_element_ids: string[];
  };
  expect(preview.transaction).not.toBeNull();
  expect(preview.reproducibility.transaction_digest).toMatch(/^[0-9a-f]{64}$/);
  expect(preview.metrics.regressions).toEqual([]);
  expect(preview.metrics.after.score).toBeGreaterThanOrEqual(preview.metrics.before.score);
  expect(preview.locked_element_ids).toEqual([]);

  // Same content, same request, same digest — and still no write.
  const second = await request.post(`${API_ROOT}/documents/${document.id}/drafting/preview`, {
    data: { expected_revision: document.revision },
  });
  const again = await second.json() as { reproducibility: { transaction_digest: string } };
  expect(again.reproducibility.transaction_digest).toBe(preview.reproducibility.transaction_digest);
  const unchanged = await getDocument(request, document.id);
  expect(unchanged.revision).toBe(document.revision);

  // A stale revision is refused rather than silently re-based.
  const stale = await request.post(`${API_ROOT}/documents/${document.id}/drafting/preview`, {
    data: { expected_revision: document.revision + 5 },
  });
  expect(stale.status()).toBe(409);

  const unknown = await request.post(`${API_ROOT}/documents/does_not_exist/drafting/report`, { data: {} });
  expect(unknown.status()).toBe(404);
});
