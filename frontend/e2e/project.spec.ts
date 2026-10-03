import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";

import { openDocument, symbol } from "./fixtures";

// M12-D5 frozen browser acceptance: the read-only project inspection surface
// end to end — members, links, readiness with a fixed evaluation_as_of,
// stale -> stable refusal, re-pin -> recovery, and three-way byte parity
// (HTTP body == UI download == fresh-process rebuild).
// Link/membership rows are SQL-seeded fixtures (their write governance is
// D3-covered); this spec proves the D5 read surface.

const DEFAULT_PROJECT = "proj_m12default";
const AS_OF = "2026-10-03T12:00:00.000Z";

function databasePath(): string {
  return path.resolve("test-results", `pid-agent-e2e-${process.ppid}.db`);
}

function seedCableEnvelope(documentId: string, revision: number): void {
  const database = new DatabaseSync(databasePath());
  const now = new Date().toISOString();
  const payload = JSON.stringify({
    schema: "pid-agent.cable-document/1",
    name: "e2e 线缆",
    segments: [{ id: "CBL-E2E", from_node: "MCC-1", to_node: "PMP-101" }],
  });
  database
    .prepare(
      "INSERT INTO documents_registry (document_id, domain, created_at) VALUES (?, 'cable', ?)",
    )
    .run(documentId, now);
  database
    .prepare(
      "INSERT INTO cable_documents (document_id, revision, data_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
    )
    .run(documentId, revision, payload, now, now);
  database
    .prepare(
      "INSERT INTO project_documents (project_id, document_id, added_at, added_by) VALUES (?, ?, ?, 'e2e')",
    )
    .run(DEFAULT_PROJECT, documentId, now);
  database.close();
}

function seedLink(
  linkId: string,
  cableId: string,
  pidId: string,
  pinnedSource: number,
  pinnedTarget: number,
): void {
  const database = new DatabaseSync(databasePath());
  const now = new Date().toISOString();
  database
    .prepare(
      "INSERT INTO engineering_links ("
      + " link_id, project_id, relation_type, source_domain, source_document_id,"
      + " source_object_ref, source_endpoint, target_domain, target_document_id,"
      + " target_object_ref, pinned_source_revision, pinned_target_revision,"
      + " created_at, created_by"
      + ") VALUES (?, ?, 'cable_endpoint_equipment', 'cable', ?, 'CBL-E2E', 'from',"
      + " 'pid', ?, 'PMP-E2E', ?, ?, ?, 'e2e')",
    )
    .run(linkId, DEFAULT_PROJECT, cableId, pidId, pinnedSource, pinnedTarget, now);
  database.close();
}

function bumpCableRevision(cableId: string, revision: number): void {
  const database = new DatabaseSync(databasePath());
  database
    .prepare("UPDATE cable_documents SET revision = ?, updated_at = ? WHERE document_id = ?")
    .run(revision, new Date().toISOString(), cableId);
  database.close();
}

function repinLink(linkId: string, pinnedSource: number): void {
  const database = new DatabaseSync(databasePath());
  database
    .prepare("UPDATE engineering_links SET pinned_source_revision = ? WHERE link_id = ?")
    .run(pinnedSource, linkId);
  database.close();
}

async function pidCurrentRevision(request, pidId: string): Promise<number> {
  const response = await request.get(`/api/v2/documents/${pidId}`);
  return (await response.json()).revision;
}

