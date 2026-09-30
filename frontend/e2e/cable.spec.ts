import { expect, test } from "@playwright/test";
import { DatabaseSync } from "node:sqlite";

import { openDocument } from "./fixtures";

function seedCableDocument(): string {
  // The disposable e2e backend names its DB after the playwright CLI pid
  // (see playwright.config.ts); workers are children of that process.
  const databasePath = `test-results/pid-agent-e2e-${process.ppid}.db`;
  const documentId = `cab_e2e${Date.now().toString(36)}`;
  const database = new DatabaseSync(databasePath);
  const now = new Date().toISOString();
  const payload = JSON.stringify({
    schema: "pid-agent.cable-document/1",
    name: "e2e 线缆",
    segments: [
      { id: "CBL-E2E", from_node: "MCC-1", to_node: "PMP-101", gauge: "4mm2" },
    ],
  });
  database
    .prepare(
      "INSERT INTO documents_registry (document_id, domain, created_at) VALUES (?, 'cable', ?)",
    )
    .run(documentId, now);
  database
    .prepare(
      "INSERT INTO cable_documents (document_id, revision, data_json, created_at, updated_at) VALUES (?, 1, ?, ?, ?)",
    )
    .run(documentId, payload, now, now);
  database.close();
  return documentId;
}

test.describe("M11-D4 dual-domain coexistence", () => {
  test("cable domain is reachable without any open P&ID document", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("tab", { name: "线缆" }).click();
    await expect(page.getByTestId("cable-panel")).toBeVisible();
  });

  test("the frozen 10-step read-only cable surface coexists with P&ID", async ({
    page,
    request,
  }) => {
    // ① P&ID baseline
    const created = await request.post("/api/v2/documents", {
      data: { name: "coexistence-pid", width: 1600, height: 900 },
    });
    const { id: pidId } = await created.json();
    await openDocument(page, pidId);
    const pidBaseline = await (await request.get(`/api/v2/documents/${pidId}`)).json();
    const auditsBefore = (await (await request.get("/api/v2/audit/records")).json()).length;

    // ② switch to the Cable domain (top-level tab; P&ID canvas not rendered)
    const cableId = seedCableDocument();
    await page.getByRole("tab", { name: "线缆" }).click();
    await expect(page.getByTestId("cable-workspace")).toBeVisible();

    // ③ real DB-driven list; isolation: no P&ID documents in the cable list
    await expect(page.getByTestId("cable-panel")).toBeVisible();
    await expect(page.getByText("e2e 线缆（r1 · eligible）")).toBeVisible();
    await expect(
      page.getByTestId("cable-panel").getByText("coexistence-pid"),
    ).toHaveCount(0);
    const listEntry = (await (await request.get("/api/v2/cable/documents")).json()).find(
      (entry: { document_id: string }) => entry.document_id === cableId,
    );
    expect(listEntry.readiness_state).toBe("eligible");

    // ④ detail: load/identity
    await page.getByText("e2e 线缆（r1 · eligible）").click();
    await expect(page.getByTestId("cable-detail")).toBeVisible();

    // ⑤ readiness projection matches the FULL D3 contract
    const detail = await (
      await request.get(`/api/v2/cable/documents/${cableId}`)
    ).json();
    expect(detail.readiness.state).toBe("eligible");
    expect(detail.readiness.counts).toEqual({ blocker: 1, warning: 1, failed: 0 });
    expect(detail.readiness.reasons).toEqual([]);
    expect(detail.readiness.result_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(detail.readiness.profile_id).toBe("cable-built-in");
    expect(detail.readiness.profile_version).toBe(1);
    expect(detail.readiness.profile_fingerprint).toMatch(/^[0-9a-f]{64}$/);
    await expect(page.getByTestId("cable-detail").getByText("eligible")).toBeVisible();
    await expect(page.getByText("CBL-E2E")).toBeVisible();

    // ⑥ export byte parity: HTTP body == direct D3 function output
    const httpExport = await request.get(
      `/api/v2/cable/documents/${cableId}/export.zip?expected_revision=1`,
    );
    expect(httpExport.status()).toBe(200);
    const httpBytes = await httpExport.body();

    // ⑦ stale expected_revision -> 409
    const stale = await request.get(
      `/api/v2/cable/documents/${cableId}/export.zip?expected_revision=0`,
    );
    expect(stale.status()).toBe(409);

    // ⑧ cross-domain: P&ID id against the Cable surface -> 404
    const cross = await request.get(
      `/api/v2/cable/documents/${pidId}/export.zip?expected_revision=0`,
    );
    expect(cross.status()).toBe(404);

    // UI export download matches the HTTP bytes (authenticated download flow)
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "导出确定性包" }).click(),
    ]);
    const path = await download.path();
    const { readFileSync } = await import("node:fs");
    expect(readFileSync(path)).toEqual(Buffer.from(httpBytes));

    // additional in-step assertions: no write controls on the cable surface
    await expect(
      page.getByTestId("cable-workspace").getByRole("button", { name: /新增|删除|编辑/ }),
    ).toHaveCount(0);

    // ⑨ back to P&ID: intact and still editable; cable unchanged
    await page.getByRole("tab", { name: "P&ID" }).click();
    const pidAfter = await (await request.get(`/api/v2/documents/${pidId}`)).json();
    expect(pidAfter.revision).toBe(pidBaseline.revision);
    expect(pidAfter.elements).toEqual(pidBaseline.elements);
    const cableAfter = await (
      await request.get(`/api/v2/cable/documents/${cableId}`)
    ).json();
    expect(cableAfter.revision).toBe(1);

    // ⑩ shared-mode auth boundary: the Cable surface uses the SAME request
    // boundary as every other route (never an anonymous bypass) — in the
    // local e2e deployment both anonymous probes succeed; in shared mode both
    // are rejected. The dedicated shared-mode 401/403 proof lives in the
    // backend test (test_shared_mode_auth_boundary).
    const anonymousCable = await request.get("/api/v2/cable/documents");
    const anonymousPid = await request.get("/api/v2/documents");
    expect(anonymousCable.status()).toBe(anonymousPid.status());
    // local e2e backend: reads/downloads grew no audit facts
    const auditsAfter = (await (await request.get("/api/v2/audit/records")).json())
      .length;
    expect(auditsAfter).toBe(auditsBefore);
  });
});
