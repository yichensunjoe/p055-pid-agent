import assert from "node:assert/strict";
import { describe, it } from "node:test";

/**
 * The CAD import talks to the backend over a raw-body POST. These tests pin the request
 * shape the backend reads: the URL carries the decisions (file name, crop window, skipped
 * layers) and the body is the file itself, never JSON.
 *
 * A stubbed `fetch` is the only way to observe that, and `api.ts` stays importable as a
 * self-contained module precisely so it can be loaded here.
 */

class MemoryStorage {
  values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: { sessionStorage: new MemoryStorage(), localStorage: new MemoryStorage() },
});

const { api, cadImportQuery } = await import("../src/api.ts");

function fakeFile(name: string, contents: string): File {
  return new File([contents], name, { type: "application/octet-stream" });
}

async function capture(run: () => Promise<unknown>): Promise<Request> {
  const originalFetch = globalThis.fetch;
  let request: Request | undefined;
  globalThis.fetch = async (input, init) => {
    // The app fetches relative paths (the browser resolves them against the page); node's
    // Request needs an absolute URL, so the test does the resolving instead.
    const url = new URL(input instanceof Request ? input.url : String(input), "http://localhost");
    request = new Request(url, init);
    // The body is read so the assertion sees the bytes the server would receive.
    if (request.body) await request.clone().arrayBuffer();
    return new Response(JSON.stringify({}), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  try {
    await run();
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.ok(request, "no request was issued");
  return request;
}

describe("cadImportQuery", () => {
  it("always names the source file and omits defaults", () => {
    const query = new URLSearchParams(cadImportQuery("气路系统总图.dwg"));
    assert.equal(query.get("filename"), "气路系统总图.dwg");
    assert.equal(query.get("fills"), null);
    assert.equal(query.get("include_text"), null);
    assert.equal(query.get("unit_scale"), null);
    assert.equal(query.get("curve_segments"), null);
  });

  it("sends only the options that were actually changed", () => {
    const query = new URLSearchParams(cadImportQuery("sheet.dxf", {
      frame: [0, 0, 120, 60],
      fills: "skip",
      includeText: false,
      unitScale: 2,
      curveSegments: 8,
      preserveColors: false,
      name: "复现图",
      layers: ["管道", "仪表"],
    }));
    assert.equal(query.get("frame"), "0,0,120,60");
    assert.equal(query.get("fills"), "skip");
    assert.equal(query.get("include_text"), "false");
    assert.equal(query.get("unit_scale"), "2");
    assert.equal(query.get("curve_segments"), "8");
    assert.equal(query.get("preserve_colors"), "false");
    assert.equal(query.get("name"), "复现图");
    assert.equal(query.get("layers"), "管道,仪表");
  });

  it("falls back to a filename when the browser gives none", () => {
    assert.equal(new URLSearchParams(cadImportQuery("")).get("filename"), "drawing");
  });
});

describe("api.importCadDrawing", () => {
  it("posts the file bytes to /imports/cad with the file name in the query", async () => {
    const file = fakeFile("气路系统总图.dwg", "AC1024-fake-dwg");
    const request = await capture(() => api.importCadDrawing(file));

    assert.equal(request.method, "POST");
    const url = new URL(request.url);
    assert.equal(url.pathname, "/api/v2/imports/cad");
    assert.equal(url.searchParams.get("filename"), "气路系统总图.dwg");
    assert.equal(request.headers.get("Content-Type"), "application/octet-stream");
    assert.equal(await request.text(), "AC1024-fake-dwg");
  });

  it("passes the crop window and skipped layers through the query", async () => {
    const request = await capture(() => api.importCadDrawing(fakeFile("sheet.dxf", "0\nSECTION\n"), {
      name: "复现图",
      frame: [100, 200, 400, 500],
      layers: ["管道"],
      fills: "skip",
    }));

    const url = new URL(request.url);
    assert.equal(url.searchParams.get("frame"), "100,200,400,500");
    assert.equal(url.searchParams.get("layers"), "管道");
    assert.equal(url.searchParams.get("fills"), "skip");
    assert.equal(url.searchParams.get("name"), "复现图");
  });
});

describe("api.planCadDrawing", () => {
  it("uploads to the dry-run route so nothing can be written", async () => {
    const request = await capture(() => api.planCadDrawing(fakeFile("sheet.dxf", "0\nSECTION\n")));
    const url = new URL(request.url);
    assert.equal(url.pathname, "/api/v2/imports/cad/plan");
    assert.equal(url.searchParams.get("filename"), "sheet.dxf");
  });
});

describe("api.cadImportCapabilities", () => {
  it("reads the capability probe", async () => {
    const request = await capture(() => api.cadImportCapabilities());
    assert.equal(new URL(request.url).pathname, "/api/v2/imports/cad/capabilities");
    assert.equal(request.method, "GET");
  });
});
