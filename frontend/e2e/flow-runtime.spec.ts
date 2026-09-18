import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import {
  API_ROOT,
  connector,
  createDocument,
  junction,
  openDocument,
  resetDocuments,
  selectElements,
  symbol,
} from "./fixtures";

const enableRuntime = () => {
  sessionStorage.setItem("pid-agent:enable-runtime-e2e", "true");
};

test.beforeEach(async ({ page, request }) => {
  await resetDocuments(request);
  await page.addInitScript(enableRuntime);
});

test("green flow overlay and valve state remain visible without blockage errors", async ({ page, request }) => {
  const source = junction("source", { x: 280, y: 460 });
  const closedValve = {
    ...symbol("valve", "gate_valve", { x: 430, y: 430 }, 60, 50, "XV-101"),
    properties: { valve_state: "closed" },
  };
  const sink = junction("sink", { x: 640, y: 460 });
  const inlet = {
    ...connector(
      "line-in",
      [{ x: 280, y: 460 }, { x: 430, y: 460 }],
      { element_id: "source", port_id: "node", point: { x: 280, y: 460 } },
      { element_id: "valve", port_id: "in", point: { x: 430, y: 460 } },
      { main_route_id: "route-water" },
    ),
    medium: "water",
  };
  const outlet = {
    ...connector(
      "line-out",
      [{ x: 490, y: 460 }, { x: 640, y: 460 }],
      { element_id: "valve", port_id: "out", point: { x: 490, y: 460 } },
      { element_id: "sink", port_id: "node", point: { x: 640, y: 460 } },
      { main_route_id: "route-water" },
    ),
    medium: "water",
  };
  const document = await createDocument(request, "Flow runtime", [
    { op: "add_element", element: source },
    { op: "add_element", element: closedValve },
    { op: "add_element", element: sink },
    { op: "add_element", element: inlet },
    { op: "add_element", element: outlet },
  ]);

  await openDocument(page, document.id);

  await expect(page.locator('[aria-label="左侧工作区分区"]')).toBeVisible();
  const inletFlow = page.locator('[data-flow-for="line-in"]');
  await expect(inletFlow).toHaveClass(/medium-water/);
  await expect(inletFlow).toHaveCSS("stroke", "rgb(22, 163, 74)");
  await expect(inletFlow).toHaveCSS("stroke-width", "4.2px");
  await expect(page.locator('[data-flow-for="line-out"]')).toHaveClass(/medium-water/);
  await expect(page.locator('[data-valve-state-for="valve"]')).toHaveClass(/state-closed/);
  await expect(page.getByRole("alert")).toHaveCount(0);

  await selectElements(page, ["valve"]);
  const panel = page.getByRole("complementary", { name: "工艺流向状态" });
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("不再生成阻断报错");
  await panel.getByRole("button", { name: "开", exact: true }).click();

  await expect(page.locator('[data-valve-state-for="valve"]')).toHaveClass(/state-open/);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("OPC double click opens its linked P&ID and offers return navigation", async ({ page, request }) => {
  const target = await createDocument(request, "Target P&ID");
  const opc = {
    ...symbol("opc", "off_page_connector_out", { x: 500, y: 300 }, 100, 50, "TO TARGET"),
    properties: {
      opc_direction: "out",
      target_document_id: target.id,
    },
  };
  const source = await createDocument(request, "Source P&ID", [
    { op: "add_element", element: opc },
  ]);

  await openDocument(page, source.id);
  const jumpTarget = page.locator('[data-opc-jump-for="opc"]');
  await expect(jumpTarget).toBeVisible();
  await jumpTarget.dblclick();

  await expect(page.getByTestId("app-shell")).toHaveAttribute("data-document-id", target.id);
  await expect(page.getByRole("button", { name: "返回上一张 P&ID" })).toBeVisible();
});

test("renames the current P&ID and downloads a real PNG", async ({ page, request }) => {
  // Renaming and PNG export live in the runtime enhancements, which the E2E bundle keeps
  // off by default so the rest of the suite sees the plain editor. The opt-in is read from
  // sessionStorage on every navigation, so it has to be set before the page loads — without
  // it the controls simply are not rendered and this scenario times out waiting for them.
  await page.addInitScript(() => {
    window.sessionStorage.setItem("pid-agent:enable-runtime-e2e", "true");
  });
  const document = await createDocument(request, "Original P&ID", [
    {
      op: "add_element",
      element: symbol("pump", "centrifugal_pump", { x: 420, y: 320 }, 100, 70, "P-101"),
    },
  ]);
  await openDocument(page, document.id);

  await page.getByRole("button", { name: "重命名" }).click();
  const renameDialog = page.getByRole("dialog", { name: "重命名 P&ID 图纸" });
  await expect(renameDialog).toBeVisible();
  await renameDialog.getByRole("textbox", { name: "图纸名称" }).fill("Feed Preparation P&ID");
  await renameDialog.getByRole("button", { name: "保存" }).click();
  await expect(page.locator(".document-bar strong")).toHaveText("Feed Preparation P&ID");

  const persisted = await request.get(`${API_ROOT}/documents/${document.id}`);
  expect(persisted.ok(), await persisted.text()).toBeTruthy();
  expect((await persisted.json() as { name: string; revision: number }).name).toBe("Feed Preparation P&ID");

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 PNG" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/\.png$/);
  const path = await download.path();
  expect(path).not.toBeNull();
  const png = await readFile(path!);
  expect(png.subarray(0, 8)).toEqual(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
});

test("shape variety dropdown renders fully above the canvas without scrolling", async ({ page, request }) => {
  const document = await createDocument(request, "Toolbar menu");
  await openDocument(page, document.id);

  await page.getByTitle(/矩形 \(R\)/).click();
  const menu = page.locator(".tool-menu");
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem")).toHaveCount(3);

  const box = await menu.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(page.viewportSize()!.height);
  const topElementIsMenu = await menu.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    const top = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    return Boolean(top && element.contains(top));
  });
  expect(topElementIsMenu).toBe(true);
  expect(await menu.evaluate((element) => element.scrollHeight)).toBe(await menu.evaluate((element) => element.clientHeight));
});
