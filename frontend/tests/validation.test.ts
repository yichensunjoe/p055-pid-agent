import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  approvalDisclaimer,
  bindEvaluation,
  bindingNotice,
  filterIssues,
  hasBlockingWork,
  issueDetailRows,
  severityLabels,
  sortIssues,
  type ReleaseReadiness,
  type ValidationIssue,
  type ValidationResult,
} from "../src/validation.ts";

function issue(overrides: Partial<ValidationIssue> = {}): ValidationIssue {
  return {
    code: "TAG_DUPLICATE",
    severity: "error",
    object_ids: [],
    element_ids: ["sym_a"],
    message: "two symbols share the tag HV-101",
    expected: "",
    actual: "",
    suggested_repair: "",
    rule_source: "built-in",
    registered: true,
    threshold: null,
    waiver_status: "not_waived",
    waiver: null,
    validator_id: "engineering-report",
    rule_id: "engineering-report.TAG_DUPLICATE",
    profile_id: "built-in",
    profile_version: "1",
    details: {},
    ...overrides,
  };
}

function result(issues: ValidationIssue[]): ValidationResult {
  return {
    document_id: "doc_1",
    revision: 1,
    content_hash: "0".repeat(64),
    profile_id: "built-in",
    profile_version: "1",
    rule_bundle_fingerprint: "1".repeat(64),
    engine_version: "1",
    symbol_registry_fingerprint: "2".repeat(64),
    evaluated_at: "2026-09-19T12:00:00Z",
    validators_run: ["diagram-quality", "engineering-graph", "engineering-report"],
    validators_skipped: [],
    issues,
    counts: { blocker: 0, error: 0, warning: 0, info: 0, total: issues.length, waived: 0, unregistered: 0 },
    result_hash: "3".repeat(64),
  };
}

function readiness(overrides: Partial<ReleaseReadiness> = {}): ReleaseReadiness {
  return {
    document_id: "doc_1",
    revision: 1,
    profile_id: "built-in",
    profile_version: "1",
    rule_bundle_fingerprint: "1".repeat(64),
    state: "eligible",
    reasons: [],
    missing_required_validators: [],
    unwaived_blockers: [],
    unregistered_codes: [],
    waivers_considered: [],
    counts: { blocker: 0, error: 0, warning: 0, info: 0, total: 0, waived: 0, unregistered: 0 },
    human_approval_required: true,
    validation_hash: "3".repeat(64),
    readiness_hash: "4".repeat(64),
    evaluated_at: "2026-09-19T12:00:00Z",
    ...overrides,
  };
}

describe("validation view helpers", () => {
  it("orders issues worst first and keeps the server order inside one severity", () => {
    const sorted = sortIssues([
      issue({ code: "DUPLICATE_LABEL", severity: "warning" }),
      issue({ code: "TAG_DUPLICATE", severity: "error" }),
      issue({ code: "BRIDGE", severity: "info" }),
      issue({ code: "BLOCKED", severity: "blocker" }),
    ]);

    assert.deepEqual(
      sorted.map((item) => item.code),
      ["BLOCKED", "TAG_DUPLICATE", "DUPLICATE_LABEL", "BRIDGE"],
    );
  });

  it("keeps waived issues visible instead of hiding them", () => {
    const issues = [issue(), issue({ code: "WAIVED_ONE", waiver_status: "waived" })];

    assert.equal(filterIssues(issues, "", "all").length, 2);
    assert.deepEqual(
      filterIssues(issues, "", "waived").map((item) => item.code),
      ["WAIVED_ONE"],
    );
    assert.equal(filterIssues(issues, "", "error").length, 2);
  });

  it("filters on the fields a reviewer searches by", () => {
    const issues = [
      issue({ object_ids: ["EQ-101"], element_ids: ["sym_a"] }),
      issue({ code: "IR_ORPHAN_LINE", message: "line has no endpoint" }),
    ];

    assert.equal(filterIssues(issues, "eq-101").length, 1);
    assert.equal(filterIssues(issues, "orphan").length, 1);
    assert.equal(filterIssues(issues, "nothing here").length, 0);
  });

  it("shows every field needed to judge one issue", () => {
    const rows = issueDetailRows(
      issue({
        object_ids: ["EQ-101"],
        expected: ">= 95",
        actual: "92",
        threshold: 95,
        suggested_repair: "reroute the segment",
        waiver_status: "waived",
        waiver: {
          waiver_id: "W-1",
          actor: "chief-engineer",
          reason: "renumbered next revision",
          granted_at: "2026-09-19T12:00:00Z",
          expires_at: null,
          status: "waived",
        },
      }),
    );

    const byLabel = Object.fromEntries(rows.map((row) => [row.label, row.value]));
    assert.equal(byLabel["工程对象"], "EQ-101");
    assert.equal(byLabel["图元"], "sym_a");
    assert.equal(byLabel["期望 / 实际"], ">= 95 / 92");
    assert.equal(byLabel["生效阈值"], "95");
    assert.equal(byLabel["规则来源"], "built-in");
    assert.equal(byLabel["建议修复"], "reroute the segment");
    assert.equal(byLabel["豁免状态"], "已豁免");
    assert.ok(byLabel["豁免证据"].includes("W-1"));
  });

  it("marks an unregistered finding as such rather than calling it built-in", () => {
    const rows = issueDetailRows(issue({ registered: false, rule_source: "unregistered" }));
    const source = rows.find((row) => row.label === "规则来源");

    assert.ok(source?.value.includes("未注册"));
  });

  it("counts only unwaived blockers as blocking work", () => {
    assert.equal(hasBlockingWork(result([issue({ severity: "blocker" })])), true);
    assert.equal(
      hasBlockingWork(result([issue({ severity: "blocker", waiver_status: "waived" })])),
      false,
    );
  });

  it("states that validation is evidence, not approval", () => {
    assert.ok(approvalDisclaimer().includes("不会批准"));
    assert.ok(Object.values(severityLabels).includes("阻断"));
  });

  it("accepts a readiness verdict that names the validation result on screen", () => {
    const binding = bindEvaluation(result([issue()]), readiness());

    assert.equal(binding.bound, true);
    assert.deepEqual(binding.mismatches, []);
    assert.ok(bindingNotice(binding).includes("同一评估时刻"));
  });

  it("refuses to present two different evaluation instants as one decision", () => {
    // The exact failure the panel used to have: readiness re-ran validation at its own
    // `now`, so its validation_hash names a result the reviewer never saw.
    const binding = bindEvaluation(
      result([issue()]),
      readiness({
        evaluated_at: "2026-09-19T12:00:01Z",
        validation_hash: "9".repeat(64),
      }),
    );

    assert.equal(binding.bound, false);
    assert.deepEqual(binding.mismatches, ["evaluated_at", "validation_hash"]);
    assert.ok(bindingNotice(binding).includes("不一致"));
  });

  it("refuses a pair that describes different revisions, profiles or rule bundles", () => {
    const moved = bindEvaluation(
      result([issue()]),
      readiness({ revision: 2, rule_bundle_fingerprint: "8".repeat(64) }),
    );
    const other_profile = bindEvaluation(
      result([issue()]),
      readiness({ profile_id: "company", profile_version: "3" }),
    );
    const other_document = bindEvaluation(
      result([issue()]),
      readiness({ document_id: "doc_2" }),
    );

    assert.deepEqual(moved.mismatches, ["revision", "rule_bundle_fingerprint"]);
    assert.deepEqual(other_profile.mismatches, ["profile_id", "profile_version"]);
    assert.deepEqual(other_document.mismatches, ["document_id"]);
  });
});
