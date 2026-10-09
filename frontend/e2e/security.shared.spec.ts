import { expect, test, type APIRequestContext } from "@playwright/test";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

import { symbol } from "./fixtures";

const TOKEN = process.env.PID_AGENT_E2E_SHARED_TOKEN ?? "pid-agent-shared-e2e-token";
// The config's port override (PID_AGENT_E2E_API_PORT) has to reach the direct API calls too, or
// the suite talks to a different server than the browser does.
const API = `http://127.0.0.1:${process.env.PID_AGENT_E2E_API_PORT ?? 8000}/api/v2`;
const authorization = { Authorization: `Bearer ${TOKEN}` };

async function resetDocuments(request: APIRequestContext) {
  const listed = await request.get(`${API}/documents`, { headers: authorization });
  expect(listed.ok()).toBeTruthy();
  for (const document of (await listed.json()) as Array<{ id: string; revision: number }>) {
    const removed = await request.delete(
      `${API}/documents/${document.id}?expected_revision=${encodeURIComponent(document.revision)}`,
      { headers: authorization },
    );
    expect(removed.ok(), await removed.text()).toBeTruthy();
  }
}

test.beforeEach(async ({ request }) => {
  await resetDocuments(request);
});

test("shared deployment requires a token and keeps it tab-session scoped", async ({ page, request }) => {
  const unauthenticated = await request.get(`${API}/documents`);
  expect(unauthenticated.status()).toBe(401);
  expect((await unauthenticated.json()).detail.error).toBe("authentication_required");

  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("需要服务访问令牌");
  await page.locator(".service-access-settings").getByText("共享部署访问令牌").click();

  await page.getByTestId("service-token-input").fill("wrong-token");
  await page.getByTestId("service-token-apply").click();
  await expect(page.getByRole("alert")).toContainText("服务访问令牌错误");

  await page.getByTestId("service-token-input").fill(TOKEN);
  await page.getByTestId("service-token-apply").click();
  await expect(page.getByTestId("project-summary")).toContainText("0 个文档");

  expect(page.url()).not.toContain(TOKEN);
  expect(await page.evaluate(() => window.localStorage.getItem("pid-agent-service-token"))).toBeNull();
  expect(await page.evaluate(() => window.sessionStorage.getItem("pid-agent-service-token"))).toBe(TOKEN);

  await page.reload();
  await expect(page.getByTestId("project-summary")).toContainText("0 个文档");
  expect(await page.getByTestId("service-token-input").inputValue()).toBe(TOKEN);
  expect(page.url()).not.toContain(TOKEN);
});

test("shared provider policy blocks localhost, permits an allowlisted target, and preserves revision", async ({ page, request }) => {
  const created = await request.post(`${API}/documents`, {
    headers: authorization,
    data: { name: "Shared security E2E" },
  });
  expect(created.ok()).toBeTruthy();
  const document = (await created.json()) as { id: string; revision: number };

  await page.goto("/");
  await page.locator(".service-access-settings").getByText("共享部署访问令牌").click();
  await page.getByTestId("service-token-input").fill(TOKEN);
  await page.getByTestId("service-token-apply").click();
  await page.locator(`.document-open[data-document-id="${document.id}"]`).click();
  await page.waitForFunction(() => Boolean(window.__PID_AGENT_E2E__?.snapshot().document));

  await page.getByRole("tab", { name: "Agent" }).click();
  await page.locator(".agent-provider-settings").getByText(/模型服务与高级设置/).click();
  const baseUrl = page.getByRole("textbox", { name: "Base URL" });
  const model = page.getByRole("textbox", { name: /Model name/ });

  await baseUrl.fill("http://localhost:8999/v1");
  await model.fill("test-model");
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.locator(".provider-test-error").last()).toContainText("网络安全策略阻止");
  await expect(page.locator(".provider-test-error").last()).toContainText("loopback");

  await baseUrl.fill("http://127.0.0.1:8999/v1");
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.locator(".provider-test-success")).toContainText("连接成功");
  await expect(page.locator(".provider-test-success")).toContainText("test-model");

  await page.getByRole("combobox", { name: "服务预设" }).selectOption("openai-compatible");
  await expect(baseUrl).toHaveValue("https://api.openai.com/v1");
  await expect(model).toHaveValue("");

  const after = await request.get(`${API}/documents/${document.id}`, { headers: authorization });
  expect(after.ok()).toBeTruthy();
  expect((await after.json()).revision).toBe(document.revision);
});


