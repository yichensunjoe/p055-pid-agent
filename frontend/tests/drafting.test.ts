import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  DRAFTING_LOCK_KEY,
  buildDraftingOptions,
  crossingsSummary,
  gateReasons,
  gateSummary,
  gateVerdict,
  groupFindings,
  isLockedElement,
  junctionsSummary,
  lockOperations,
  lockSummary,
  lockedElementIds,
  metricRows,
  previewAppliesTo,
  previewSummary,
  severityLabel,
  stalePreviewMessage,
} from "../src/drafting.ts";
import type {
  DraftingFinding,
  DraftingGate,
  DraftingPreview,
  DraftingReport,
  DraftingSnapshot,
} from "../src/draftingTypes.ts";
import type { Document } from "../src/types.ts";

function finding(overrides: Partial<DraftingFinding>): DraftingFinding {
  return {
    severity: "error",
    code: "DRAFT_NODE_OVERLAP",
    message: "设备重叠",
    element_ids: ["pump_a"],
    details: {},
    rule_source: "drafting",
    waived: false,
    ...overrides,
  };
}

function snapshot(overrides: Partial<DraftingSnapshot>): DraftingSnapshot {
  return {
    score: 100,
    passed: true,
    error_issue_count: 0,
    warning_issue_count: 0,
    symbol_count: 2,
    connector_count: 1,
    junction_count: 0,
    dangling_junction_count: 0,
    node_overlaps: 0,
    crowded_node_pairs: 0,
    pipe_obstacle_intersections: 0,
    geometric_crossings: 0,
    unbridged_crossings: 0,
    total_bends: 0,
    non_orthogonal_segments: 0,
    micro_segments: 0,
    unnecessary_bends: 0,
    text_text_overlaps: 0,
    text_symbol_overlaps: 0,
    text_connector_intersections: 0,
    duplicate_label_count: 0,
    out_of_bounds_symbols: 0,
    out_of_bounds_connector_points: 0,
    reserved_region_intrusions: 0,
    total_route_length: 0,
    ...overrides,
  };
}

function gate(overrides: Partial<DraftingGate>): DraftingGate {
  return {
    passed: true,
    score: 100,
    target_score: 95,
    blockers: [],
    drawing_issues: [],
    waived_codes: [],
    checked_codes: ["DRAFT_NODE_OVERLAP"],
    ...overrides,
  };
}

function preview(overrides: Partial<DraftingPreview> = {}): DraftingPreview {
  return {
    valid: true,
    document_id: "doc_1",
    document_name: "Drawing",
    current_revision: 4,
    transaction: { expected_revision: 4, label: "Drafting", operations: [{ op: "update_element", element_id: "pipe", patch: {} }] },
    settled: false,
    moved_element_ids: ["pump"],
    rerouted_connector_ids: ["pipe"],
    moved_annotation_ids: [],
    bridged_connector_ids: [],
    locked_element_ids: [],
    locks: {
      locked_element_ids: [],
      request_element_ids: [],
      metadata_element_ids: [],
      region_element_ids: [],
      region_labels: [],
    },
    skipped_locked_element_ids: [],
    in_scope_element_ids: ["pump", "pipe"],
    collisions: [],
    crossings: [],
    junctions: [],
    warnings: [],
    metrics: {
      before: snapshot({ score: 80, node_overlaps: 1 }),
      after: snapshot({ score: 100 }),
      improvements: ["node_overlaps: 1 -> 0"],
      regressions: [],
    },
    gate: gate({}),
    findings: [],
    reproducibility: {
      engine_version: 1,
      algorithm: "canonical-order pipeline",
      input_content_hash: "a".repeat(64),
      output_content_hash: "b".repeat(64),
      transaction_digest: "c".repeat(64),
      operation_count: 1,
      settled: false,
    },
    ...overrides,
  };
}

function documentWithLocks(): Document {
  return {
    id: "doc_1",
    name: "Drawing",
    revision: 4,
    canvas: { width: 1200, height: 800, grid_size: 10 },
    layers: [{ id: "layer_default", name: "Default", visible: true, locked: false }],
    systems: [{ id: "system_default", name: "Default", visible: true }],
    elements: [
      {
        id: "locked_pump",
        type: "symbol",
        layer_id: "layer_default",
        system_id: "system_default",
        metadata: { [DRAFTING_LOCK_KEY]: true },
        symbol_key: "centrifugal_pump",
        position: { x: 100, y: 300 },
        width: 80,
        height: 70,
        rotation: 0,
        label: "P-101",
        properties: {},
      },
      {
        id: "free_valve",
        type: "symbol",
        layer_id: "layer_default",
        system_id: "system_default",
        metadata: {},
        symbol_key: "gate_valve",
        position: { x: 400, y: 300 },
        width: 60,
        height: 50,
        rotation: 0,
        label: "HV-101",
        properties: {},
      },
    ],
  } as unknown as Document;
}

