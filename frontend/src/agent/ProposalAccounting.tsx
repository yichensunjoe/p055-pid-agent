import { automaticAgentReceipt, automaticAgentVerdict, completenessPresentation } from "./automaticAgentLoop";
import type { SemanticAgentPlanResult } from "../types";

/**
 * The arithmetic of a proposal, shown wherever a human is asked to decide.
 *
 * A confirmation surface that shows only `valid` is how a plan missing 39% of its own operations
 * looked like a plan that passed: the counts are what make that visible, and `partial` has to be
 * readable without opening anything.
 */
export function ProposalAccounting({ result }: { result: SemanticAgentPlanResult }) {
  const presentation = completenessPresentation(result);
  const verdict = automaticAgentVerdict(result);
  const reasonCodes = Array.from(new Set(verdict.rejectedReasonCodes));
  if (verdict.branch === "assessment_contract_violation") {
    // Not a proposal with unknown numbers: a response that does not carry the accounting
    // contract. Showing "?" counts here would invite someone to approve it anyway.
    return (
      <p className="proposal-accounting proposal-accounting-violation">
        <span>响应契约不兼容，无法确认这笔账</span>
        <span>{verdict.contractViolation.join("；")}</span>
        <span>请刷新或重新生成；已拒绝应用，也不会自动消耗重规划次数。</span>
      </p>
    );
  }
  if (verdict.accounting === "not_evaluated") {
    return (
      <p className="proposal-accounting proposal-accounting-not_evaluated">
        <span>提案 {presentation.proposed_operation_count ?? "?"}</span>
        <span>未逐项评估</span>
        <span>{verdict.globalFailureReason || "服务端未给出原因"}</span>
        <span className="proposal-accounting-summary">{automaticAgentReceipt(result).summary}</span>
      </p>
    );
  }
  return (
    <p className={`proposal-accounting proposal-accounting-${presentation.completeness}`}>
      <span>提案 {presentation.proposed_operation_count ?? "?"}</span>
      <span>接受 {presentation.accepted_operation_count ?? "?"}</span>
      <span>拒绝 {presentation.rejected_operation_count ?? "?"}</span>
      <span>完整性 {presentation.completeness}</span>
      {reasonCodes.length ? <span>拒绝原因 {reasonCodes.join(", ")}</span> : null}
      <span className="proposal-accounting-summary">{automaticAgentReceipt(result).summary}</span>
    </p>
  );
}
