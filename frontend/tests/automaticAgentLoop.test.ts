import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import {
  AssessmentContractViolation,
  MAX_REPLANS,
  assessmentContractViolation,
  automaticAgentReceipt,
  automaticAgentVerdict,
  completenessPresentation,
  driveAutomaticAgentPlan,
  type AutomaticAgentReplanRequest,
} from "../src/agent/automaticAgentLoop.ts";
import type {
  AgentRejectedOperationReceipt,
  AgentTransactionAssessment,
  SemanticAgentPlanResult,
} from "../src/types.ts";

/**
 * The shape of the real incident: 197 operations proposed, 120 accepted, 77 rejected.
 *
 * The numbers are not decoration. The defect being fixed here is that this exact plan was
 * reported as passing and the session was recorded as completed, so the regression fixture is
 * the incident's own arithmetic rather than a convenient `1 of 2`.
 */
const INCIDENT = { proposed: 197, accepted: 120, rejected: 77 } as const;

function receiptFor(index: number): AgentRejectedOperationReceipt {
  return {
    original_index: index,
    operation_id: `op${String(index).padStart(4, "0")}:add_element`,
    operation_kind: "add_element",
    reason_code: "unknown_symbol",
    message: "符号键不在目录中",
    field_path: "transaction.operations[0].element.symbol_key",
    available_values: { symbol_keys: ["buffer_tank", "purifier"] },
    suggestions: ["改用目录中的符号键"],
  };
}

function assessment(overrides: Partial<AgentTransactionAssessment>): AgentTransactionAssessment {
  return {
    valid: true,
    stage: "validate",
    document_id: "doc-a",
    current_revision: 7,
    next_revision: 8,
    semantic_operation_count: INCIDENT.proposed,
    compiled_operation_count: 354,
    resulting_element_count: 184,
    affected_element_ids: [],
    added_element_ids: [],
    updated_element_ids: [],
    deleted_element_ids: [],
    issues: [],
    ...overrides,
  };
}

function partialAssessment(): AgentTransactionAssessment {
  return assessment({
    operation_accounting: "evaluated",
    accepted_operation_count: INCIDENT.accepted,
    rejected_operation_count: INCIDENT.rejected,
    completeness: "partial",
    rejected_operations: Array.from({ length: INCIDENT.rejected }, (_, index) => receiptFor(index)),
  });
}

function completeAssessment(): AgentTransactionAssessment {
  return assessment({
    operation_accounting: "evaluated",
    accepted_operation_count: INCIDENT.proposed,
    rejected_operation_count: 0,
    completeness: "complete",
    rejected_operations: [],
  });
}

/** A legitimate "the compiler never got to this plan" answer, with the server's own reason. */
function notEvaluatedAssessment(): AgentTransactionAssessment {
  return assessment({
    valid: false,
    stage: "compile",
    operation_accounting: "not_evaluated",
    accepted_operation_count: null,
    rejected_operation_count: null,
    rejected_operations: null,
    global_failure_reason: "revision_conflict: 文档已在规划后被修改",
    issues: [
      {
        operation_index: null,
        operation: "",
        code: "revision_conflict",
        message: "文档已在规划后被修改",
        field_path: "expected_revision",
        available_values: {},
        suggestions: [],
      },
    ],
  });
}

function planResult(
  attempt: number,
  planAssessment: AgentTransactionAssessment,
): SemanticAgentPlanResult {
  return {
    session_id: "session-1",
    plan: {
      plan_id: `plan-${attempt}`,
      explanation: "test",
      transaction: { operations: [], expected_revision: 7, label: "test" },
    },
    compiled_plan: {
      explanation: "compiled",
      transaction: { operations: [], expected_revision: 7, label: "test" },
    },
    assessment: planAssessment,
    attempt,
    parent_plan_id: attempt ? `plan-${attempt - 1}` : null,
  };
}

function replanRecorder() {
  const requests: AutomaticAgentReplanRequest[] = [];
  return {
    requests,
    replan: async (request: AutomaticAgentReplanRequest) => {
      requests.push(request);
      return planResult(request.nextAttempt, completeAssessment());
    },
  };
}

test("a valid but partial proposal is never offered, and asks for the next one", async () => {
  const first = planResult(0, partialAssessment());
  const verdict = automaticAgentVerdict(first);

  assert.equal(verdict.valid, true, "the accepted operations are individually legal");
  assert.equal(verdict.completeness, "partial");
  assert.equal(verdict.mayProceed, false, "valid is not enough to reach a human");

  const recorder = replanRecorder();
  const driven = await driveAutomaticAgentPlan(first, recorder.replan);

  assert.equal(recorder.requests.length, 1, "the partial proposal asks for a repair");
  assert.equal(recorder.requests[0].nextAttempt, 1);
  assert.equal(recorder.requests[0].receipt.presentation.completeness, "partial");
  assert.equal(driven.verdicts.length, 2);
  assert.equal(driven.result.assessment.completeness, "complete");
});