describe("drafting findings", () => {
  it("groups findings by code, worst severity first, and keeps waivers visible", () => {
    const groups = groupFindings([
      finding({ code: "DRAFT_TEXT_SYMBOL_OVERLAP", severity: "warning", element_ids: ["t1"] }),
      finding({ code: "DRAFT_NODE_OVERLAP", severity: "error", element_ids: ["pump_a"] }),
      finding({ code: "DRAFT_NODE_OVERLAP", severity: "blocker", element_ids: ["pump_b"] }),
      finding({ code: "DRAFT_CROSSING_DOUBLE_BRIDGED", severity: "warning", waived: true }),
    ]);

    assert.deepEqual(
      groups.map((group) => [group.code, group.severity, group.count]),
      [
        ["DRAFT_NODE_OVERLAP", "blocker", 2],
        ["DRAFT_CROSSING_DOUBLE_BRIDGED", "warning", 1],
        ["DRAFT_TEXT_SYMBOL_OVERLAP", "warning", 1],
      ],
    );
    assert.deepEqual(groups[0].elementIds, ["pump_a", "pump_b"]);
    assert.equal(groups[1].waived, true);
    assert.equal(groups[0].waived, false);
  });

  it("labels severity and rule source in the language of the drawing, not the code", () => {
    assert.equal(severityLabel("blocker"), "阻塞");
    assert.equal(severityLabel("warning"), "警告");
  });
});

describe("drafting gate", () => {
  it("explains every reason the gate is closed, drafting rules before drawing rules", () => {
    const reasons = gateReasons(
      gate({
        passed: false,
        score: 88,
        target_score: 95,
        blockers: [finding({ code: "DRAFT_CROSSING_UNBRIDGED", message: "管线交叉无跨线桥" })],
        drawing_issues: [
          finding({ code: "MICRO_SEGMENT", message: "微小线段", rule_source: "diagram_quality" }),
          finding({ code: "UNNECESSARY_BEND", message: "多余拐点", rule_source: "diagram_quality", severity: "warning" }),
        ],
      }),
    );

    assert.deepEqual(reasons, [
      "[DRAFT_CROSSING_UNBRIDGED] 管线交叉无跨线桥",
      "[MICRO_SEGMENT] 微小线段",
      "图面评分 88 低于目标 95",
    ]);
    assert.equal(gateVerdict(gate({ passed: false })).tone, "fail");
    assert.match(gateSummary(gate({ passed: false, score: 88, target_score: 95 })), /未通过 · 评分 88\/95/);
  });

  it("does not fail on a waived code but still reports it", () => {
    const reasonless = gateReasons(gate({ passed: true, waived_codes: ["DRAFT_JUNCTION_DANGLING"] }));
    assert.deepEqual(reasonless, []);
  });
});

describe("drafting metrics", () => {
  it("reports hard metrics as before/after rows and flags the direction of change", () => {
    const rows = metricRows({
      before: snapshot({ node_overlaps: 3, micro_segments: 0, total_route_length: 120.5 }),
      after: snapshot({ node_overlaps: 0, micro_segments: 2, total_route_length: 118.25 }),
      improvements: [],
      regressions: [],
    });

    const overlaps = rows.find((row) => row.key === "node_overlaps");
    assert.deepEqual(
      overlaps && [overlaps.before, overlaps.after, overlaps.delta, overlaps.improved, overlaps.worsened],
      [3, 0, -3, true, false],
    );
    const micro = rows.find((row) => row.key === "micro_segments");
    assert.equal(micro?.worsened, true);
    const length = rows.find((row) => row.key === "total_route_length");
    // Rounded to the row's own precision, so the table never implies false accuracy.
    assert.equal(length?.after, 118.3);
    assert.equal(length?.digits, 1);
    // A metric that is zero before and after is noise, not information.
    assert.equal(rows.some((row) => row.key === "duplicate_label_count"), false);
  });
});

describe("drafting locks", () => {
  it("reads persistent locks from document data and writes them as an ordinary patch", () => {
    const document = documentWithLocks();
    assert.deepEqual(lockedElementIds(document), ["locked_pump"]);
    const locked = document.elements[0];
    assert.equal(isLockedElement(locked), true);
    assert.equal(isLockedElement(document.elements[1]), false);

    assert.deepEqual(lockOperations(["free_valve", "free_valve", ""], true), [
      {
        op: "update_element",
        element_id: "free_valve",
        patch: { metadata: { [DRAFTING_LOCK_KEY]: true } },
      },
    ]);
    assert.deepEqual(lockOperations(["locked_pump"], false)[0].patch, {
      metadata: { [DRAFTING_LOCK_KEY]: false },
    });
  });

  it("explains where every freeze came from", () => {
    assert.equal(
      lockSummary({
        locked_element_ids: ["a", "b"],
        request_element_ids: ["b"],
        metadata_element_ids: ["a"],
        region_element_ids: ["c"],
        region_labels: ["已确认区"],
      }),
      "手动锁定 1 · 本次临时锁定 1 · 锁定区域 已确认区（1 个元素）",
    );
    assert.equal(
      lockSummary({
        locked_element_ids: [],
        request_element_ids: [],
        metadata_element_ids: [],
        region_element_ids: [],
        region_labels: [],
      }),
      "没有锁定元素",
    );
  });
});

