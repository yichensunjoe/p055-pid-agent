import type { Operation } from "./types";

/**
 * Types for the deterministic drafting engine (Charter M3).
 *
 * The engine is preview-only: `DraftingPreview.transaction` is an ordinary document
 * transaction the editor applies through the same governed channel as a manual edit,
 * so drafting has no private write path.
 */

export type DraftingSeverity = "info" | "warning" | "error" | "blocker";

export type DraftingFinding = {
  severity: DraftingSeverity;
  code: string;
  message: string;
  element_ids: string[];
  details: Record<string, unknown>;
  /** `drafting` for this engine's own rules, `diagram_quality` for the drawing rules. */
  rule_source: string;
  /** A waived finding is still reported; only the gate stops blocking on it. */
  waived: boolean;
};

export type DraftingSnapshot = {
  score: number;
  passed: boolean;
  error_issue_count: number;
  warning_issue_count: number;
  symbol_count: number;
  connector_count: number;
  junction_count: number;
  dangling_junction_count: number;
  node_overlaps: number;
  crowded_node_pairs: number;
  pipe_obstacle_intersections: number;
  geometric_crossings: number;
  unbridged_crossings: number;
  total_bends: number;
  non_orthogonal_segments: number;
  micro_segments: number;
  unnecessary_bends: number;
  text_text_overlaps: number;
  text_symbol_overlaps: number;
  text_connector_intersections: number;
  duplicate_label_count: number;
  out_of_bounds_symbols: number;
  out_of_bounds_connector_points: number;
  reserved_region_intrusions: number;
  total_route_length: number;
};

export type DraftingMetrics = {
  before: DraftingSnapshot;
  after: DraftingSnapshot;
  improvements: string[];
  regressions: string[];
};

export type DraftingGate = {
  passed: boolean;
  score: number;
  target_score: number;
  blockers: DraftingFinding[];
  drawing_issues: DraftingFinding[];
  waived_codes: string[];
  checked_codes: string[];
};

export type DraftingLocks = {
  locked_element_ids: string[];
  request_element_ids: string[];
  metadata_element_ids: string[];
  region_element_ids: string[];
  region_labels: string[];
};

export type DraftingPort = {
  element_id: string;
  element_kind: "symbol" | "junction";
  port_id: string;
  port_name: string;
  direction: "in" | "out" | "bidirectional" | "none";
  medium: string;
  side: "left" | "right" | "top" | "bottom" | "interior";
  outward_normal: [number, number] | null;
  point: { x: number; y: number };
  connected_element_ids: string[];
  connector_ids: string[];
};

export type DraftingCrossing = {
  x: number;
  y: number;
  first_connector_id: string;
  second_connector_id: string;
  bridged: boolean;
  bridged_by: string;
  at_junction_id: string;
};

export type DraftingJunction = {
  element_id: string;
  degree: number;
  kind: "branch" | "inline" | "dangling";
  connector_ids: string[];
  element_ids: string[];
};

export type DraftingReport = {
  schema: "pid-agent.drafting-report";
  version: number;
  engine_version: number;
  document_id: string;
  document_name: string;
  revision: number;
  content_hash: string;
  input_content_hash: string;
  scope_kind: "document" | "region" | "selection";
  scope_element_ids: string[];
  locks: DraftingLocks;
  ports: DraftingPort[];
  collisions: DraftingFinding[];
  crossings: DraftingCrossing[];
  junctions: DraftingJunction[];
  findings: DraftingFinding[];
  gate: DraftingGate;
  score: number;
};

export type DraftingReproducibility = {
  engine_version: number;
  algorithm: string;
  input_content_hash: string;
  output_content_hash: string;
  transaction_digest: string;
  operation_count: number;
  settled: boolean;
};

export type DraftingPreview = {
  valid: boolean;
  document_id: string;
  document_name: string;
  current_revision: number;
  transaction: { expected_revision: number; label: string; operations: Operation[] } | null;
  settled: boolean;
  moved_element_ids: string[];
  rerouted_connector_ids: string[];
  moved_annotation_ids: string[];
  bridged_connector_ids: string[];
  locked_element_ids: string[];
  locks: DraftingLocks;
  skipped_locked_element_ids: string[];
  in_scope_element_ids: string[];
  collisions: DraftingFinding[];
  crossings: DraftingCrossing[];
  junctions: DraftingJunction[];
  warnings: string[];
  metrics: DraftingMetrics;
  gate: DraftingGate;
  findings: DraftingFinding[];
  reproducibility: DraftingReproducibility;
};

export type DraftingRegion = {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  label: string;
};

export type DraftingOptions = {
  expected_revision?: number | null;
  region?: DraftingRegion | null;
  element_ids?: string[];
  locked_element_ids?: string[];
  direction?: "horizontal" | "vertical";
  rank_gap?: number;
  node_gap?: number;
  component_gap?: number;
  relayout?: boolean;
  reroute_connectors?: boolean;
  place_annotations?: boolean;
  bridge_crossings?: boolean;
  resolve_collisions?: boolean;
  include_hidden?: boolean;
  policy?: {
    target_score?: number;
    obstacle_margin?: number;
    lane_gap?: number;
    route_clearance?: number;
    max_bends?: number;
    collision_passes?: number;
    annotation_passes?: number;
    pipeline_rounds?: number;
    waived_codes?: string[];
  };
};

/** A declared drawing region: `lock` freezes what is inside, anything else reserves it. */
export type LayoutRegion = {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  label?: string;
  kind?: "lock" | "legend" | "title_block" | "notes" | "keep_clear";
};
