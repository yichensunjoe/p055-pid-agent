import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { DatabaseSync } from "node:sqlite";
import path from "node:path";

import { openDocument, resetDocuments } from "./fixtures";

// M6-2B-D3 frozen browser acceptance: the human review queue end to end — the
// seeded candidates come from the *real* D2 adapter (a python one-shot against the
// e2e database, same store code as production), and the decisions go through the
// UI. Cleanup follows the shared-suite discipline: this spec's rows are removed in
// afterEach, keyed by the fixed seed document ids.

const SOURCE_ID = "doc_m6e2e_source";
const TARGET_ID = "doc_m6e2e_target";
const API_ROOT = process.env.PID_AGENT_E2E_API_ROOT ?? "http://127.0.0.1:8000/api/v2";

function databasePath(): string {
  return path.resolve("test-results", `pid-agent-e2e-${process.ppid}.db`);
}

const SEED_SCRIPT = `
import sys
from agentcad.m6_source_adapter import M6SourceAdapter
from agentcad.models import CircleElement, Document, Layer, LineElement, Point, TextElement
from agentcad.store import SQLiteDocumentStore, StoredDocument
from agentcad.symbols import SymbolRegistry

store = SQLiteDocumentStore(sys.argv[1])

def block_lines(prefix, x, y, block):
    return [
        LineElement(id=f"{prefix}_a", start=Point(x=x, y=y), end=Point(x=x + 20, y=y), metadata={"cad_block": block, "cad_layer": "EQUIP"}),
        LineElement(id=f"{prefix}_b", start=Point(x=x + 20, y=y), end=Point(x=x + 20, y=y + 20), metadata={"cad_block": block, "cad_layer": "EQUIP"}),
        LineElement(id=f"{prefix}_c", start=Point(x=x + 20, y=y + 20), end=Point(x=x, y=y + 20), metadata={"cad_block": block, "cad_layer": "EQUIP"}),
        LineElement(id=f"{prefix}_d", start=Point(x=x, y=y + 20), end=Point(x=x, y=y), metadata={"cad_block": block, "cad_layer": "EQUIP"}),
        CircleElement(id=f"{prefix}_e", center=Point(x=x + 10, y=y + 10), radius=8, metadata={"cad_block": block, "cad_layer": "EQUIP"}),
    ]

source = Document(
    id="${SOURCE_ID}",
    name="M6 e2e 源图",
    revision=3,
    layers=[Layer(id="layer_default", name="Default"), Layer(id="layer_cad", name="EQUIP")],
    elements=[
        *block_lines("p1", 100, 100, "centrifugal_pump"),
        *block_lines("p2", 300, 100, "centrifugal_pump"),
        TextElement(id="t1", position=Point(x=110, y=130), text="P-201", font_size=12, metadata={"cad_layer": "EQUIP"}),
        TextElement(id="t2", position=Point(x=310, y=130), text="P-202", font_size=12, metadata={"cad_layer": "EQUIP"}),
    ],
)
target = Document(id="${TARGET_ID}", name="M6 e2e 目标", revision=0)
store.save(StoredDocument(document=source, undo_stack=[], redo_stack=[]))
store.save(StoredDocument(document=target, undo_stack=[], redo_stack=[]))
adapter = M6SourceAdapter(store, SymbolRegistry())
summary = adapter.ingest(adapter.pin_source(source.id), target_document_id=target.id)
print(f"seeded {len(summary.filed)} candidates")
`;

function seedQueue(): void {
  execFileSync(process.env.PID_AGENT_E2E_PYTHON ?? "python", ["-c", SEED_SCRIPT, databasePath()], {
    env: { ...process.env, PYTHONPATH: path.resolve("..", "backend") },
  });
}

function cleanQueue(): void {
  const database = new DatabaseSync(databasePath());
  try {
    database
      .prepare(
        "DELETE FROM confirmed_semantic_findings WHERE candidate_id IN (SELECT candidate_id FROM semantic_candidates WHERE source_document_id = ?)",
      )
      .run(SOURCE_ID);
    database
      .prepare(
        "DELETE FROM review_decisions WHERE candidate_id IN (SELECT candidate_id FROM semantic_candidates WHERE source_document_id = ?)",
      )
      .run(SOURCE_ID);
    database
      .prepare("DELETE FROM semantic_candidates WHERE source_document_id = ?")
      .run(SOURCE_ID);
  } finally {
    database.close();
  }
}

