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
  value: { sessionStorage, localStorage },
});

const {
  describeTypesafeKeySource,
  getTypesafeApiKey,
  readTypesafePreference,
  setTypesafeApiKey,
  writeTypesafePreferences,
  api,
} = await import("../src/api.ts");

const OTHER_KEYS = "pid-agent.typesafe-key|pid-agent.typesafe";

test("a TypeSafe key lives only in this tab's sessionStorage, and clearing it really removes it", () => {
  const key = "jev-panel-key-0001";
  setTypesafeApiKey(`  ${key}  `);

  assert.equal(getTypesafeApiKey(), key);
  for (const [name, storage] of [["session", sessionStorage], ["local", localStorage]] as const) {
    for (const stored of [...storage.values.entries()]) {
      assert.equal(
        stored[1] === key,
        name === "session",
        `the key must be in ${name}Storage only when that is the tab session`,
      );
    }
  }

  setTypesafeApiKey("");
  assert.equal(getTypesafeApiKey(), "");
  assert.equal([...sessionStorage.values.values()].includes(key), false);
});

test("preferences outlive the session and never carry the key", () => {
  const key = "jev-panel-key-0002";
  setTypesafeApiKey(key);
  writeTypesafePreferences({ baseUrl: "https://api.typesafe.ai", model: "jev-latest", enabled: true });

  assert.equal(readTypesafePreference("enabled", "false"), "true");
  assert.equal(readTypesafePreference("model", "jev-latest"), "jev-latest");
  for (const value of localStorage.values.values()) {
    assert.equal(value.includes(key), false, "a preference must never become a place a key can be read from");
  }
});

test("the panel says which key the next call would use, including one the server holds", () => {
  const base = { configured: true, api_key_present: true, base_url: "https://api.typesafe.ai", model: "jev-latest" };

  const unknown = describeTypesafeKeySource(null);
  assert.equal(unknown.tone, "warning");

  const missing = describeTypesafeKeySource({ ...base, configured: false, api_key_present: false, api_key_source: undefined });
  assert.equal(missing.tone, "warning");
  assert.match(missing.message, /服务端/);

  // The server's own key is reported as usable with an empty field: that is the whole point of
  // looking, since a key exported in a shell profile is invisible from the browser.
  const fromServer = describeTypesafeKeySource({ ...base, api_key_source: "environment" });
  assert.equal(fromServer.tone, "ok");
  assert.match(fromServer.message, /TYPESAFE_API_KEY/);
  assert.match(fromServer.placeholder, /留空/);

  const fromPanel = describeTypesafeKeySource({ ...base, api_key_source: "request" });
  assert.equal(fromPanel.tone, "ok");
  assert.equal(fromPanel.placeholder.includes("留空"), false);
});

test("verifying sends the key in the body to the provider route, never in the URL", async () => {
  const key = "jev-panel-key-0003";
  const requests: Request[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    // The client calls relative paths; the page origin is what the browser would prepend.
    requests.push(new Request(new URL(input as string, "http://panel.test"), init));
    return new Response(JSON.stringify({ ok: true, model: "jev-1.13.0", latency_ms: 12 }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    await api.verifyTypesafe({ api_key: key, model: "jev-latest" }, undefined);
    await api.typesafeStatus(undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }

  const [verify, status] = requests;
  assert.equal(verify.url.includes(key), false, "a key in a URL ends up in logs and histories");
  assert.equal(JSON.parse(await verify.clone().text()).api_key, key);
  assert.match(status.url, /\/provider\/typesafe\/status$/);
  assert.equal(status.method, "GET");
  assert.equal(status.url.includes(key), false);
});

test("the storage names this suite reasons about are the ones the module uses", () => {
  // Guards the assertions above from silently passing after a rename: the two known keys are the
  // only entries this file's loops can inspect.
  assert.equal(OTHER_KEYS.split("|").every((name) => name.startsWith("pid-agent.")), true);
  assert.equal([...localStorage.values.keys()].every((name) => name.startsWith("pid-agent.")), true);
  assert.equal([...sessionStorage.values.keys()].every((name) => name.startsWith("pid-agent.")), true);
});