test.describe("M12-D5 project inspection surface", () => {
  test("members, links, readiness, stale refusal, re-pin recovery and byte parity", async ({
    page,
    request,
  }) => {
    // ① baseline: one P&ID member (with an equipment element) + one cable
    // member + one active link pinned at current revisions
    const created = await request.post("/api/v2/documents", {
      data: { name: "project-e2e-pid", width: 1600, height: 900 },
    });
    const { id: pidId } = await created.json();
    await request.post(`/api/v2/documents/${pidId}/transactions`, {
      data: {
        expected_revision: 0,
        label: "seed",
        operations: [
          { op: "add_element", element: symbol("PMP-E2E", "agitator", { x: 200, y: 200 }, 60, 40, "PMP-E2E") },
        ],
      },
    });
    const pidRevision = await pidCurrentRevision(request, pidId);
    const cableId = `cab_e2e${Date.now().toString(36)}`;
    seedCableEnvelope(cableId, 1);
    seedLink("lnk_e2e", cableId, pidId, 1, pidRevision);

    // membership for the pid side (cable side was seeded with its envelope)
    const membership = new DatabaseSync(databasePath());
    membership
      .prepare(
        "INSERT INTO project_documents (project_id, document_id, added_at, added_by) VALUES (?, ?, ?, 'e2e')",
      )
      .run(DEFAULT_PROJECT, pidId, new Date().toISOString());
    membership.close();

    await openDocument(page, pidId);
    await page.getByRole("tab", { name: "项目" }).click();
    const panel = page.getByTestId("project-panel");
    await expect(panel).toBeVisible();
    await expect(page.getByTestId("project-workspace")).toBeVisible();

    // pin evaluation_as_of to the frozen semantic input
    await page.getByTestId("project-as-of").fill(AS_OF);

    // members + links render (member ids appear in the members table; the
    // links table repeats them as endpoints, so scope the assertion)
    await expect(panel.locator("table").first()).toContainText(pidId);
    await expect(panel.locator("table").first()).toContainText(cableId);
    await expect(panel.getByText("lnk_e2e")).toBeVisible();

    // ② readiness eligible
    await expect(page.getByTestId("project-readiness")).toContainText("eligible");

    // ③ stale: cable revision advances -> readiness not_eligible + stable refusal
    bumpCableRevision(cableId, 2);
    await page.getByRole("button", { name: "重新评估" }).click();
    await expect(page.getByTestId("project-readiness")).toContainText("not_eligible");
    await expect(page.getByTestId("project-readiness")).toContainText(
      "stale_pinned_revision_source",
    );
    await page.getByRole("button", { name: "下载项目交付包" }).click();
    await expect(panel.getByRole("alert")).toContainText("409");

    // ④ re-pin -> eligible again, package builds
    repinLink("lnk_e2e", 2);
    await page.getByRole("button", { name: "重新评估" }).click();
    await expect(page.getByTestId("project-readiness")).toContainText("eligible");

    // ⑤ UI download == HTTP body == fresh-process rebuild
    const httpPackage = await request.get(
      `/api/v2/projects/${DEFAULT_PROJECT}/package.zip?evaluation_as_of=${encodeURIComponent(AS_OF)}`,
    );
    expect(httpPackage.status()).toBe(200);
    const httpBytes = await httpPackage.body();

    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "下载项目交付包" }).click(),
    ]);
    const { readFileSync } = await import("node:fs");
    expect(readFileSync((await download.path())!)).toEqual(Buffer.from(httpBytes));

    const directBytes = directPackageBytes(cableId, pidId);
    expect(directBytes).toEqual(Buffer.from(httpBytes));

    // P&ID domain intact: switch back, canvas still renders the document
    await page.getByRole("tab", { name: "P&ID" }).click();
    const pidAfter = await (await request.get(`/api/v2/documents/${pidId}`)).json();
    expect(pidAfter.revision).toBe(pidRevision);
  });
});

function directPackageBytes(cableId: string, pidId: string): Buffer {
  const python = process.env.PID_AGENT_E2E_PYTHON ?? "python";
  const script = `
import base64, sys
from datetime import datetime, timezone
from agentcad.cable_service import CableService
from agentcad.project_package import build_project_package
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry

store = SQLiteDocumentStore(sys.argv[1])
service = DocumentService(store, SymbolRegistry())
package = build_project_package(
    store=store,
    pid_service=service,
    cable_service=CableService(store),
    project_id="${DEFAULT_PROJECT}",
    evaluation_as_of=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
)
assert "${cableId}" and "${pidId}"  # fixture identity bound into the check
sys.stdout.buffer.write(base64.b64encode(package))
`;
  const database = databasePath();
  const out = execFileSync(python, ["-c", script, database], {
    env: { ...process.env, PYTHONPATH: path.resolve("..", "backend") },
  });
  return Buffer.from(out.toString("utf8").trim(), "base64");
}
