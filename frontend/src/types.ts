export type Point = { x: number; y: number };

export type Style = {
  stroke: string;
  fill: string;
  stroke_width: number;
  opacity: number;
  dash: number[];
};

export type BaseElement = {
  id: string;
  layer_id: string;
  system_id: string;
  style: Style;
  name: string;
  metadata: Record<string, unknown>;
};

export type LineElement = BaseElement & { type: "line"; start: Point; end: Point };
export type PolylineElement = BaseElement & { type: "polyline"; points: Point[]; closed: boolean };
export type RectangleElement = BaseElement & { type: "rectangle"; x: number; y: number; width: number; height: number; corner_radius: number };
export type CircleElement = BaseElement & { type: "circle"; center: Point; radius: number };
export type TextElement = BaseElement & { type: "text"; position: Point; text: string; font_size: number; anchor: "start" | "middle" | "end" };
export type SymbolElement = BaseElement & { type: "symbol"; symbol_key: string; position: Point; width: number; height: number; rotation: number; label: string; properties: Record<string, unknown> };
export type JunctionElement = BaseElement & { type: "junction"; position: Point; radius: number; label: string };

export type ConnectorEndpoint = {
  element_id?: string | null;
  port_id?: string | null;
  point: Point;
};

export type ConnectorElement = BaseElement & {
  type: "connector";
  points: Point[];
  source?: ConnectorEndpoint | null;
  target?: ConnectorEndpoint | null;
  routing: "orthogonal" | "direct" | "manual";
  process_tag: string;
  medium: string;
  nominal_diameter: string;
  flow_direction: "forward" | "reverse" | "none";
  arrow_position: "start" | "middle" | "end";
  crossing_style: "none" | "jump";
  jump_radius: number;
};

export type Element =
  | LineElement
  | PolylineElement
  | RectangleElement
  | CircleElement
  | TextElement
  | SymbolElement
  | JunctionElement
  | ConnectorElement;

export type Layer = { id: string; name: string; visible: boolean; locked: boolean };
export type SystemGroup = { id: string; name: string; visible: boolean };

