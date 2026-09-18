import { expect, test, type Page } from "@playwright/test";
import {
  API_ROOT,
  createDocument,
  getDocument,
  openDocument,
  resetDocuments,
  workspaceSnapshot,
} from "./fixtures";

/**
 * CAD (DWG/DXF) intake through the editor.
 *
 * The fixture is a hand-written ASCII DXF for the same reason the backend fixtures are:
 * a test can state exactly the records it wants. It carries two named layers, a line, a
 * polyline, a circle and a text, which is the smallest drawing that can prove layers and
 * element kinds survived the round trip.
 */
function dxfStream(): string {
  const lines: string[] = [
    "0", "SECTION", "2", "HEADER",
    "9", "$ACADVER", "1", "AC1032",
    "9", "$DWGCODEPAGE", "3", "UTF-8",
    "9", "$EXTMIN", "10", "100.0", "20", "100.0", "30", "0.0",
    "9", "$EXTMAX", "10", "600.0", "20", "400.0", "30", "0.0",
    "0", "ENDSEC",
    "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER",
    "0", "LAYER", "2", "0", "70", "0", "62", "7", "6", "CONTINUOUS",
    "0", "LAYER", "2", "管道", "70", "0", "62", "3", "6", "CONTINUOUS",
    "0", "LAYER", "2", "仪表", "70", "0", "62", "2", "6", "CONTINUOUS",
    "0", "ENDTAB", "0", "ENDSEC",
    "0", "SECTION", "2", "ENTITIES",
    // A process line, a bent pipe run, a vessel circle and its tag.
    "0", "LINE", "8", "管道", "10", "100.0", "20", "100.0", "11", "300.0", "21", "100.0", "30", "0.0",
    "0", "LWPOLYLINE", "8", "管道", "90", "3", "70", "0",
    "10", "300.0", "20", "100.0",
    "10", "300.0", "20", "250.0",
    "10", "500.0", "20", "250.0",
    "0", "CIRCLE", "8", "仪表", "10", "500.0", "20", "350.0", "30", "0.0", "40", "60.0",
    "0", "TEXT", "8", "仪表", "10", "500.0", "20", "350.0", "30", "0.0", "40", "24.0", "1", "PT-101",
    "0", "ENDSEC", "0", "EOF",
  ];
  return `${lines.join("\n")}\n`;
}

const dxfFile = {
  name: "气路系统总图.dxf",
  mimeType: "application/dxf",
  buffer: Buffer.from(dxfStream(), "utf-8"),
};

/**
 * Open the editor and wait for the workspace bootstrap to settle.
 *
 * Baseline document reads have to happen *after* the bootstrap, which creates its own
 * default document when the project is empty; otherwise "did this action write anything?"
 * would be answered by the bootstrap's write instead of by the request under test.
 */
async function openWorkspace(page: Page): Promise<void> {
  await page.goto("/");
  await page.waitForFunction(() => Boolean(window.__PID_AGENT_E2E__));
  await expect(page.locator(".sync-badge.sync-synced")).toBeVisible();
  await expect(page.getByTestId("app-shell")).toHaveAttribute("data-document-id", /^doc_/);
  await expect(page.getByTestId("command-palette-trigger")).toBeVisible();
}

async function documentList(
  request: Parameters<typeof createDocument>[0],
): Promise<Array<{ id: string; revision: number }>> {
  const response = await request.get(`${API_ROOT}/documents`);
  expect(response.ok()).toBeTruthy();
  return response.json() as Promise<Array<{ id: string; revision: number }>>;
}

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

