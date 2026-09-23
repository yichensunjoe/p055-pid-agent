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
