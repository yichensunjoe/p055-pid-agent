import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";

import { openDocument, symbol } from "./fixtures";

// M13-D5 frozen browser acceptance: the governed change-set surface end to
// end — stage (zero-write shadow preview), apply (sealed M10/D4 flow with
// exactly-one governance audit), evidence/readiness hash parity, and package
// byte parity against a fresh-process rebuild at the evidence after_pins.
// Link/membership rows are SQL-seeded fixtures; the spec cleans its own rows
// (shared-suite FK discipline, same as project.spec.ts).

const DEFAULT_PROJECT = "proj_m12default";
const AS_OF = "2026-10-06T12:00:00.000Z";

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

async function pidCurrentRevision(request, pidId: string): Promise<number> {
  const response = await request.get(`/api/v2/documents/${pidId}`);
  return (await response.json()).revision;
}

async function cableCurrentRevision(request, cableId: string): Promise<number> {
  const response = await request.get(`/api/v2/cable/documents/${cableId}`);
  return (await response.json()).revision;
}

function stagePayload(
  pidId: string,
  pidRevision: number,
  cableId: string,
  cableRevision: number,
) {
  return {
    base_member_pins: { [pidId]: pidRevision, [cableId]: cableRevision },
    evaluation_as_of: AS_OF,
    mutations: [
      {
        domain: "pid",
        document_id: pidId,
        kind: "pid_transaction",
        payload: {
          operations: [
            { op: "update_element", element_id: "PMP-E2E", patch: { label: "PMP-E2EA" } },
          ],
        },
      },
      {
        domain: "cable",
        document_id: cableId,
        kind: "cable_update",
        payload: {
          schema: "pid-agent.cable-document/1",
          name: "e2e 线缆",
          segments: [
            { id: "CBL-E2E", from_node: "MCC-1", to_node: "PMP-101", gauge: "6mm2" },
          ],
        },
      },
    ],
  };
}