// Frozen ⑩ of the Cable surface's 10-step e2e: the shared-mode auth boundary.
// The local-mode spec (cable.spec.ts) cannot host this step; here, against a
// real shared deployment, anonymous Cable requests must be rejected and the
// Cable UI list/detail/export must actually work with a service token.
test("shared deployment protects the Cable surface with the same token boundary", async ({ page, request }) => {
  // Seed one cable document directly into the shared e2e database.
  const databasePath = `test-results/pid-agent-shared-e2e-${process.ppid}.db`;
  const cableId = `cab_shared${Date.now().toString(36)}`;
  const database = new DatabaseSync(databasePath);
  const now = new Date().toISOString();
  const payload = JSON.stringify({
    schema: "pid-agent.cable-document/1",
    name: "shared 线缆",
    segments: [
      { id: "CBL-SHR", from_node: "MCC-1", to_node: "PMP-101", gauge: "4mm2" },
    ],
  });
  database
    .prepare(
      "INSERT INTO documents_registry (document_id, domain, created_at) VALUES (?, 'cable', ?)",
    )
    .run(cableId, now);
  database
    .prepare(
      "INSERT INTO cable_documents (document_id, revision, data_json, created_at, updated_at) VALUES (?, 1, ?, ?, ?)",
    )
    .run(cableId, payload, now, now);
  database.close();

  // Anonymous probes on the Cable plane are rejected exactly like the P&ID plane.
  const anonymousList = await request.get(`${API}/cable/documents`);
  expect(anonymousList.status()).toBe(401);
  const anonymousDetail = await request.get(`${API}/cable/documents/${cableId}`);
  expect(anonymousDetail.status()).toBe(401);
  const anonymousExport = await request.get(
    `${API}/cable/documents/${cableId}/export.zip?expected_revision=1`,
  );
  expect(anonymousExport.status()).toBe(401);

  // Browser + service token: the Cable UI list/detail/export actually works.
  await page.goto("/");
  await page.locator(".service-access-settings").getByText("共享部署访问令牌").click();
  await page.getByTestId("service-token-input").fill(TOKEN);
  await page.getByTestId("service-token-apply").click();
  await expect(page.getByTestId("project-summary")).toContainText("0 个文档");

  await page.getByRole("tab", { name: "线缆" }).click();
  await expect(page.getByTestId("cable-workspace")).toBeVisible();
  await expect(page.getByText("shared 线缆（r1 · eligible）")).toBeVisible();

  await page.getByText("shared 线缆（r1 · eligible）").click();
  await expect(page.getByTestId("cable-detail")).toBeVisible();
  await expect(page.getByTestId("cable-detail").getByText("eligible")).toBeVisible();

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "导出确定性包" }).click(),
  ]);
  const authedExport = await request.get(
    `${API}/cable/documents/${cableId}/export.zip?expected_revision=1`,
    { headers: authorization },
  );
  expect(authedExport.status()).toBe(200);
  expect(readFileSync((await download.path())!)).toEqual(
    Buffer.from(await authedExport.body()),
  );
});


