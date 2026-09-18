import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  connectionMatchLabel,
  connectionSummary,
  engineeringObjectLabel,
  engineeringObjectSummary,
  filterEngineeringObjects,
  filterFindings,
  groupEngineeringObjects,
  identityBasisLabel,
  kindCount,
  projectFreshnessSummary,
  severityCounts,
  stalenessLabel,
  traceSummary,
} from "../src/engineeringGraph.ts";
import type {
  EngineeringGraph,
  EngineeringObject,
  ProjectEngineeringGraph,
} from "../src/types.ts";

function object(overrides: Partial<EngineeringObject>): EngineeringObject {
  return {
    engineering_id: "eq_9f3a2b1c4d5e",
    kind: "equipment",
    identity_basis: "element",
    declared_id: "",
    tag: "P-101",
    tag_key: "equipment:p-101",
    name: "",
    label: "P-101",
    symbol_key: "centrifugal_pump",
    symbol_name: "离心泵",
    category: "泵",
    capability: "",
    element_ids: ["pump_1"],
    primary_element_id: "pump_1",
    layer_id: "layer_default",
    layer_name: "Process",
    system_id: "system_default",
    system_name: "Default",
    media: "",
    medium_class: "",
    nominal_diameter: "",
    flow_direction: "none",
    required_port_count: 2,
    connected_port_count: 1,
    connection_count: 1,
    length: 0,
    opc_direction: "",
    target_document_id: "",
    off_page_connection_id: "",
    signal: null,
    ...overrides,
  };
}

const graph: EngineeringGraph = {
  schema: "pid-agent.engineering-graph",
  version: 2,
  builder_version: 2,
  document_id: "doc_1",
  document_name: "Demo",
  revision: 3,
  content_hash: "abc123",
  counts: {
    equipment: 1,
    valves: 0,
    instruments: 1,
    signals: 1,
    lines: 1,
    junctions: 0,
    off_page_connectors: 1,
    annotations: 0,
    graphics: 0,
    objects: 5,
    edges: 2,
    process_edges: 1,
    signal_edges: 1,
    errors: 1,
    warnings: 1,
    info: 0,
  },
  objects: [
    object({}),
    object({
      engineering_id: "inst_0431929b5d16",
      kind: "instrument",
      tag: "PI-101",
      tag_key: "instrument:pi-101",
      label: "PI-101",
      element_ids: ["pi_1"],
      primary_element_id: "pi_1",
      symbol_key: "pressure_indicator",
      symbol_name: "压力指示仪",
      required_port_count: 1,
      connected_port_count: 0,
    }),
    object({
      engineering_id: "ln_7841e5ecff50",
      kind: "line",
      tag: "PL-1001",
      tag_key: "line:pl-1001",
      label: "PL-1001",
      element_ids: ["line_a", "line_b"],
      primary_element_id: "line_a",
      symbol_key: "",
      symbol_name: "",
      media: "water",
      medium_class: "water",
      nominal_diameter: "DN50",
      length: 240.4,
      required_port_count: 0,
      connected_port_count: 0,
      connection_count: 2,
    }),
    object({
      engineering_id: "sg_37ee17f10597",
      kind: "signal",
      tag: "SI-1001",
      tag_key: "signal:si-1001",
      label: "SI-1001",
      element_ids: ["sig_1"],
      primary_element_id: "sig_1",
      symbol_key: "",
      symbol_name: "",
      required_port_count: 0,
      connected_port_count: 0,
      signal: {
        signal_id: "sg_37ee17f10597",
        connector_id: "sig_1",
        signal_type: "electrical",
        medium: "electric signal",
        medium_class: "other",
        source_engineering_id: "inst_0431929b5d16",
        target_engineering_id: "eq_9f3a2b1c4d5e",
        instrument_engineering_ids: ["inst_0431929b5d16"],
        equipment_engineering_ids: ["eq_9f3a2b1c4d5e"],
        classification: "declared_medium",
        provenance: { basis: "medium" },
      },
    }),
    object({
      engineering_id: "opc_5a1b2c3d4e5f",
      kind: "off_page_connector",
      tag: "",
      tag_key: "",
      label: "PL-2002",
      element_ids: ["opc_1"],
      primary_element_id: "opc_1",
      symbol_key: "off_page_connector_out",
      symbol_name: "OPC OUT（跨图出口）",
      opc_direction: "out",
      target_document_id: "doc_2",
      off_page_connection_id: "opc_conn_11aa22bb33cc",
      required_port_count: 0,
      connected_port_count: 0,
    }),
  ],
  edges: [{
    connector_id: "line_a",
    edge_class: "process",
    pipeline_engineering_id: "ln_7841e5ecff50",
    signal_engineering_id: "",
    source_engineering_id: "eq_9f3a2b1c4d5e",
    target_engineering_id: "inst_0431929b5d16",
    source_port_id: "discharge",
    target_port_id: "process",
    medium: "water",
    medium_class: "water",
    flow_direction: "forward",
    directed: true,
  }],
  signals: ["sg_37ee17f10597"],
  off_page_object_ids: ["opc_5a1b2c3d4e5f"],
  connectivity_components: [
    ["eq_9f3a2b1c4d5e", "inst_0431929b5d16", "ln_7841e5ecff50"],
    ["opc_5a1b2c3d4e5f"],
  ],
  findings: [
    { severity: "error", code: "IR_DUPLICATE_IDENTITY", message: "duplicate", object_ids: ["eq_9f3a2b1c4d5e"], element_ids: ["pump_1"], details: {} },
    { severity: "warning", code: "IR_OPC_TARGET_MISSING", message: "no target", object_ids: ["opc_5a1b2c3d4e5f"], element_ids: ["opc_1"], details: {} },
  ],
};