test("the receipt carries the incident arithmetic and the reasons, not just a verdict", () => {
  const receipt = automaticAgentReceipt(planResult(0, partialAssessment()));

  assert.equal(receipt.presentation.proposed_operation_count, 197);
  assert.equal(receipt.presentation.accepted_operation_count, 120);
  assert.equal(receipt.presentation.rejected_operation_count, 77);
  assert.equal(receipt.rejected.length, 77);
  assert.equal(receipt.rejected[0].reasonCode, "unknown_symbol");
  assert.match(receipt.summary, /提案 197/);
  assert.match(receipt.summary, /接受 120/);
  assert.match(receipt.summary, /拒绝 77/);
  assert.match(receipt.summary, /partial/);
});

test("an explicit not_evaluated answer takes the error-recovery path", async () => {
  // A revision check that stopped compilation before any operation was examined. `0 of 0`
  // here would be the same family of lie as reporting a dropped plan as complete, and the
  // server's own reason is what the recovery uses.
  const unevaluated = planResult(0, notEvaluatedAssessment());

  const verdict = automaticAgentVerdict(unevaluated);
  assert.equal(verdict.branch, "not_evaluated");
  assert.equal(verdict.accounting, "not_evaluated");
  assert.equal(verdict.completeness, "unknown");
  assert.equal(verdict.accepted, null);
  assert.equal(verdict.rejected, null);
  assert.equal(verdict.mayProceed, false);
  assert.equal(verdict.mayReplanAutomatically, true);
  assert.deepEqual(assessmentContractViolation(unevaluated), []);
  assert.match(verdict.globalFailureReason, /revision_conflict/);

  const presentation = completenessPresentation(unevaluated);
  assert.equal(presentation.accepted_operation_count, null);
  assert.notEqual(presentation.accepted_operation_count, 0);

  const recorder = replanRecorder();
  await driveAutomaticAgentPlan(unevaluated, recorder.replan);
  assert.equal(recorder.requests.length, 1, "an uncounted proposal is asked again, not accepted");
});

test("a response that omits the accounting axis is a contract violation, not a plan to replan", async () => {
  // An older server, or a caller that simply does not set the field. This is not "unevaluated"
  // and it is not "unknown": the response does not carry the contract, so it must not be fed
  // back into the semantic replan loop, where it would spend the run's attempts hiding a
  // server defect and never fix it.
  const malformed = planResult(0, assessment({}));
  const verdict = automaticAgentVerdict(malformed);

  assert.equal(verdict.branch, "assessment_contract_violation");
  assert.equal(verdict.accounting, "violation");
  assert.equal(verdict.mayProceed, false);
  assert.equal(verdict.mayReplanAutomatically, false);
  assert.ok(verdict.contractViolation.some((item) => item.includes("operation_accounting")));

  const recorder = replanRecorder();
  await assert.rejects(
    () => driveAutomaticAgentPlan(malformed, recorder.replan),
    AssessmentContractViolation,
  );
  assert.equal(recorder.requests.length, 0, "zero replan attempts may be spent on it");

  const receipt = automaticAgentReceipt(malformed);
  assert.match(receipt.summary, /响应契约不兼容/);
  assert.equal(receipt.presentation.completeness, "unknown");
  assert.equal(receipt.presentation.accepted_operation_count, null);
});

test("an evaluated envelope missing its counts or verdict is a contract violation", () => {
  // `evaluated` is a promise that the counts and the verdict are there. Without them this is
  // not a partial plan and not an empty one -- there is nothing to call it.
  for (const broken of [
    assessment({ operation_accounting: "evaluated", rejected_operations: [], completeness: "complete" }),
    assessment({
      operation_accounting: "evaluated",
      accepted_operation_count: 3,
      rejected_operation_count: 0,
      rejected_operations: [],
    }),
    assessment({
      operation_accounting: "evaluated",
      accepted_operation_count: 3,
      rejected_operation_count: 0,
      completeness: "complete",
    }),
  ]) {
    const verdict = automaticAgentVerdict(planResult(0, broken));
    assert.equal(verdict.branch, "assessment_contract_violation");
    assert.equal(verdict.completeness, "unknown", "it is not partial and not empty");
    assert.equal(verdict.mayProceed, false);
    assert.ok(verdict.contractViolation.length > 0);
  }
});