// M12-D5: the project inspection surface rides the same shared-mode token
// boundary as every other route — anonymous probes are rejected, never
// bypassed.
test("shared deployment protects the project inspection surface", async ({ request }) => {
  const base = `${API}/projects/proj_m12default`;
  for (const path of [
    `${base}`,
    `${base}/links`,
    `${base}/readiness?evaluation_as_of=${encodeURIComponent("2026-10-03T12:00:00+00:00")}`,
    `${base}/package.zip?evaluation_as_of=${encodeURIComponent("2026-10-03T12:00:00+00:00")}`,
    // M13-D5: the change-set read surface rides the same boundary
    `${base}/change-sets`,
    `${base}/change-sets/cs_nope`,
  ]) {
    const anonymous = await request.get(path);
    expect(anonymous.status(), path).toBe(401);
  }

  // M13-D5: the governed write endpoints are also token-gated — an anonymous
  // caller hits the auth boundary before any staged-row or apply logic.
  const anonymousStage = await request.post(`${base}/change-sets`, {
    data: { base_member_pins: {}, mutations: [] },
  });
  expect(anonymousStage.status()).toBe(401);
  const anonymousApply = await request.post(`${base}/change-sets/cs_nope/apply`);
  expect(anonymousApply.status()).toBe(401);

  // with a token the read surface actually works
  const summary = await request.get(base, { headers: authorization });
  expect(summary.status()).toBe(200);
  const readiness = await request.get(
    `${base}/readiness?evaluation_as_of=${encodeURIComponent("2026-10-03T12:00:00+00:00")}`,
    { headers: authorization },
  );
  expect(readiness.status()).toBe(200);
  const changeSets = await request.get(`${base}/change-sets`, { headers: authorization });
  expect(changeSets.status()).toBe(200);
});


// M13-D5 / D86-1: shared mode is not "fail closed and done" — with a token the
// full governed chain works: stage -> approval-request -> HUMAN resolve ->
// apply -> 200. The human decision can never be self-served by the endpoint.
test("shared deployment runs the full governed change-set chain with a human approval", async ({
  request,
}) => {
  const databasePath = `test-results/pid-agent-shared-e2e-${process.ppid}.db`;
  const now = new Date().toISOString();
  const asOf = "2026-10-06T12:00:00.000Z";

  // dual-domain fixture: pid member (with the linked equipment element) +
  // cable envelope + active link + memberships (link rows are D3-governed,
  // so they are SQL-seeded fixtures like the local change_set spec)
  const created = await request.post(`${API}/documents`, {
    headers: authorization,
    data: { name: "shared-cs-pid", width: 1600, height: 900 },
  });
  expect(created.ok()).toBeTruthy();
  const { id: pidId } = await created.json();
  const seeded = await request.post(`${API}/documents/${pidId}/transactions`, {
    headers: authorization,
    data: {
      expected_revision: 0,
      label: "seed",
      operations: [
        { op: "add_element", element: symbol("PMP-SHR", "agitator", { x: 200, y: 200 }, 60, 40, "PMP-SHR") },
      ],
    },
  });
  expect(seeded.ok()).toBeTruthy();
  const pidRevision = (
    await (await request.get(`${API}/documents/${pidId}`, { headers: authorization })).json()
  ).revision as number;
  const cableId = `cab_shrcs${Date.now().toString(36)}`;
  const database = new DatabaseSync(databasePath);
  database
    .prepare(
      "INSERT INTO documents_registry (document_id, domain, created_at) VALUES (?, 'cable', ?)",
    )
    .run(cableId, now);
  database
    .prepare(
      "INSERT INTO cable_documents (document_id, revision, data_json, created_at, updated_at) VALUES (?, 1, ?, ?, ?)",
    )
    .run(
      cableId,
      JSON.stringify({
        schema: "pid-agent.cable-document/1",
        name: "shared 线缆",
        segments: [{ id: "CBL-SHR", from_node: "MCC-1", to_node: "PMP-101" }],
      }),
      now,
      now,
    );
  database
    .prepare(
      "INSERT INTO project_documents (project_id, document_id, added_at, added_by) VALUES ('proj_m12default', ?, ?, 'e2e')",
    )
    .run(cableId, now);
  database
    .prepare(
      "INSERT INTO project_documents (project_id, document_id, added_at, added_by) VALUES ('proj_m12default', ?, ?, 'e2e')",
    )
    .run(pidId, now);
  database
    .prepare(
      "INSERT INTO engineering_links ("
        + " link_id, project_id, relation_type, source_domain, source_document_id,"
        + " source_object_ref, source_endpoint, target_domain, target_document_id,"
        + " target_object_ref, pinned_source_revision, pinned_target_revision,"
        + " created_at, created_by"
        + ") VALUES ('lnk_shrcs', 'proj_m12default', 'cable_endpoint_equipment', 'cable', ?,"
        + " 'CBL-SHR', 'from', 'pid', ?, 'PMP-SHR', 1, ?, ?, 'e2e')",
    )
    .run(cableId, pidId, pidRevision, now);
  database.close();

  const base = `${API}/projects/proj_m12default`;
  const staged = await request.post(`${base}/change-sets`, {
    headers: authorization,
    data: {
      base_member_pins: { [pidId]: pidRevision, [cableId]: 1 },
      evaluation_as_of: asOf,
      mutations: [
        {
          domain: "pid",
          document_id: pidId,
          kind: "pid_transaction",
          payload: {
            operations: [
              { op: "update_element", element_id: "PMP-SHR", patch: { label: "PMP-SHR-A" } },
            ],
          },
        },
        {
          domain: "cable",
          document_id: cableId,
          kind: "cable_update",
          payload: {
            schema: "pid-agent.cable-document/1",
            name: "shared 线缆",
            segments: [
              { id: "CBL-SHR", from_node: "MCC-1", to_node: "PMP-101", gauge: "6mm2" },
            ],
          },
        },
      ],
    },
  });
  expect(staged.status()).toBe(200);
  const { change_set_id: changeSetId } = await staged.json();

  const requested = await request.post(
    `${base}/change-sets/${changeSetId}/approval-requests`,
    { headers: authorization },
  );
  expect(requested.status()).toBe(200);
  const { session_id: sessionId, approval_id: approvalId } = await requested.json();

  const resolved = await request.post(`${API}/agent/approvals/${approvalId}/resolve`, {
    headers: authorization,
    data: { approved: true, actor: "共享复核人", note: "shared-mode human decision" },
  });
  expect(resolved.status()).toBe(200);
  expect((await resolved.json()).status).toBe("approved");

  const applied = await request.post(`${base}/change-sets/${changeSetId}/apply`, {
    headers: authorization,
    data: { session_id: sessionId, approval_id: approvalId },
  });
  expect(applied.status()).toBe(200);
  const appliedBody = await applied.json();
  expect(appliedBody.result_pins).toEqual({ [pidId]: pidRevision + 1, [cableId]: 2 });

  // token-less caller still cannot apply anything
  const anonymous = await request.post(`${base}/change-sets/${changeSetId}/apply`, {
    data: { session_id: sessionId, approval_id: approvalId },
  });
  expect(anonymous.status()).toBe(401);

  // clean our own governance rows (shared-suite FK discipline)
  const cleanup = new DatabaseSync(databasePath);
  cleanup.prepare("DELETE FROM engineering_links WHERE created_by = 'e2e'").run();
  cleanup.prepare("DELETE FROM project_documents WHERE added_by = 'e2e'").run();
  cleanup.prepare("DELETE FROM project_change_sets WHERE created_by = 'web-user'").run();
  cleanup.close();
});