const project: ProjectEngineeringGraph = {
  schema: "pid-agent.project-engineering-index",
  version: 2,
  generated_at: "2026-09-18T00:00:00+00:00",
  freshness: "cheap",
  document_count: 2,
  indexed_document_count: 1,
  stale_document_ids: ["doc_2"],
  unindexed_document_ids: ["doc_2"],
  totals: graph.counts,
  documents: [{
    document_id: "doc_1",
    document_name: "Demo",
    revision: 3,
    content_hash: "abc123",
    graph_hash: "def456",
    builder_version: 2,
    built_at: "2026-09-18T00:00:00+00:00",
    built_by: "",
    counts: {
      objects: 5,
      equipment: 1,
      valves: 0,
      instruments: 1,
      signals: 1,
      lines: 1,
      off_page_connectors: 1,
      errors: 1,
      warnings: 1,
    },
    staleness: "stale",
    stale_reasons: ["revision_changed"],
  }],
  off_page_connections: [{
    connection_id: "opc_conn_11aa22bb33cc",
    source_document_id: "doc_1",
    source_engineering_id: "opc_5a1b2c3d4e5f",
    source_tag: "PL-2002",
    source_connection_endpoint_id: "opc_conn_end_a",
    target_document_id: "doc_2",
    target_engineering_id: "",
    target_tag: "",
    target_connection_endpoint_id: "",
    direction: "out",
    service: "PL-2002",
    line_engineering_ids: [],
    resolved: false,
    matched_by: "unresolved",
    tag_agrees: false,
    target_document_found: true,
    declared_by_document_ids: ["doc_1"],
  }],
  findings: [],
};

