import { expect, test } from "@playwright/test";
import { API_ROOT, baseEngineeringOperations, createDocument, openDocument, resetDocuments } from "./fixtures";

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

test("inspects derived engineering objects, findings and a trace", async ({ page, request }) => {
  const document = await createDocument(request, "Engineering graph acceptance", baseEngineeringOperations());
  await openDocument(page, document.id);

  await page.getByRole("tab", { name: "工程图谱" }).click();
  await expect(page.getByTestId("engineering-graph-panel")).toBeVisible();
  await expect(page.getByTestId("graph-revision")).toContainText(`revision ${document.revision}`);

  // The panel shows derived engineering objects, not raw geometry.
  const equipmentGroup = page.getByTestId("graph-group-equipment");
  await expect(equipmentGroup).toBeVisible();
  await expect(equipmentGroup).toContainText("位号标识");
  await expect(page.getByTestId("graph-edge-count")).toBeVisible();
  await expect(page.getByTestId("graph-severity-all")).toBeVisible();

  // Filtering narrows the object list without calling the backend again.
  await page.getByTestId("graph-filter").fill("XV-101");
  await expect(page.getByTestId("graph-groups")).toContainText("XV-101");
  await page.getByTestId("graph-filter").fill("");

  // Tracing walks the topology and reports the pipelines traversed.
  const traceButton = page.locator('[data-testid^="graph-trace-"]').first();
  await traceButton.click();
  await expect(page.getByTestId("graph-trace-summary")).toContainText("个对象");
  await expect(page.getByTestId("graph-trace-steps").locator("li").first()).toContainText("起点");
  await page.getByTestId("graph-trace-direction").selectOption("downstream");
  await expect(page.getByTestId("graph-trace-summary")).toContainText("下游");

  // The project index section never claims more freshness than it verified.
  await expect(page.getByTestId("graph-project-summary")).toContainText("张图纸");
  await expect(page.getByTestId("graph-project-summary")).toContainText("仅比对 revision");
  await page.getByTestId("graph-rebuild-index").click();
  await expect(page.getByTestId("graph-project-totals")).toContainText("对象");
});

test("engineering graph APIs are read-only until the index rebuild is requested", async ({ request }) => {
  const document = await createDocument(request, "Engineering graph API", baseEngineeringOperations());

  const before = await request.get(`${API_ROOT}/documents/${document.id}`);
  const beforeRevision = (await before.json() as { revision: number }).revision;

  const graph = await request.get(`${API_ROOT}/documents/${document.id}/engineering-graph`);
  expect(graph.ok(), await graph.text()).toBeTruthy();
  expect(graph.headers()["x-pid-agent-graph-revision"]).toBe(String(beforeRevision));
  const body = await graph.json() as { schema: string; objects: Array<{ object_id: string; identity_scope: string }>; edges: unknown[] };
  expect(body.schema).toBe("pid-agent.engineering-graph");
  expect(body.objects.length).toBeGreaterThan(0);
  expect(body.objects.every((object) => object.identity_scope === "tag" || object.identity_scope === "element")).toBe(true);

  const trace = await request.get(
    `${API_ROOT}/documents/${document.id}/engineering-graph/trace?object_id=${encodeURIComponent(body.objects[0].object_id)}&direction=downstream`,
  );
  expect(trace.ok(), await trace.text()).toBeTruthy();
  expect((await trace.json() as { origin_object_id: string }).origin_object_id).toBe(body.objects[0].object_id);

  const unknown = await request.get(
    `${API_ROOT}/documents/${document.id}/engineering-graph/trace?object_id=equipment:does-not-exist`,
  );
  expect(unknown.status()).toBe(404);

  // A read must not have created an index row or moved the revision.
  const entryBefore = await request.get(`${API_ROOT}/documents/${document.id}/project-index`);
  expect(entryBefore.status()).toBe(404);
  const afterReads = await request.get(`${API_ROOT}/documents/${document.id}`);
  expect((await afterReads.json() as { revision: number }).revision).toBe(beforeRevision);

  const rebuild = await request.post(`${API_ROOT}/project/index/rebuild`);
  expect(rebuild.ok(), await rebuild.text()).toBeTruthy();
  const entryAfter = await request.get(`${API_ROOT}/documents/${document.id}/project-index`);
  expect(entryAfter.ok()).toBeTruthy();
  expect((await entryAfter.json() as { staleness: string }).staleness).toBe("verified_fresh");

  // The rebuild is recorded as its own audit fact and keeps the chain valid.
  const audit = await request.get(`${API_ROOT}/audit/records?event_type=engineering.index.rebuilt&limit=5`);
  expect(audit.ok()).toBeTruthy();
  expect((await audit.json() as unknown[]).length).toBeGreaterThan(0);
  const verification = await request.get(`${API_ROOT}/audit/verify`);
  expect((await verification.json() as { ok: boolean }).ok).toBe(true);
});