test.describe("M13-D5 governed change-set surface", () => {
  test.afterEach(() => {
    const database = new DatabaseSync(databasePath());
    database.prepare("DELETE FROM engineering_links WHERE created_by = 'e2e'").run();
    database.prepare("DELETE FROM project_documents WHERE added_by = 'e2e'").run();
    // this spec is the only change-set writer in the e2e suite
    database.prepare("DELETE FROM project_change_sets WHERE created_by = 'web-user'").run();
    database.close();
  });

  test("stage zero-write preview, governed apply, evidence parity and package bytes", async ({
    page,
    request,
  }) => {
    // ① seed: pid member (with the linked equipment element) + cable member
    // + one active link pinned at current revisions
    const created = await request.post("/api/v2/documents", {
      data: { name: "change-set-e2e-pid", width: 1600, height: 900 },
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
    const cableId = `cab_cs${Date.now().toString(36)}`;
    seedCableEnvelope(cableId, 1);
    seedLink("lnk_cs_e2e", cableId, pidId, 1, pidRevision);
    const membership = new DatabaseSync(databasePath());
    membership
      .prepare(
        "INSERT INTO project_documents (project_id, document_id, added_at, added_by) VALUES (?, ?, ?, 'e2e')",
      )
      .run(DEFAULT_PROJECT, pidId, new Date().toISOString());
    membership.close();

    const payload = stagePayload(pidId, pidRevision, cableId, 1);

    // ② stage: 200 + a staged row, and ZERO engineering movement
    const staged = await request.post(
      `/api/v2/projects/${DEFAULT_PROJECT}/change-sets`,
      { data: payload },
    );
    expect(staged.status()).toBe(200);
    const stagedBody = await staged.json();
    const changeSetId = stagedBody.change_set_id as string;
    expect(stagedBody.status).toBe("staged");
    expect(stagedBody.impacted.affected_documents).toBeTruthy();
    expect(await pidCurrentRevision(request, pidId)).toBe(pidRevision);
    expect(await cableCurrentRevision(request, cableId)).toBe(1);

    // read surface: detail exposes the shadow preview, no result pins yet
    const detail = await (
      await request.get(`/api/v2/projects/${DEFAULT_PROJECT}/change-sets/${changeSetId}`)
    ).json();
    expect(detail.status).toBe("staged");
    expect(detail.preview.intent_hash).toBeTruthy();
    expect(detail.result_pins).toEqual({});

    // ③ apply: the sealed governed flow; both members advance exactly once
    const applied = await request.post(
      `/api/v2/projects/${DEFAULT_PROJECT}/change-sets/${changeSetId}/apply`,
    );
    expect(applied.status()).toBe(200);
    const appliedBody = await applied.json();
    expect(appliedBody.result_pins).toEqual({
      [pidId]: pidRevision + 1,
      [cableId]: 2,
    });
    expect(await pidCurrentRevision(request, pidId)).toBe(pidRevision + 1);
    expect(await cableCurrentRevision(request, cableId)).toBe(2);

    // ④ evidence: applied row, readiness hash parity, link re-pinned
    const after = await (
      await request.get(`/api/v2/projects/${DEFAULT_PROJECT}/change-sets/${changeSetId}`)
    ).json();
    expect(after.status).toBe("applied");
    expect(after.evidence.before_pins).toEqual({ [pidId]: pidRevision, [cableId]: 1 });
    expect(after.evidence.after_pins).toEqual(appliedBody.result_pins);
    const readiness = await (
      await request.get(
        `/api/v2/projects/${DEFAULT_PROJECT}/readiness?evaluation_as_of=${encodeURIComponent(AS_OF)}`,
      )
    ).json();
    expect(after.evidence.readiness_result_hash).toBe(readiness.result_hash);

    // ⑤ package at the evidence after_pins == fresh-process rebuild
    const httpPackage = await request.get(
      `/api/v2/projects/${DEFAULT_PROJECT}/package.zip?evaluation_as_of=${encodeURIComponent(AS_OF)}&pins=${encodeURIComponent(JSON.stringify(after.evidence.after_pins))}`,
    );
    expect(httpPackage.status()).toBe(200);
    const httpBytes = await httpPackage.body();
    const directBytes = directPackageBytes(after.evidence.after_pins);
    expect(directBytes).toEqual(Buffer.from(httpBytes));

    // ⑤-b frozen UI parity (D86-2): the ProjectPanel download at the same
    // evaluation_as_of produces byte-identical content (UI pins == the
    // post-apply current pins == evidence after_pins).
    await openDocument(page, pidId);
    await page.getByRole("tab", { name: "项目" }).click();
    await expect(page.getByTestId("project-panel")).toBeVisible();
    await page.getByTestId("project-as-of").fill(AS_OF);
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByRole("button", { name: "下载项目交付包" }).click(),
    ]);
    const { readFileSync } = await import("node:fs");
    const uiBytes = readFileSync((await download.path())!);
    expect(uiBytes).toEqual(Buffer.from(httpBytes));
    expect(uiBytes).toEqual(directBytes);

    // re-apply is refused: terminal closeout already landed
    const second = await request.post(
      `/api/v2/projects/${DEFAULT_PROJECT}/change-sets/${changeSetId}/apply`,
    );
    expect(second.status()).toBe(409);
    expect((await second.json()).detail.code).toBe("change_set_not_staged");
  });
});

function directPackageBytes(pins: Record<string, number>): Buffer {
  const python = process.env.PID_AGENT_E2E_PYTHON ?? "python";
  const script = `
import base64, json, sys
from datetime import datetime, timezone
from agentcad.cable_service import CableService
from agentcad.project_package import build_project_package, verify_project_package
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
    member_pins=json.loads(sys.argv[2]),
    evaluation_as_of=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc),
)
verify_project_package(package)
sys.stdout.buffer.write(base64.b64encode(package))
`;
  const database = databasePath();
  const out = execFileSync(python, ["-c", script, database, JSON.stringify(pins)], {
    env: { ...process.env, PYTHONPATH: path.resolve("..", "backend") },
  });
  return Buffer.from(out.toString("utf8").trim(), "base64");
}
