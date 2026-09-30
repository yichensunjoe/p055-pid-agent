import assert from "node:assert/strict";
import test from "node:test";

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

const sessionStorage = new MemoryStorage();
const localStorage = new MemoryStorage();
Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: { sessionStorage, localStorage, setTimeout: globalThis.setTimeout },
});

const apiModule = await import("../src/api.ts");
const {
  clearServiceAccessToken,
  downloadCableExport,
  fetchCableDetail,
  fetchCableDocuments,
  setServiceAccessToken,
} = apiModule;

test("cable read client attaches the service token to list and detail", async () => {
  setServiceAccessToken("cable-session-token");
  const seen: Array<{ url: string; auth: string | null }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    seen.push({
      url: String(input),
      auth: headers.get("authorization"),
    });
    return new Response(
      JSON.stringify(
        String(input).endsWith("/documents")
          ? [{ document_id: "cab_x", name: "n", revision: 0, readiness_state: "eligible" }]
          : {
              document_id: "cab_x",
              revision: 0,
              schema: "pid-agent.cable-document/1",
              name: "n",
              segments: [],
              readiness: {
                state: "not_eligible",
                counts: {},
                reasons: [],
                result_hash: "h",
                profile_id: "cable-built-in",
                profile_version: 1,
                profile_fingerprint: "f",
              },
            },
      ),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };
  try {
    const list = await fetchCableDocuments();
    assert.equal(list.length, 1);
    assert.equal(list[0].readiness_state, "eligible");
    const detail = await fetchCableDetail("cab_x");
    assert.equal(detail.readiness.profile_id, "cable-built-in");
    assert.deepEqual(
      seen.map((entry) => entry.auth),
      ["Bearer cable-session-token", "Bearer cable-session-token"],
    );
    assert.ok(seen.every((entry) => entry.url.startsWith("/api/v2/cable/")));
  } finally {
    globalThis.fetch = originalFetch;
    clearServiceAccessToken();
  }
});

test("cable export uses the authenticated download flow", async () => {
  setServiceAccessToken("cable-session-token");
  const seen: Array<{ url: string; auth: string | null }> = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    seen.push({ url: String(input), auth: headers.get("authorization") });
    return new Response(new Uint8Array([80, 75, 3, 4]), {
      status: 200,
      headers: {
        "Content-Type": "application/zip",
        "Content-Disposition": 'attachment; filename="cab_x-r0.zip"',
      },
    });
  };
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;
  URL.createObjectURL = () => "blob:mock";
  URL.revokeObjectURL = () => undefined;
  let clicked: string | null = null;
  Object.defineProperty(globalThis, "document", {
    configurable: true,
    value: {
      createElement: (tag: string) => {
        if (tag !== "a") return { style: {} };
        return {
          set href(value: string) {
            clicked = value;
          },
          get href() {
            return clicked ?? "";
          },
          download: "",
          click: () => undefined,
        };
      },
      body: { appendChild: () => undefined, removeChild: () => undefined },
    },
  });
  try {
    await downloadCableExport("cab_x", 0);
    assert.equal(seen.length, 1);
    assert.ok(seen[0].url.includes("/api/v2/cable/documents/cab_x/export.zip?expected_revision=0"));
    assert.equal(seen[0].auth, "Bearer cable-session-token");
    assert.equal(clicked, "blob:mock");
  } finally {
    globalThis.fetch = originalFetch;
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    clearServiceAccessToken();
  }
});