// M6-2B-D3: the review queue surface sits behind the same shared-mode boundary.
// Without the token both the read routes and the decision route are 401; with it the
// (empty) queue reads fine, and a decision against a nonexistent candidate is a clean
// 404 — proving auth passed and the route exists.
test("shared deployment protects the M6 review surface with the same token boundary", async ({ request }) => {
  const created = await request.post(`${API}/documents`, {
    headers: authorization,
    data: { name: "M6 shared boundary" },
  });
  expect(created.ok()).toBeTruthy();
  const { id: documentId } = (await created.json()) as { id: string };

  const queueUrl = `${API}/documents/${documentId}/m6/candidates`;
  const anonymousQueue = await request.get(queueUrl);
  expect(anonymousQueue.status()).toBe(401);

  const anonymousDecision = await request.post(`${queueUrl}/cand_m6_nope/decisions`, {
    data: { action: "reject", reviewer_identity: "engineer.joe", reviewer_action: "拒绝" },
  });
  expect(anonymousDecision.status()).toBe(401);

  const queue = await request.get(queueUrl, { headers: authorization });
  expect(queue.status()).toBe(200);
  expect((await queue.json()).candidates).toEqual([]);

  const missing = await request.post(`${queueUrl}/cand_m6_nope/decisions`, {
    headers: authorization,
    data: { action: "reject", reviewer_identity: "engineer.joe", reviewer_action: "拒绝" },
  });
  expect(missing.status()).toBe(404);
  expect((await missing.json()).detail.code).toBe("candidate_not_found");
});