export type Document = {
  id: string;
  name: string;
  revision: number;
  canvas: { width: number; height: number; grid_size: number; background: string };
  layers: Layer[];
  systems: SystemGroup[];
  elements: Element[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ProjectFolder = {
  id: string;
  name: string;
  color?: string;
  created_at?: string;
};

export type DocumentSummary = {
  id: string;
  name: string;
  revision: number;
  element_count: number;
  updated_at: string;
  metadata?: Record<string, unknown>;
};

export type HistoryChange = {
  entity_kind: "element" | "layer" | "system";
  entity_id: string;
  change: "added" | "updated" | "deleted";
  entity_type?: string | null;
  changed_fields: string[];
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
};

export type HistoryOperationSummary = {
  op: Operation["op"];
  element_id?: string;
  element_type?: string;
  entity_id?: string;
  name?: string;
  patch_fields?: string[];
  move_elements_to?: string;
};

export type HistoryDetails = {
  schema_version?: number;
  action?: string;
  base_revision?: number;
  result_revision?: number;
  element_count_before?: number;
  element_count_after?: number;
  affected_element_ids?: string[];
  added_element_ids?: string[];
  updated_element_ids?: string[];
  deleted_element_ids?: string[];
  change_count?: number;
  changes?: HistoryChange[];
  diff_truncated?: boolean;
  operation_summaries?: HistoryOperationSummary[];
  decode_error?: boolean;
};

export type HistoryEntry = {
  id: number | null;
  document_id: string;
  revision: number;
  timestamp: string;
  source: "web" | "llm" | "mcp" | "system";
  action: "create" | "transaction" | "undo" | "redo";
  label: string;
  operation_count: number;
  details?: HistoryDetails;
};

export type SymbolShape =
  | { type: "line"; x1: number; y1: number; x2: number; y2: number; dash?: number[] }
  | { type: "polyline"; points: [number, number][]; closed?: boolean; fill?: string; dash?: number[] }
  | { type: "rect"; x: number; y: number; width: number; height: number; rx?: number; dash?: number[] }
  | { type: "circle"; cx: number; cy: number; r: number; dash?: number[] }
  | { type: "path"; d: string; dash?: number[] }
  | { type: "text"; x: number; y: number; text: string; font_size?: number; anchor?: string };

export type SymbolPort = {
  id: string;
  name: string;
  x: number;
  y: number;
  direction: "in" | "out" | "bidirectional" | "none";
  medium: string;
};

export type SymbolDefinition = {
  key: string;
  name: string;
  category: string;
  description: string;
  width: number;
  height: number;
  ports: SymbolPort[];
  shapes: SymbolShape[];
  metadata?: Record<string, unknown>;
};

export type Operation =
  | { op: "add_element"; element: Omit<Element, keyof BaseElement> & Partial<BaseElement> }
  | { op: "update_element"; element_id: string; patch: Record<string, unknown> }
  | { op: "delete_element"; element_id: string }
  | { op: "add_layer"; layer: Layer }
  | { op: "update_layer"; layer_id: string; patch: Partial<Omit<Layer, "id">> }
  | { op: "delete_layer"; layer_id: string; move_elements_to?: string }
  | { op: "add_system"; system: SystemGroup }
  | { op: "update_system"; system_id: string; patch: Partial<Omit<SystemGroup, "id">> }
  | { op: "delete_system"; system_id: string; move_elements_to?: string }
  | { op: "clear_document" };

export type AgentTransaction = {
  operations: Operation[];
  expected_revision?: number | null;
  label: string;
  source?: "web" | "llm" | "mcp" | "system" | null;
};

export type AgentPlan = {
  explanation: string;
  transaction: AgentTransaction;
};

export type SemanticOperation =
  | Exclude<Operation, { op: "delete_element" }>
  | { op: "delete_element"; element_id: string; connection_policy?: "reject_if_connected" | "detach" | "delete_connectors" }
  | { op: "replace_symbol"; element_id: string; symbol_key: string; port_mapping?: Record<string, string>; preserve_size?: boolean; label?: string | null; properties_patch?: Record<string, unknown> }
  | { op: "reconnect_connector"; connector_id: string; endpoint: "source" | "target"; element_id?: string | null; port_id?: string | null; point?: Point | null; routing?: "orthogonal" | "direct" | "manual" | null }
  | { op: "connect_ports"; connector_id: string; source_element_id: string; source_port_id: string; target_element_id: string; target_port_id: string; routing?: "orthogonal" | "direct"; waypoints?: Point[]; process_tag?: string; medium?: string; nominal_diameter?: string; flow_direction?: "forward" | "reverse" | "none"; arrow_position?: "start" | "middle" | "end"; crossing_style?: "none" | "jump"; jump_radius?: number; layer_id?: string | null; system_id?: string | null; style?: Partial<Style> | null; name?: string; metadata?: Record<string, unknown> }
  | { op: "instrument_tap"; main_connector_id: string; junction_point: Point; direction?: "up" | "down"; branch_length?: number; measurement: "pressure" | "temperature" | "flow"; instrument_label: string; instrument_symbol_key?: string | null; instrument_port_id?: string; root_valve_symbol_key?: string; root_valve_in_port_id?: string; root_valve_out_port_id?: string; root_valve_label?: string; junction_id: string; downstream_connector_id: string; root_valve_id: string; instrument_id: string; junction_to_valve_connector_id: string; valve_to_instrument_connector_id: string; layer_id?: string | null; system_id?: string | null; style?: Partial<Style> | null; metadata?: Record<string, unknown> };

export type SemanticTransaction = {
  operations: SemanticOperation[];
  expected_revision?: number | null;
  label: string;
};

export type SemanticAgentPlan = {
  plan_id: string;
  explanation: string;
  transaction: SemanticTransaction;
};

export type AgentOperationIssue = {
  operation_index: number | null;
  operation: string;
  code: string;
  message: string;
  field_path: string;
  element_id?: string | null;
  connector_id?: string | null;
  available_values: Record<string, string[]>;
  suggestions: string[];
};

/** Why one submitted operation did not make it into the drawing. */
export type AgentRejectedOperationReceipt = {
  original_index: number;
  operation_id: string;
  operation_kind: string;
  reason_code: string;
  message: string;
  field_path: string;
  available_values: Record<string, string[]>;
  suggestions: string[];
};

/**
 * How a compiled plan is judged, on two axes rather than one.
 *
 * `valid` answers "is each retained operation legal"; `completeness` answers "is the submitted
 * plan whole". They used to be one field, which is why a plan that lost 39% of its own
 * operations was reported as passing validation and recorded as a completed session.
 *
 * `operation_accounting` says whether the compiler examined the plan operation by operation. It
 * is the field a consumer must read first: when it is `not_evaluated` the counts and both
 * verdicts are `null`, because the truth is that no claim was made. Zero is a claim ("nothing
 * survived") and is never used to mean "unknown".
 */
export type AgentTransactionAssessment = {
  valid: boolean;
  stage: "compile" | "validate";
  document_id: string;
  current_revision: number;
  next_revision: number;
  semantic_operation_count: number;
  compiled_operation_count: number;
  resulting_element_count?: number | null;
  affected_element_ids: string[];
  added_element_ids: string[];
  updated_element_ids: string[];
  deleted_element_ids: string[];
  issues: AgentOperationIssue[];
  operation_accounting?: "evaluated" | "not_evaluated";
  accepted_operation_count?: number | null;
  rejected_operation_count?: number | null;
  completeness?: "complete" | "partial" | "empty" | null;
  rejected_operations?: AgentRejectedOperationReceipt[] | null;
};

export type AnnotationQuality = {
  duplicate_label_count: number;
  text_text_overlaps: number;
  text_symbol_overlaps: number;
  text_connector_intersections: number;
};

export type AnnotationLayoutMetrics = {
  before: AnnotationQuality;
  after: AnnotationQuality;
  generated_text_ids: string[];
  moved_text_ids: string[];
  deleted_text_ids: string[];
  leader_line_ids: string[];
};

export type DiagramQualityIssue = {
  severity: "warning" | "error";
  code: string;
  message: string;
  element_ids: string[];
  details: Record<string, unknown>;
};

export type DiagramQualityMetrics = {
  symbol_count: number;
  connector_count: number;
  total_bends: number;
  non_orthogonal_segments: number;
  micro_segments: number;
  unnecessary_bends: number;
  excessive_bend_connectors: number;
  node_overlaps: number;
  crowded_node_pairs: number;
  pipe_obstacle_intersections: number;
  geometric_crossings: number;
  unbridged_crossings: number;
  port_direction_mismatches: number;
  port_exit_mismatches: number;
  port_facing_mismatches: number;
  backward_flow_connectors: number;
  out_of_bounds_symbols: number;
  out_of_bounds_connector_points: number;
  duplicate_label_count: number;
  text_text_overlaps: number;
  text_symbol_overlaps: number;
  text_connector_intersections: number;
};

export type DiagramQualityReport = {
  schema: "pid-agent.diagram-quality";
  version: 2;
  passed: boolean;
  score: number;
  metrics: DiagramQualityMetrics;
  issues: DiagramQualityIssue[];
};

export type SemanticAgentPlanResult = {
  session_id?: string;
  plan: SemanticAgentPlan;
  compiled_plan?: AgentPlan | null;
  assessment: AgentTransactionAssessment;
  attempt: number;
  parent_plan_id?: string | null;
  annotation_metrics?: AnnotationLayoutMetrics | null;
  diagram_quality?: DiagramQualityReport | null;
};

export type TransactionValidation = {
  valid: boolean;
  document_id: string;
  current_revision: number;
  next_revision: number;
  operation_count: number;
  resulting_element_count: number;
  affected_element_ids: string[];
  added_element_ids?: string[];
  updated_element_ids?: string[];
  deleted_element_ids?: string[];
  change_count?: number;
};

export type Tool = "select" | "line" | "rectangle" | "circle" | "connector" | "junction" | "text" | "symbol";

export type LineVariety = "solid" | "dashed";
export type RectangleVariety = "solid" | "rounded" | "dashed";
export type CircleVariety = "solid" | "dashed" | "filled";

export type ProjectSettings = {
  name: string;
  metadata: Record<string, unknown>;
};

export type DocumentEnvelope = {
  format: "pid-agent.document";
  version: 1;
  document: Document;
};

export type ProjectPackageEnvelope = {
  format: "pid-agent.project-package";
  version: 1;
  project: ProjectSettings;
  documents: Document[];
};

export type ImportResult = {
  documents: Document[];
  document_id_map: Record<string, string>;
  project?: ProjectSettings | null;
};

export type ReportScope = "visible" | "all";
export type RuleSeverity = "info" | "warning" | "error";

export type EquipmentScheduleRow = {
  element_id: string;
  tag: string;
  name: string;
  symbol_key: string;
  symbol_name: string;
  category: string;
  layer_id: string;
  layer_name: string;
  system_id: string;
  system_name: string;
  required_port_count: number;
  connected_port_count: number;
  properties: Record<string, unknown>;
};

export type LineScheduleRow = {
  element_id: string;
  line_tag: string;
  name: string;
  medium: string;
  nominal_diameter: string;
  routing: string;
  flow_direction: string;
  layer_id: string;
  layer_name: string;
  system_id: string;
  system_name: string;
  source: string;
  target: string;
  metadata: Record<string, unknown>;
};

export type InstrumentScheduleRow = EquipmentScheduleRow;

export type RuleFinding = {
  severity: RuleSeverity;
  code: string;
  message: string;
  element_ids: string[];
  details: Record<string, unknown>;
};

export type EngineeringReportCounts = {
  equipment: number;
  lines: number;
  instruments: number;
  errors: number;
  warnings: number;
  info: number;
};

export type EngineeringReport = {
  schema: "pid-agent.engineering-report";
  version: 1;
  document_id: string;
  document_name: string;
  revision: number;
  scope: ReportScope;
  counts: EngineeringReportCounts;
  equipment: EquipmentScheduleRow[];
  lines: LineScheduleRow[];
  instruments: InstrumentScheduleRow[];
  findings: RuleFinding[];
};

// --- M2 engineering semantic graph -----------------------------------------------

export type EngineeringObjectKind =
  | "equipment"
  | "valve"
  | "instrument"
  | "signal"
  | "line"
  | "junction"
  | "off_page_connector"
  | "annotation"
  | "graphic";

/** Where the immutable engineering identity came from. */
export type EngineeringIdentityBasis = "declared" | "element";

export type EngineeringSignalDetail = {
  signal_id: string;
  connector_id: string;
  signal_type: "analog" | "digital" | "electrical" | "pneumatic" | "impulse" | "unknown";
  medium: string;
  medium_class: string;
  source_engineering_id: string;
  target_engineering_id: string;
  instrument_engineering_ids: string[];
  equipment_engineering_ids: string[];
  classification: "declared_medium" | "instrument_to_instrument";
  provenance: Record<string, unknown>;
};

export type EngineeringObject = {
  /** Immutable identity. A tag rename never changes it. */
  engineering_id: string;
  kind: EngineeringObjectKind;
  identity_basis: EngineeringIdentityBasis;
  declared_id: string;
  /** Mutable engineering attribute, and its derived human-readable alias key. */
  tag: string;
  tag_key: string;
  name: string;
  label: string;
  symbol_key: string;
  symbol_name: string;
  category: string;
  capability: string;
  element_ids: string[];
  primary_element_id: string;
  layer_id: string;
  layer_name: string;
  system_id: string;
  system_name: string;
  media: string;
  medium_class: string;
  nominal_diameter: string;
  flow_direction: string;
  required_port_count: number;
  connected_port_count: number;
  connection_count: number;
  length: number;
  opc_direction: "in" | "out" | "";
  target_document_id: string;
  /** Stable identity of the off-page connection this connector carries. */
  off_page_connection_id: string;
  /** Present on first-class signal objects only. */
  signal: EngineeringSignalDetail | null;
};

export type EngineeringGraphFinding = {
  severity: RuleSeverity;
  code: string;
  message: string;
  object_ids: string[];
  element_ids: string[];
  details: Record<string, unknown>;
};

export type EngineeringGraphCounts = {
  equipment: number;
  valves: number;
  instruments: number;
  signals: number;
  lines: number;
  junctions: number;
  off_page_connectors: number;
  annotations: number;
  graphics: number;
  objects: number;
  edges: number;
  process_edges: number;
  signal_edges: number;
  errors: number;
  warnings: number;
  info: number;
};

/** Process flow and instrument wiring are separate edge classes. */
export type EngineeringEdgeClass = "process" | "signal";

export type TopologyEdge = {
  connector_id: string;
  edge_class: EngineeringEdgeClass;
  pipeline_engineering_id: string;
  signal_engineering_id: string;
  source_engineering_id: string;
  target_engineering_id: string;
  source_port_id: string;
  target_port_id: string;
  medium: string;
  medium_class: string;
  flow_direction: "forward" | "reverse" | "none";
  directed: boolean;
};

export type EngineeringGraph = {
  schema: "pid-agent.engineering-graph";
  version: 2;
  builder_version: number;
  document_id: string;
  document_name: string;
  revision: number;
  content_hash: string;
  counts: EngineeringGraphCounts;
  objects: EngineeringObject[];
  edges: TopologyEdge[];
  signals: string[];
  off_page_object_ids: string[];
  connectivity_components: string[][];
  findings: EngineeringGraphFinding[];
};

export type EngineeringTraceStep = {
  depth: number;
  engineering_id: string;
  kind: EngineeringObjectKind;
  via_connector_id: string;
  direction: "upstream" | "downstream" | "undirected" | "origin";
};

export type EngineeringTraceResult = {
  document_id: string;
  revision: number;
  origin_engineering_id: string;
  /** The reference the caller used, when it was not already the identity. */
  resolved_from: string;
  direction: "upstream" | "downstream" | "both";
  edge_class: EngineeringEdgeClass;
  steps: EngineeringTraceStep[];
  reached_engineering_ids: string[];
  traversed_pipeline_ids: string[];
  truncated: boolean;
};

export type ProjectIndexStaleness =
  | "verified_fresh"
  | "fresh"
  | "stale"
  | "missing_document"
  | "builder_outdated";

export type ProjectIndexEntry = {
  document_id: string;
  document_name: string;
  revision: number;
  content_hash: string;
  graph_hash: string;
  builder_version: number;
  built_at: string;
  built_by: string;
  counts: {
    objects: number;
    equipment: number;
    valves: number;
    instruments: number;
    signals: number;
    lines: number;
    off_page_connectors: number;
    errors: number;
    warnings: number;
  };
  staleness: ProjectIndexStaleness;
  stale_reasons: string[];
};

/** How a cross-drawing connection was matched (tags only ever cross-check). */
export type OffPageConnectionMatch =
  | "reciprocal_declaration"
  | "reciprocal_declaration+service"
  | "service_convention"
  | "ambiguous"
  | "unresolved";

export type OffPageConnection = {
  /** Symmetric, tag-free identity derived from the two stable ends. */
  connection_id: string;
  source_document_id: string;
  source_engineering_id: string;
  source_tag: string;
  source_connection_endpoint_id: string;
  target_document_id: string;
  target_engineering_id: string;
  target_tag: string;
  target_connection_endpoint_id: string;
  direction: "in" | "out" | "";
  service: string;
  line_engineering_ids: string[];
  resolved: boolean;
  matched_by: OffPageConnectionMatch;
  tag_agrees: boolean;
  target_document_found: boolean;
  declared_by_document_ids: string[];
};

export type ProjectEngineeringGraph = {
  schema: "pid-agent.project-engineering-index";
  version: 2;
  generated_at: string;
  freshness: "cheap" | "verified";
  document_count: number;
  indexed_document_count: number;
  stale_document_ids: string[];
  unindexed_document_ids: string[];
  totals: EngineeringGraphCounts;
  documents: ProjectIndexEntry[];
  off_page_connections: OffPageConnection[];
  findings: EngineeringGraphFinding[];
};

/** One hit from the project-wide engineering-object lookup. */
export type EngineeringObjectMatch = {
  document_id: string;
  document_name: string;
  revision: number;
  engineering_id: string;
  kind: EngineeringObjectKind;
  tag: string;
  tag_key: string;
  identity_basis: EngineeringIdentityBasis;
  resolved_from: string;
};

export type ProjectIndexRebuildReport = {
  schema: "pid-agent.project-index-rebuild";
  version: 1;
  rebuilt: string[];
  unchanged: string[];
  removed: string[];
  stale_after: string[];
  duration_ms: number;
  built_by: string;
};