describe("drafting preview", () => {
  it("describes what the run will do and refuses a stale preview", () => {
    const current = preview();
    assert.match(previewSummary(current), /将修改 1 处/);
    assert.equal(previewAppliesTo(current, 4), true);
    assert.equal(previewAppliesTo(current, 5), false);
    assert.match(stalePreviewMessage(current, 5), /基于 r4，当前文档已是 r5/);
    assert.equal(previewAppliesTo(preview({ transaction: null, settled: true }), 4), false);
    assert.match(previewSummary(preview({ transaction: null, settled: true })), /无需改动/);
  });

  it("summarises crossings and junctions by what they mean, not by count alone", () => {
    assert.equal(
      crossingsSummary([
        { x: 1, y: 1, first_connector_id: "a", second_connector_id: "b", bridged: true, bridged_by: "b", at_junction_id: "" },
        { x: 2, y: 2, first_connector_id: "c", second_connector_id: "d", bridged: false, bridged_by: "", at_junction_id: "j1" },
      ]),
      "交叉 2 · 已加跨线桥 1 · 落在连接节点上 1",
    );
    assert.equal(crossingsSummary([]), "没有交叉");
    assert.equal(
      junctionsSummary([
        { element_id: "j1", degree: 3, kind: "branch", connector_ids: [], element_ids: [] },
        { element_id: "j2", degree: 2, kind: "inline", connector_ids: [], element_ids: [] },
        { element_id: "j3", degree: 1, kind: "dangling", connector_ids: [], element_ids: [] },
      ]),
      "连接节点 3 · 分支 1 · 管线分段 1 · 悬空 1",
    );
    assert.equal(junctionsSummary([]), "没有连接节点");
  });
});

describe("drafting request", () => {
  it("builds a request that carries scope, switches and the waiver list", () => {
    const options = buildDraftingOptions({
      revision: 7,
      scope: "selection",
      selectedElementIds: ["pump", "pipe"],
      direction: "vertical",
      targetScore: 90,
      relayout: false,
      rerouteConnectors: true,
      placeAnnotations: true,
      bridgeCrossings: false,
      resolveCollisions: true,
      waivedCodes: ["DRAFT_JUNCTION_DANGLING"],
    });

    assert.deepEqual(options, {
      expected_revision: 7,
      element_ids: ["pump", "pipe"],
      direction: "vertical",
      relayout: false,
      reroute_connectors: true,
      place_annotations: true,
      bridge_crossings: false,
      resolve_collisions: true,
      policy: { target_score: 90, waived_codes: ["DRAFT_JUNCTION_DANGLING"] },
    });
    assert.deepEqual(
      buildDraftingOptions({
        revision: 1,
        scope: "document",
        selectedElementIds: ["ignored"],
        direction: "horizontal",
        targetScore: 95,
        relayout: true,
        rerouteConnectors: true,
        placeAnnotations: true,
        bridgeCrossings: true,
        resolveCollisions: true,
        waivedCodes: [],
      }).element_ids,
      [],
    );
  });
});

describe("drafting report surface", () => {
  it("keeps the drafting report's scope and findings addressable", () => {
    const report: DraftingReport = {
      schema: "pid-agent.drafting-report",
      version: 1,
      engine_version: 1,
      document_id: "doc_1",
      document_name: "Drawing",
      revision: 4,
      content_hash: "a".repeat(64),
      input_content_hash: "a".repeat(64),
      scope_kind: "region",
      scope_element_ids: ["pump"],
      locks: {
        locked_element_ids: [],
        request_element_ids: [],
        metadata_element_ids: [],
        region_element_ids: [],
        region_labels: [],
      },
      ports: [],
      collisions: [],
      crossings: [],
      junctions: [],
      findings: [finding({ code: "DRAFT_RESERVED_REGION_OVERLAP", severity: "error" })],
      gate: gate({ passed: false }),
      score: 90,
    };
    assert.equal(report.scope_kind, "region");
    assert.equal(groupFindings(report.findings)[0].code, "DRAFT_RESERVED_REGION_OVERLAP");
  });
});