test("imports a DXF through the editor, opens it, and leaves the other drawings alone", async ({ page, request }) => {
  // A neighbouring drawing exists so "the import created a document" cannot be confused
  // with "the import changed a document".
  const neighbour = await createDocument(request, "邻居图纸");
  await openDocument(page, neighbour.id);

  await page.setInputFiles('[data-testid="import-cad-input"]', dxfFile);

  // The report is the deliverable: it states the source, the counts, the layers and what
  // could not be reproduced rather than only confirming success.
  await expect(page.getByTestId("cad-import-report")).toBeVisible();
  await expect(page.getByTestId("cad-import-report-title")).toContainText("已导入");
  const report = page.getByTestId("cad-import-report-body");
  await expect(report).toContainText("气路系统总图.dxf");
  await expect(report).toContainText("DXF");
  await expect(report).toContainText("内置 DXF 读取器");
  await expect(report).toContainText("图元");
  await expect(report).toContainText("管道");
  await expect(report).toContainText("仪表");
  await expect(report).toContainText("未能复现");

  // The editor switched to the imported drawing and it is actually on the canvas.
  const snapshot = await workspaceSnapshot(page);
  expect(snapshot.document).not.toBeNull();
  expect(snapshot.document.id).not.toBe(neighbour.id);
  expect(snapshot.document.name).toContain("气路系统总图");
  expect(snapshot.document.elements.length).toBeGreaterThanOrEqual(4);
  expect(snapshot.documents.length).toBe(2);
  await expect(page.getByTestId("app-shell")).toHaveAttribute("data-document-id", snapshot.document.id);

  // The imported geometry is on the rendered sheet, not parked at the CAD origin. The
  // editor culls to the viewport, so fitting all content has to bring every element in.
  const canvas = page.getByTestId("editor-canvas");
  await expect(canvas).toHaveAttribute("data-rendered-elements", /[1-9]/);
  await page.getByTestId("command-palette-trigger").click();
  const palette = page.getByRole("dialog", { name: "命令面板" });
  await palette.getByPlaceholder("搜索命令、设备位号、管线标签或元素 ID").fill("适应全部");
  await palette.getByRole("option", { name: /适应全部内容/ }).click();
  await expect.poll(
    async () => Number(await canvas.getAttribute("data-rendered-elements")),
  ).toBeGreaterThanOrEqual(snapshot.document.elements.length);

  // Layers and geometry survive the backend round trip, not just the client cache.
  const imported = await getDocument(request, snapshot.document.id) as unknown as {
    layers: Array<{ name: string }>;
    elements: Array<{ type: string }>;
  };
  const layerNames = imported.layers.map((layer) => layer.name);
  expect(layerNames).toEqual(expect.arrayContaining(["管道", "仪表"]));
  const kinds = new Set(imported.elements.map((element: { type: string }) => element.type));
  expect(kinds.has("line")).toBe(true);
  expect(kinds.has("connector") || kinds.has("polyline")).toBe(true);

  const untouched = await getDocument(request, neighbour.id);
  expect(untouched.revision).toBe(neighbour.revision);
  expect(untouched.elements.length).toBe(0);
});

test("planning a CAD file reports the whole surface and writes nothing", async ({ page, request }) => {
  await openWorkspace(page);
  const before = await documentList(request);

  await page.setInputFiles('[data-testid="plan-cad-input"]', dxfFile);

  await expect(page.getByTestId("cad-import-report")).toBeVisible();
  await expect(page.getByTestId("cad-import-report-title")).toContainText("未写入");
  await expect(page.getByTestId("cad-import-report-body")).toContainText("管道");

  const after = await documentList(request);
  expect(after).toEqual(before);
});

test("refuses a non-CAD file before any upload happens", async ({ page, request }) => {
  const uploads: string[] = [];
  page.on("request", (outgoing) => {
    // The capability probe is a GET against the same prefix and is expected; only an
    // actual upload would mean the file was sent to the server.
    if (outgoing.method() === "POST" && outgoing.url().includes("/imports/cad")) uploads.push(outgoing.url());
  });
  await openWorkspace(page);
  const before = await documentList(request);

  await page.setInputFiles('[data-testid="import-cad-input"]', {
    name: "说明.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("这不是图纸", "utf-8"),
  });

  await expect(page.locator(".document-import-error")).toContainText("请选择 DWG 或 DXF");
  expect(uploads).toEqual([]);
  expect(await documentList(request)).toEqual(before);
});

test("explains DWG support from the capability probe instead of failing after upload", async ({ page, request }) => {
  const capabilities = await (await request.get(`${API_ROOT}/imports/cad/capabilities`)).json() as {
    dxf_import: boolean;
    dwg_import: boolean;
  };
  expect(capabilities.dxf_import).toBe(true);

  await openWorkspace(page);
  const before = await documentList(request);

  if (capabilities.dwg_import) {
    // This installation has a decoder; the hint is not shown and a DWG import is expected
    // to be attempted rather than refused up front.
    await expect(page.getByTestId("cad-import-hint")).toHaveCount(0);
    test.skip(true, "LibreDWG/ODA is installed on this machine; the missing-decoder path cannot be exercised");
  }

  await expect(page.getByTestId("cad-import-hint")).toContainText("未安装 DWG 解码器");

  await page.setInputFiles('[data-testid="import-cad-input"]', {
    name: "无解码器.dwg",
    mimeType: "application/acad",
    buffer: Buffer.from("AC1032-not-really-a-dwg", "utf-8"),
  });

  // The refusal names the missing capability, and no half-imported document is left behind.
  await expect(page.locator(".document-import-error")).toContainText("DWG");
  expect(await documentList(request)).toEqual(before);
});
