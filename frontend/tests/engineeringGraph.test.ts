import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  engineeringObjectLabel,
  engineeringObjectSummary,
  filterEngineeringObjects,
  filterFindings,
  groupEngineeringObjects,
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
    object_id: "equipment:p-101",
    kind: "equipment",
    identity_scope: "tag",
    tag: "P-101",
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
    ...overrides,
  };
}

const graph: EngineeringGraph = {
  schema: "pid-agent.engineering-graph",
  version: 1,
  builder_version: 1,
  document_id: "doc_1",
  document_name: "Demo",
  revision: 3,
  content_hash: "abc123",
  counts: {
    equipment: 1,
    valves: 0,
    instruments: 1,
    lines: 1,
    junctions: 0,
    off_page_connectors: 1,
    annotations: 0,
    graphics: 0,
    objects: 4,
    edges: 1,
    signal_links: 0,
    errors: 1,
    warnings: 1,
    info: 0,
  },
  objects: [
    object({}),
    object({
      object_id: "instrument:pi-101",
      kind: "instrument",
      tag: "PI-101",
      label: "PI-101",
      element_ids: ["pi_1"],
      primary_element_id: "pi_1",
      symbol_key: "pressure_indicator",
      symbol_name: "压力指示仪",
      required_port_count: 1,
      connected_port_count: 0,
    }),
    object({
      object_id: "line:pl-1001",
      kind: "line",
      identity_scope: "tag",
      tag: "PL-1001",
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
      object_id: "opc:pl-2002",
      kind: "off_page_connector",
      identity_scope: "element",
      tag: "",
      label: "PL-2002",
      element_ids: ["opc_1"],
      primary_element_id: "opc_1",
      symbol_key: "off_page_connector_out",
      symbol_name: "OPC OUT（跨图出口）",
      opc_direction: "out",
      target_document_id: "doc_2",
      required_port_count: 0,
      connected_port_count: 0,
    }),
  ],
  edges: [{
    connector_id: "line_a",
    pipeline_object_id: "line:pl-1001",
    source_object_id: "equipment:p-101",
    target_object_id: "instrument:pi-101",
    source_port_id: "discharge",
    target_port_id: "process",
    medium: "water",
    medium_class: "water",
    flow_direction: "forward",
    directed: true,
  }],
  signal_links: [],
  off_page_object_ids: ["opc:pl-2002"],
  connectivity_components: [["equipment:p-101", "instrument:pi-101", "line:pl-1001"], ["opc:pl-2002"]],
  findings: [
    { severity: "error", code: "IR_DUPLICATE_IDENTITY", message: "duplicate", object_ids: ["equipment:p-101"], element_ids: ["pump_1"], details: {} },
    { severity: "warning", code: "IR_OPC_TARGET_MISSING", message: "no target", object_ids: ["opc:pl-2002"], element_ids: ["opc_1"], details: {} },
  ],
};

const project: ProjectEngineeringGraph = {
  schema: "pid-agent.project-engineering-index",
  version: 1,
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
    builder_version: 1,
    built_at: "2026-09-18T00:00:00+00:00",
    built_by: "",
    counts: {
      objects: 4,
      equipment: 1,
      valves: 0,
      instruments: 1,
      lines: 1,
      off_page_connectors: 1,
      errors: 1,
      warnings: 1,
    },
    staleness: "stale",
    stale_reasons: ["revision_changed"],
  }],
  cross_document_links: [{
    source_document_id: "doc_1",
    source_object_id: "opc:pl-2002",
    source_tag: "PL-2002",
    direction: "out",
    target_document_id: "doc_2",
    target_document_found: true,
    resolved: false,
    matching_object_ids: [],
  }],
  findings: [],
};

describe("engineering graph helpers", () => {
  it("groups objects by engineering kind in reading order", () => {
    const groups = groupEngineeringObjects(graph);
    assert.deepEqual(
      groups.map((group) => group.kind),
      ["equipment", "instrument", "line", "off_page_connector"],
    );
    assert.equal(groups.find((group) => group.kind === "line")?.objects.length, 1);
    // Empty kinds are not rendered as empty sections.
    assert.equal(groups.some((group) => group.kind === "valve"), false);
  });

  it("maps kind names onto the backend count keys", () => {
    assert.equal(kindCount(graph.counts, "instrument"), 1);
    assert.equal(kindCount(graph.counts, "off_page_connector"), 1);
    assert.equal(kindCount(graph.counts, "graphic"), 0);
  });

  it("labels and summarises objects without inventing data", () => {
    const pump = graph.objects[0];
    const line = graph.objects[2];
    const opc = graph.objects[3];
    assert.equal(engineeringObjectLabel(pump), "P-101");
    assert.match(engineeringObjectSummary(pump), /端口 1\/2/);
    assert.match(engineeringObjectSummary(line), /DN50/);
    assert.match(engineeringObjectSummary(opc), /出口/);
    assert.match(engineeringObjectSummary(opc), /doc_2/);
    // An element-scoped identity is surfaced instead of being hidden.
    assert.match(engineeringObjectSummary(opc), /无位号/);
  });

  it("filters objects by tag, medium, symbol and object id", () => {
    assert.equal(filterEngineeringObjects(graph.objects, "pl-1001").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "water").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "centrifugal").length, 1);
    assert.equal(filterEngineeringObjects(graph.objects, "  ").length, graph.objects.length);
    assert.equal(filterEngineeringObjects(graph.objects, "does-not-exist").length, 0);
  });

  it("counts and filters findings by severity", () => {
    assert.deepEqual(severityCounts(graph.findings), { error: 1, warning: 1, info: 0 });
    assert.equal(filterFindings(graph.findings, "error").length, 1);
    assert.equal(filterFindings(graph.findings, "info").length, 0);
    assert.equal(filterFindings(graph.findings, "all").length, 2);
  });

  it("describes a trace including the pipelines it traversed", () => {
    const summary = traceSummary({
      document_id: "doc_1",
      revision: 3,
      origin_object_id: "equipment:p-101",
      direction: "downstream",
      steps: [
        { depth: 0, object_id: "equipment:p-101", kind: "equipment", via_connector_id: "", direction: "origin" },
        { depth: 1, object_id: "instrument:pi-101", kind: "instrument", via_connector_id: "line_a", direction: "downstream" },
      ],
      reached_object_ids: ["equipment:p-101", "instrument:pi-101"],
      traversed_pipeline_ids: ["line:pl-1001"],
      truncated: false,
    });
    assert.match(summary, /下游/);
    assert.match(summary, /line:pl-1001/);
    assert.match(summary, /2 个对象/);
    assert.equal(traceSummary(null), "");
  });

  it("reports freshness terms and project summary honestly", () => {
    assert.equal(stalenessLabel("verified_fresh"), "已验证最新");
    assert.equal(stalenessLabel("fresh"), "revision 一致");
    assert.equal(stalenessLabel("missing_document"), "图纸已不存在");
    const summary = projectFreshnessSummary(project);
    assert.match(summary, /2 张图纸/);
    assert.match(summary, /过期 1/);
    assert.match(summary, /仅比对 revision/);
    assert.equal(projectFreshnessSummary(null), "");
  });
});