describe("engineering graph helpers", () => {
  it("groups objects by engineering kind in reading order", () => {
    const groups = groupEngineeringObjects(graph);
    assert.deepEqual(
      groups.map((group) => group.kind),
      ["equipment", "instrument", "signal", "line", "off_page_connector"],
    );
    assert.equal(groups.find((group) => group.kind === "line")?.objects.length, 1);
    assert.equal(groups.find((group) => group.kind === "signal")?.objects.length, 1);
    // Empty kinds are not rendered as empty sections.
    assert.equal(groups.some((group) => group.kind === "valve"), false);
  });

  it("maps kind names onto the backend count keys", () => {
    assert.equal(kindCount(graph.counts, "instrument"), 1);
    assert.equal(kindCount(graph.counts, "signal"), 1);
    assert.equal(kindCount(graph.counts, "off_page_connector"), 1);
    assert.equal(kindCount(graph.counts, "graphic"), 0);
  });

  it("labels and summarises objects without inventing data", () => {
    const pump = graph.objects[0];
    const line = graph.objects[2];
    const signal = graph.objects[3];
    const opc = graph.objects[4];
    assert.equal(engineeringObjectLabel(pump), "P-101");
    assert.equal(identityBasisLabel(pump), "图元句柄标识");
    assert.equal(identityBasisLabel(object({ identity_basis: "declared", declared_id: "EQ-1" })), "图纸声明标识");
    assert.match(engineeringObjectSummary(pump), /端口 1\/2/);
    assert.match(engineeringObjectSummary(line), /DN50/);
    assert.match(engineeringObjectSummary(opc), /出口/);
    assert.match(engineeringObjectSummary(opc), /doc_2/);
    assert.match(engineeringObjectSummary(opc), /opc_conn_11aa22bb33cc/);
    // A signal shows its type, how it was classified and what it wires together.
    assert.match(engineeringObjectSummary(signal), /电气/);
    assert.match(engineeringObjectSummary(signal), /按介质声明/);
    assert.match(engineeringObjectSummary(signal), /inst_0431929b5d16 → eq_9f3a2b1c4d5e/);
    // An untagged object is surfaced instead of being hidden.
    assert.match(engineeringObjectSummary(opc), /无位号/);
  });

  it("filters objects by identity, tag key, medium and element id", () => {
    assert.equal(filterEngineeringObjects(graph.objects, "ln_7841e5ecff50").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "line:pl-1001").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "pl-1001").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "water").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "centrifugal").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "pump_1").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "  ").length, graph.objects.length);
    assert.equal(filterEngineeringObjects(graph.objects, "does-not-exist").length, 0);
  });

  it("counts and filters findings by severity", () => {
    assert.deepEqual(severityCounts(graph.findings), { error: 1, warning: 1, info: 0 });
    assert.equal(filterFindings(graph.findings, "error").length, 1);
    assert.equal(filterFindings(graph.findings, "info").length, 0);
    assert.equal(filterFindings(graph.findings, "all").length, 2);
  });

  it("describes a trace including identity resolution and the network it walked", () => {
    const summary = traceSummary({
      document_id: "doc_1",
      revision: 3,
      origin_engineering_id: "eq_9f3a2b1c4d5e",
      resolved_from: "equipment:p-101",
      direction: "downstream",
      edge_class: "process",
      steps: [
        { depth: 0, engineering_id: "eq_9f3a2b1c4d5e", kind: "equipment", via_connector_id: "", direction: "origin" },
        { depth: 1, engineering_id: "inst_0431929b5d16", kind: "instrument", via_connector_id: "line_a", direction: "downstream" },
      ],
      reached_engineering_ids: ["eq_9f3a2b1c4d5e", "inst_0431929b5d16"],
      traversed_pipeline_ids: ["ln_7841e5ecff50"],
      truncated: false,
    });
    assert.match(summary, /下游/);
    assert.match(summary, /由 equipment:p-101 解析/);
    assert.match(summary, /工艺流/);
    assert.match(summary, /ln_7841e5ecff50/);
    assert.match(summary, /2 个对象/);

    const signalSummary = traceSummary({
      document_id: "doc_1",
      revision: 3,
      origin_engineering_id: "sg_37ee17f10597",
      resolved_from: "",
      direction: "both",
      edge_class: "signal",
      steps: [],
      reached_engineering_ids: [],
      traversed_pipeline_ids: [],
      truncated: false,
    });
    assert.match(signalSummary, /信号网络/);
    assert.equal(traceSummary(null), "");
  });

  it("describes cross-drawing connections without over-claiming", () => {
    const unresolved = project.off_page_connections[0];
    assert.equal(connectionMatchLabel("reciprocal_declaration"), "双方互相声明");
    assert.equal(connectionMatchLabel("service_convention"), "同标识约定匹配");
    assert.match(connectionSummary(unresolved), /目标图内无可对应连接/);
    assert.match(
      connectionSummary({
        ...unresolved,
        resolved: true,
        matched_by: "reciprocal_declaration+service",
        tag_agrees: true,
      }),
      /已解析 · 互相声明 \+ 标识校核 · 标识一致/,
    );
    assert.match(
      connectionSummary({ ...unresolved, resolved: true, tag_agrees: false }),
      /标识不一致/,
    );
  });

  it("reports freshness terms and project summary honestly", () => {
    assert.equal(stalenessLabel("verified_fresh"), "已验证最新");
    assert.equal(stalenessLabel("fresh"), "revision 一致");
    assert.equal(stalenessLabel("builder_outdated"), "构建器版本变化");
    assert.equal(stalenessLabel("missing_document"), "图纸已不存在");
    const summary = projectFreshnessSummary(project);
    assert.match(summary, /2 张图纸/);
    assert.match(summary, /过期 1/);
    assert.match(summary, /跨图连接 1（待解析 1）/);
    assert.match(summary, /仅比对 revision/);
    assert.equal(projectFreshnessSummary(null), "");
  });
});