test("a not_evaluated envelope that fabricates counts is a contract violation", () => {
  // The declared invariant, enforced at the boundary: zero is a claim, not a stand-in for an
  // unknown count, and a reason is required.
  const fabricated = automaticAgentVerdict(
    planResult(
      0,
      assessment({
        valid: false,
        operation_accounting: "not_evaluated",
        accepted_operation_count: 0,
        rejected_operation_count: 0,
        rejected_operations: null,
        completeness: null,
        global_failure_reason: "revision_conflict",
      }),
    ),
  );
  const unexplained = automaticAgentVerdict(
    planResult(0, assessment({ operation_accounting: "not_evaluated", rejected_operations: null })),
  );

  assert.equal(fabricated.branch, "assessment_contract_violation");
  assert.ok(fabricated.contractViolation.some((item) => item.includes("不得给出计数")));
  assert.equal(unexplained.branch, "assessment_contract_violation");
  assert.ok(unexplained.contractViolation.some((item) => item.includes("global_failure_reason")));
});

test("an evaluated, valid, complete proposal proceeds without another attempt", async () => {
  const recorder = replanRecorder();
  const driven = await driveAutomaticAgentPlan(planResult(0, completeAssessment()), recorder.replan);

  assert.equal(recorder.requests.length, 0);
  assert.equal(driven.result.assessment.completeness, "complete");
  assert.equal(automaticAgentVerdict(driven.result).mayProceed, true);
});

test("an empty proposal is an outcome, not a pass", async () => {
  const empty = planResult(
    0,
    assessment({
      valid: true,
      semantic_operation_count: 0,
      compiled_operation_count: 0,
      resulting_element_count: 0,
      operation_accounting: "evaluated",
      accepted_operation_count: 0,
      rejected_operation_count: 0,
      completeness: "empty",
      rejected_operations: [],
    }),
  );
  assert.equal(automaticAgentVerdict(empty).completeness, "empty");
  assert.equal(automaticAgentVerdict(empty).mayProceed, false);

  const recorder = replanRecorder();
  await driveAutomaticAgentPlan(empty, recorder.replan);
  assert.equal(recorder.requests.length, 1);
});

test("the same dead end twice stops instead of replanning forever", async () => {
  let calls = 0;
  const replan = async (request: AutomaticAgentReplanRequest) => {
    calls += 1;
    return planResult(request.nextAttempt, partialAssessment());
  };

  await assert.rejects(
    () => driveAutomaticAgentPlan(planResult(0, partialAssessment()), replan),
    /重复失败循环/,
  );
  assert.equal(calls, 1, "the repeat is detected before a second request is spent");
});

test("the attempt ceiling is the server's, not a second one invented here", () => {
  assert.equal(MAX_REPLANS, 5);
});

test("the loop stops when the run is cancelled", async () => {
  await assert.rejects(
    () =>
      driveAutomaticAgentPlan(planResult(0, partialAssessment()), replanRecorder().replan, {
        isCancelled: () => true,
      }),
    /已停止/,
  );
});

test("the surface names the arithmetic the contract declares", () => {
  // The contract declares four snake_case presentation fields; the surface that a human reads
  // before approving must carry those exact names, so a rename here cannot leave the contract
  // describing a presentation that no longer exists.
  const loop = readFileSync(join(process.cwd(), "src/agent/automaticAgentLoop.ts"), "utf8");
  const component = readFileSync(join(process.cwd(), "src/agent/ProposalAccounting.tsx"), "utf8");
  const surface = readFileSync(join(process.cwd(), "src/App.tsx"), "utf8");
  for (const field of [
    "proposed_operation_count",
    "accepted_operation_count",
    "rejected_operation_count",
    "completeness",
  ]) {
    assert.ok(loop.includes(field), `automaticAgentLoop.ts must name ${field}`);
    assert.ok(component.includes(field), `ProposalAccounting.tsx must render ${field}`);
  }
  assert.ok(surface.includes("ProposalAccounting"), "the confirmation surface must show it");

  // The contract enumerates six routes an assessment may take; the module that decides must
  // name all six, so a seventh cannot appear by silence and the malformed route cannot be
  // quietly folded into the unevaluated one.
  for (const branch of [
    "evaluated_complete",
    "evaluated_partial",
    "evaluated_empty",
    "evaluated_invalid",
    "not_evaluated",
    "assessment_contract_violation",
  ]) {
    assert.ok(loop.includes(branch), `automaticAgentLoop.ts must name the ${branch} branch`);
  }
});