test.describe("M6 review queue", () => {
  test.beforeEach(async ({ request }) => {
    await resetDocuments(request);
    cleanQueue();
    seedQueue();
  });

  test.afterEach(() => {
    cleanQueue();
  });

  test("confirm and reject are recorded with the declared identity", async ({ page }) => {
    await openDocument(page, SOURCE_ID);
    await page.getByRole("tab", { name: "审查" }).click();
    const panel = page.getByTestId("m6-review-panel");
    await expect(panel).toBeVisible();
    await expect(page.getByTestId("m6-source-status")).toContainText("核验通过");

    await page.getByTestId("m6-reviewer-identity").fill("engineer.joe");

    const queue = page.getByTestId("m6-queue");
    await expect(queue.locator("tbody tr").first()).toBeVisible();
    const tagRow = queue.locator("tbody tr", { hasText: "P-201" });
    await tagRow.click();
    await expect(page.getByTestId("m6-detail")).toBeVisible();
    await page.getByTestId("m6-reviewer-action").fill("核对原图位号与泵簇");
    await expect(page.getByTestId("m6-facts")).toContainText("P-201");
    await page.getByTestId("m6-confirm").click();
    await expect(page.getByTestId("m6-detail-status")).toHaveText("已确认");
    await expect(page.getByTestId("m6-decisions")).toContainText("human_confirm");
    await expect(tagRow).toHaveAttribute("data-status", "confirmed");

    const rejectRow = queue.locator("tbody tr", { hasText: "P-202" });
    await rejectRow.click();
    // The action field clears after each recorded decision — a fresh decision states its own.
    await page.getByTestId("m6-reviewer-action").fill("位号不属于该簇");
    await page.getByTestId("m6-reject").click();
    await expect(page.getByTestId("m6-detail-status")).toHaveText("已拒绝");
    await expect(rejectRow).toHaveAttribute("data-status", "rejected");

    // The decisions are audited: one m6.review.decision per recorded decision.
    const audits = await page.request.get(`${API_ROOT}/audit/records?limit=50`);
    const records = (await audits.json()).filter(
      (record: { event_type: string }) => record.event_type === "m6.review.decision",
    );
    expect(records.length).toBe(2);
    for (const record of records) {
      expect(record.actor).toBe("engineer.joe");
      expect(record.evidence.identity_assurance).toBe("declared");
      expect(record.evidence.authentication_evidence).toBe("local-process");
    }
  });

  test("baseline recheck falls into conflict and a fresh decision resolves it", async ({
    page,
    request,
  }) => {
    await openDocument(page, SOURCE_ID);
    await page.getByRole("tab", { name: "审查" }).click();
    await page.getByTestId("m6-reviewer-identity").fill("engineer.joe");

    const queue = page.getByTestId("m6-queue");
    await queue.locator("tbody tr", { hasText: "P-201" }).click();
    await page.getByTestId("m6-reviewer-action").fill("核对原图位号与泵簇");
    await page.getByTestId("m6-confirm").click();
    await expect(page.getByTestId("m6-detail-status")).toHaveText("已确认");

    // The target moves underneath the confirmation: an object tagged P-201 appears.
    const target = await request.get(`${API_ROOT}/documents/${TARGET_ID}`);
    const targetDoc = await target.json();
    await request.post(`${API_ROOT}/documents/${TARGET_ID}/transactions`, {
      data: {
        expected_revision: targetDoc.revision,
        label: "e2e: someone creates P-201 in the target",
        operations: [
          {
            op: "add_element",
            element: {
              id: "sym_p201",
              type: "symbol",
              symbol_key: "centrifugal_pump",
              position: { x: 400, y: 400 },
              width: 60,
              height: 60,
              rotation: 0,
              label: "P-201",
              properties: {},
              layer_id: "layer_default",
              system_id: "system_default",
              style: { stroke: "#111827", fill: "none", stroke_width: 1.5, opacity: 1, dash: [] },
              name: "P-201",
              metadata: {},
            },
          },
        ],
      },
    });

    await page.getByTestId("m6-recheck").click();
    await expect(page.getByTestId("m6-detail-status")).toHaveText("基线冲突");

    await page.getByTestId("m6-resolve").click();
    await page.getByTestId("m6-resolution").fill("目标侧已存在 P-201，保留既有对象");
    await page.getByTestId("m6-resolve-submit").click();
    await expect(page.getByTestId("m6-detail-status")).toHaveText("待审阅");
    await expect(page.getByTestId("m6-decisions")).toContainText("conflict_detected");
    await expect(page.getByTestId("m6-decisions")).toContainText("human_resolves_conflict");
  });

  test("reassign files a corrected candidate and supersedes the old one", async ({ page }) => {
    await openDocument(page, SOURCE_ID);
    await page.getByRole("tab", { name: "审查" }).click();
    await page.getByTestId("m6-reviewer-identity").fill("engineer.joe");

    const queue = page.getByTestId("m6-queue");
    const row = queue.locator("tbody tr", { hasText: "P-202" });
    await row.click();
    await page.getByTestId("m6-reviewer-action").fill("位号绑错簇，改派到第一台泵");
    await page.getByTestId("m6-reassign").click();
    const form = page.getByTestId("m6-reassign-form");
    await expect(form).toBeVisible();
    await page.getByTestId("m6-reassign-refs").fill("p1_a, p1_b, p1_c, p1_d, p1_e, t2");
    await page.getByTestId("m6-reassign-submit").click();

    await expect(page.getByTestId("m6-detail-status")).toHaveText("已被取代");
    // The corrected candidate joins the queue as a fresh needs_review row.
    await expect(queue.locator("tbody tr", { hasText: "P-202" }).first()).toBeVisible();
    const rows = queue.locator("tbody tr", { hasText: "P-202" });
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(1)).toHaveAttribute("data-status", "needs_review");
  });
});
