import { expect, test } from "@playwright/test";
import {
  API_ROOT,
  baseEngineeringOperations,
  createDocument,
  openDocument,
  resetDocuments,
  workspaceSnapshot,
} from "./fixtures";

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

test("inspects derived engineering objects, findings and a trace", async ({ page, request }) => {
  const document = await createDocument(request, "Engineering graph acceptance", baseEngineeringOperations());
  await openDocument(page, document.id);

  await page.getByRole("tab", { name: "工程图谱" }).click();
  await expect(page.getByTestId("engineering-graph-panel")).toBeVisible();
  await expect(page.getByTestId("graph-revision")).toContainText(`revision ${document.revision}`);

  // The panel shows derived engineering objects, not raw geometry, and states where
  // each object's immutable identity came from.
  const equipmentGroup = page.getByTestId("graph-group-equipment");
  await expect(equipmentGroup).toBeVisible();
  await expect(equipmentGroup).toContainText("图元句柄标识");
  await expect(page.getByTestId("graph-edge-count")).toContainText("工艺边");
  await expect(page.getByTestId("graph-signal-count")).toContainText("信号");
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

  // Locating a traced object highlights it on the canvas without ejecting the user
  // from the analysis panel they were reading.
  const locateButton = page.locator('[data-testid^="graph-trace-locate-"]').first();
  if (await locateButton.count()) {
    await locateButton.click();
    await expect(page.getByTestId("engineering-graph-panel")).toBeVisible();
    const selected = (await workspaceSnapshot(page)).selectedElementIds as string[];
    expect(selected.length).toBeGreaterThan(0);
  }

  // The project index section never claims more freshness than it verified.
  await expect(page.getByTestId("graph-project-summary")).toContainText("张图纸");
  await expect(page.getByTestId("graph-project-summary")).toContainText("仅比对 revision");
  await expect(page.getByTestId("graph-project-summary")).toContainText("跨图连接");
  await page.getByTestId("graph-rebuild-index").click();
  await expect(page.getByTestId("graph-project-totals")).toContainText("对象");
});

test("the right dock keeps every panel tab on one row", async ({ page, request }) => {
  // Regression guard: the tab strip used to be laid out for a fixed number of tabs,
  // so adding one pushed a tab out of the dock (and shifted the whole page).
  const document = await createDocument(request, "Right dock tabs", baseEngineeringOperations());
  await openDocument(page, document.id);

  const dock = page.getByRole("tablist", { name: "右侧面板" });
  await expect(dock).toBeVisible();
  const tabs = dock.getByRole("tab");
  await expect(tabs).toHaveCount(6);
  await expect(page.getByRole("tab", { name: "工程图谱" })).toBeVisible();

  const boxes = await tabs.evaluateAll((nodes) =>
    nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      return { top: Math.round(rect.top), right: Math.round(rect.right), width: rect.width };
    }),
  );
  const rows = new Set(boxes.map((box) => box.top));
  expect(rows.size, `tabs occupy ${rows.size} rows: ${JSON.stringify(boxes)}`).toBe(1);
  const dockRight = await dock.evaluate((node) => Math.round(node.getBoundingClientRect().right));
  for (const box of boxes) {
    expect(box.width).toBeGreaterThan(0);
    expect(box.right).toBeLessThanOrEqual(dockRight + 1);
  }
});

test("engineering graph APIs are read-only until the index rebuild is requested", async ({ request }) => {
  const document = await createDocument(request, "Engineering graph API", baseEngineeringOperations());

  const before = await request.get(`${API_ROOT}/documents/${document.id}`);
  const beforeRevision = (await before.json() as { revision: number }).revision;

  const graph = await request.get(`${API_ROOT}/documents/${document.id}/engineering-graph`);
  expect(graph.ok(), await graph.text()).toBeTruthy();
  expect(graph.headers()["x-pid-agent-graph-revision"]).toBe(String(beforeRevision));
  const body = await graph.json() as {
    schema: string;
    version: number;
    objects: Array<{ engineering_id: string; identity_basis: string; tag_key: string }>;
    counts: { signals: number; process_edges: number; signal_edges: number };
    edges: unknown[];
  };
  expect(body.schema).toBe("pid-agent.engineering-graph");
  expect(body.version).toBe(2);
  expect(body.objects.length).toBeGreaterThan(0);
  expect(
    body.objects.every(
      (object) => object.identity_basis === "declared" || object.identity_basis === "element",
    ),
  ).toBe(true);

  // Identity is listed separately from the mutable tag key.
  const first = body.objects[0];
  const trace = await request.get(
    `${API_ROOT}/documents/${document.id}/engineering-graph/trace?ref=${encodeURIComponent(first.engineering_id)}&direction=downstream`,
  );
  expect(trace.ok(), await trace.text()).toBeTruthy();
  const traced = await trace.json() as { origin_engineering_id: string; edge_class: string };
  expect(traced.origin_engineering_id).toBe(first.engineering_id);
  expect(traced.edge_class).toBe("process");

  // A tag-based reference still resolves, and reports what it resolved to.
  const tagged = body.objects.find((object) => object.tag_key);
  expect(tagged, "expected at least one tagged object in the fixture").toBeTruthy();
  const byTag = await request.get(
    `${API_ROOT}/documents/${document.id}/engineering-graph/trace?ref=${encodeURIComponent(tagged!.tag_key)}`,
  );
  expect(byTag.ok(), await byTag.text()).toBeTruthy();
  const resolved = await byTag.json() as { origin_engineering_id: string; resolved_from: string };
  expect(resolved.origin_engineering_id).toBe(tagged!.engineering_id);
  expect(resolved.resolved_from).toBe(tagged!.tag_key);

  const unknown = await request.get(
    `${API_ROOT}/documents/${document.id}/engineering-graph/trace?ref=equipment:does-not-exist`,
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

  // Once the index exists, an engineering object can be located project-wide by
  // identity, tag key or tag without knowing which drawing holds it.
  const graphAgain = await (await request.get(`${API_ROOT}/documents/${document.id}/engineering-graph`)).json() as {
    objects: Array<{ engineering_id: string; tag_key: string }>;
  };
  const target = graphAgain.objects.find((object) => object.tag_key)!;
  const found = await request.get(
    `${API_ROOT}/project/engineering-objects?ref=${encodeURIComponent(target.engineering_id)}`,
  );
  expect(found.ok(), await found.text()).toBeTruthy();
  const matches = await found.json() as Array<{ document_id: string; engineering_id: string }>;
  expect(matches.map((match) => match.document_id)).toContain(document.id);
  expect(matches.some((match) => match.engineering_id === target.engineering_id)).toBe(true);

  // The rebuild is recorded as its own audit fact and keeps the chain valid.
  const audit = await request.get(`${API_ROOT}/audit/records?event_type=engineering.index.rebuilt&limit=5`);
  expect(audit.ok()).toBeTruthy();
  expect((await audit.json() as unknown[]).length).toBeGreaterThan(0);
  const verification = await request.get(`${API_ROOT}/audit/verify`);
  expect((await verification.json() as { ok: boolean }).ok).toBe(true);
});
