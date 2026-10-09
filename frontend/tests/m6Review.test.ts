import assert from "node:assert/strict";
import test from "node:test";

// M6-2B-D3: the review client attaches the service token, addresses exactly the three
// registered routes, and surfaces the server's stable refusal codes.

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
  fetchM6CandidateDetail,
  fetchM6Candidates,
  postM6Decision,
  setServiceAccessToken,
} = apiModule;

function stubFetch(seen: Array<{ url: string; method: string; auth: string | null; body: string }>) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const headers = new Headers(init?.headers);
    const url = String(input);
    const path = url.split("?")[0];
    seen.push({
      url,
      method: init?.method ?? "GET",
      auth: headers.get("authorization"),
      body: typeof init?.body === "string" ? init.body : "",
    });
    if (path.endsWith("/m6/candidates")) {
      return new Response(
        JSON.stringify({
          document_id: "doc_src",
          source: { available: true, code: "", revision: 3 },
          total: 1,
          offset: 0,
          limit: 200,
          candidates: [
            {
              candidate_id: "cand_m6_x",
              candidate_type: "equipment_tag",
              review_status: "needs_review",
              proposed_semantics: { equipment_tag: "P-201" },
              region_id: "m6reg_x",
              target_document_id: "doc_target",
              producer: { key: "deterministic_rule_engine", version: "0.1.0" },
              confidence: 1,
              evidence_count: 2,
              created_at: "2026-10-09T00:00:00Z",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (path.includes("/decisions")) {
      return new Response(
        JSON.stringify({
          candidate_id: "cand_m6_x",
          action: "confirm",
          changed: true,
          review_decision_id: "m6dec_x",
          status: "confirmed",
          finding_id: "m6find_x",
          successor_candidate_id: "",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(
      JSON.stringify({
        candidate: { candidate_id: "cand_m6_x", candidate_type: "equipment_tag" },
        review_status: "needs_review",
        decisions: [],
        source: { available: true, code: "", revision: 3 },
        evidence_elements: [],
        target: { target_document_id: "doc_target", exists: true, revision: 1 },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  };
  return originalFetch;
}

test("the M6 review client addresses the three registered routes with the service token", async () => {
  setServiceAccessToken("m6-session-token");
  const seen: Array<{ url: string; method: string; auth: string | null; body: string }> = [];
  const originalFetch = stubFetch(seen);
  try {
    const queue = await fetchM6Candidates("doc_src", { status: "needs_review", limit: 50 });
    assert.equal(queue.candidates.length, 1);
    assert.equal(queue.candidates[0].review_status, "needs_review");

    const detail = await fetchM6CandidateDetail("doc_src", "cand_m6_x");
    assert.equal(detail.review_status, "needs_review");

    const outcome = await postM6Decision("doc_src", "cand_m6_x", {
      action: "confirm",
      reviewer_identity: "engineer.joe",
      reviewer_action: "核对了原图",
    });
    assert.equal(outcome.status, "confirmed");
    assert.equal(outcome.finding_id, "m6find_x");

    assert.deepEqual(
      seen.map((entry) => entry.auth),
      ["Bearer m6-session-token", "Bearer m6-session-token", "Bearer m6-session-token"],
    );
    assert.equal(seen[0].url, "/api/v2/documents/doc_src/m6/candidates?status=needs_review&limit=50");
    assert.equal(seen[1].url, "/api/v2/documents/doc_src/m6/candidates/cand_m6_x");
    assert.equal(seen[2].url, "/api/v2/documents/doc_src/m6/candidates/cand_m6_x/decisions");
    assert.equal(seen[2].method, "POST");
    const decisionBody = JSON.parse(seen[2].body);
    assert.equal(decisionBody.action, "confirm");
    assert.equal(decisionBody.reviewer_identity, "engineer.joe");
  } finally {
    globalThis.fetch = originalFetch;
    clearServiceAccessToken();
  }
});

test("the M6 client surfaces the server's stable refusal code", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        detail: { code: "source_revision_drift", message: "pinned revision moved" },
      }),
      { status: 409, headers: { "Content-Type": "application/json" } },
    );
  try {
    await assert.rejects(
      () =>
        postM6Decision("doc_src", "cand_m6_x", {
          action: "reject",
          reviewer_identity: "engineer.joe",
          reviewer_action: "拒绝",
        }),
      (error: unknown) => {
        assert.ok(error instanceof apiModule.ApiError);
        assert.equal((error as InstanceType<typeof apiModule.ApiError>).code, "source_revision_drift");
        assert.equal((error as InstanceType<typeof apiModule.ApiError>).status, 409);
        return true;
      },
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
