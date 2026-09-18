import { expect, type APIRequestContext, type Page } from "@playwright/test";

// The suite addresses the backend directly (create/reset documents) as well as through
// the preview proxy. Defaults match playwright.config.ts; the override exists so the
// suite can run against a scratch backend on a port that is not the default one.
export const API_ROOT =
  process.env.PID_AGENT_E2E_API_ROOT ?? "http://127.0.0.1:8000/api/v2";

export type Point = { x: number; y: number };
export type TestDocument = {
  id: string;
  name: string;
  revision: number;
  elements: Array<Record<string, any>>;
  canvas: { width: number; height: number; grid_size: number; background: string };
};

export const style = (stroke = "#111827", fill = "none") => ({
  stroke,
  fill,
  stroke_width: 1.5,
  opacity: 1,
  dash: [],
});

export const symbol = (
  id: string,
  symbolKey: string,
  position: Point,
  width: number,
  height: number,
  label: string,
  metadata: Record<string, unknown> = {},
) => ({
  id,
  type: "symbol",
  symbol_key: symbolKey,
  position,
  width,
  height,
  rotation: 0,
  label,
  properties: {},
  layer_id: "layer_default",
  system_id: "system_default",
  style: style(),
  name: label,
  metadata,
});

export const connector = (
  id: string,
  points: Point[],
  source: { element_id?: string; port_id?: string; point: Point },
  target: { element_id?: string; port_id?: string; point: Point },
  metadata: Record<string, unknown> = {},
) => ({
  id,
  type: "connector",
  points,
  source,
  target,
  routing: "manual",
  process_tag: `L-${id.toUpperCase()}`,
  medium: "process",
  nominal_diameter: "DN50",
  flow_direction: "forward",
  arrow_position: "middle",
  crossing_style: "jump",
  jump_radius: 7,
  layer_id: "layer_default",
  system_id: "system_default",
  style: style("#0f172a"),
  name: id,
  metadata,
});

export const junction = (id: string, position: Point, metadata: Record<string, unknown> = {}) => ({
  id,
  type: "junction",
  position,
  radius: 5,
  label: id.toUpperCase(),
  layer_id: "layer_default",
  system_id: "system_default",
  style: style(),
  name: id,
  metadata,
});

export async function resetDocuments(request: APIRequestContext): Promise<void> {
  const response = await request.get(`${API_ROOT}/documents`);
  expect(response.ok()).toBeTruthy();
  const documents = await response.json() as Array<{ id: string; revision: number }>;
  for (const document of documents) {
    const removed = await request.delete(
      `${API_ROOT}/documents/${document.id}?expected_revision=${encodeURIComponent(document.revision)}`,
    );
    expect(removed.ok(), await removed.text()).toBeTruthy();
  }
}

export async function createDocument(
  request: APIRequestContext,
  name: string,
  operations: Array<Record<string, unknown>> = [],
  size: { width: number; height: number } = { width: 1600, height: 900 },
): Promise<TestDocument> {
  const created = await request.post(`${API_ROOT}/documents`, {
    data: { name, width: size.width, height: size.height, metadata: { e2e_fixture: true } },
  });
  expect(created.ok()).toBeTruthy();
  let document = await created.json() as TestDocument;
  if (operations.length) {
    const applied = await request.post(`${API_ROOT}/documents/${document.id}/transactions`, {
      data: { expected_revision: document.revision, operations, label: "Seed Playwright fixture" },
    });
    expect(applied.ok(), await applied.text()).toBeTruthy();
    document = (await applied.json() as { document: TestDocument }).document;
  }
  return document;
}

export async function getDocument(request: APIRequestContext, id: string): Promise<TestDocument> {
  const response = await request.get(`${API_ROOT}/documents/${id}`);
  expect(response.ok()).toBeTruthy();
  return response.json() as Promise<TestDocument>;
}

export async function openDocument(page: Page, id: string): Promise<void> {
  await page.goto("/");
  await page.waitForFunction(() => Boolean(window.__PID_AGENT_E2E__));
  await page.evaluate(async (documentId) => window.__PID_AGENT_E2E__!.openDocument(documentId), id);
  await expect(page.getByTestId("app-shell")).toHaveAttribute("data-document-id", id);
  await expect(page.getByTestId("editor-canvas")).toBeVisible();
  // The project bootstrap runs independently from the document bridge. Wait for the
  // deterministic E2E project settings and workspace controls so visual captures do
  // not race an intermediate shell state.
  await expect(page.locator(".sync-badge.sync-synced")).toBeVisible();
  await expect(page.getByTestId("project-summary")).toBeVisible();
  await expect(page.getByTestId("import-document-json")).toBeVisible();
  await expect(page.getByTestId("export-project-package")).toBeVisible();
}

export async function workspaceSnapshot(page: Page): Promise<any> {
  return page.evaluate(() => window.__PID_AGENT_E2E__!.snapshot());
}

export async function selectElements(page: Page, ids: string[]): Promise<void> {
  await page.evaluate((elementIds) => window.__PID_AGENT_E2E__!.select(elementIds), ids);
  await expect.poll(async () => (await workspaceSnapshot(page)).selectedElementIds).toEqual(ids);
}

export async function transact(page: Page, operations: Array<Record<string, unknown>>, label: string): Promise<void> {
  await page.evaluate(
    async ({ nextOperations, nextLabel }) => window.__PID_AGENT_E2E__!.transact(nextOperations as any, nextLabel),
    { nextOperations: operations, nextLabel: label },
  );
}

export async function dragLocator(page: Page, selector: string, dx: number, dy: number): Promise<void> {
  const locator = page.locator(selector);
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2 + dx, box!.y + box!.height / 2 + dy, { steps: 8 });
  await page.mouse.up();
}

export function baseEngineeringOperations(): Array<Record<string, unknown>> {
  return [
    { op: "add_element", element: symbol("tank", "gas_tank", { x: 140, y: 180 }, 90, 140, "V-101") },
    { op: "add_element", element: symbol("pump", "centrifugal_pump", { x: 650, y: 220 }, 80, 70, "P-101") },
    {
      op: "add_element",
      element: connector(
        "main",
        [{ x: 230, y: 250 }, { x: 420, y: 250 }, { x: 420, y: 258 }, { x: 650, y: 258 }],
        { element_id: "tank", port_id: "out", point: { x: 230, y: 250 } },
        { element_id: "pump", port_id: "suction", point: { x: 650, y: 258 } },
        { main_route_id: "route-main" },
      ),
    },
    { op: "add_element", element: junction("j1", { x: 420, y: 250 }, { main_route_id: "route-main" }) },
    {
      op: "add_element",
      element: connector(
        "branch",
        [{ x: 420, y: 250 }, { x: 420, y: 100 }, { x: 760, y: 100 }],
        { element_id: "j1", port_id: "node", point: { x: 420, y: 250 } },
        { point: { x: 760, y: 100 } },
        { main_route_id: "route-main" },
      ),
    },
    { op: "add_element", element: symbol("valve", "gate_valve", { x: 430, y: 430 }, 60, 50, "XV-101") },
    {
      op: "add_element",
      element: {
        id: "obstacle",
        type: "rectangle",
        x: 350,
        y: 200,
        width: 110,
        height: 100,
        corner_radius: 6,
        layer_id: "layer_default",
        system_id: "system_default",
        style: style("#64748b", "#e2e8f0"),
        name: "Obstacle",
        metadata: {},
      },
    },
  ];
}
