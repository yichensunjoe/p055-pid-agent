from __future__ import annotations

from .agent_semantic_models import (
    CompiledSemanticTransaction,
    SemanticOperation,
    SemanticTransaction,
)
from .m7_synthesis_models import (
    RejectedOperationReceipt,
    derive_completeness,
    rejected_operation_id,
)
from .semantic_compiler_engine import (
    SemanticTransactionCompiler as StrictSemanticTransactionCompiler,
)

#: Recorded on every proposal record so a later reader can tell which compiler judged it.
COMPILER_VERSION = "permissive-semantic-compiler/1"


class PermissiveSemanticTransactionCompiler(StrictSemanticTransactionCompiler):
    """Compile as much of a model plan as can be applied safely.

    A single malformed or unsupported drawing operation should not discard an
    otherwise useful P&ID. The strict compiler remains authoritative for every
    operation that is retained; invalid operations are skipped individually.
    Revision conflicts are never bypassed, and no low-level transaction is
    returned unless it passes the normal document validation.

    Dropping an operation is where this compiler used to become a liability. It kept the
    surviving operations and discarded the rest silently, so a plan that lost 39% of its own
    operations came back marked valid and was recorded as a completed session. Every skip now
    produces a receipt carrying the diagnostic the strict compiler had already computed
    (reason code, field path, available values, suggestions), and the assessment reports the
    proposed / accepted / rejected counts plus the completeness axis derived from them.
    """

    def compile(
        self,
        document_id: str,
        transaction: SemanticTransaction,
    ) -> CompiledSemanticTransaction:
        proposed = len(transaction.operations)
        strict_result = super().compile(document_id, transaction)
        if strict_result.assessment.valid and strict_result.transaction is not None:
            # Nothing was skipped, so the plan is whole by construction.
            return self._with_accounting(
                strict_result,
                proposed=proposed,
                accepted=len(transaction.operations),
                receipts=[],
            )
        if any(issue.code == "revision_conflict" for issue in strict_result.assessment.issues):
            # The plan was never evaluated operation by operation, so there is no per-operation
            # accounting to report and none is invented. The existing recovery path handles it.
            #
            # The state is stated rather than left to the model's default, and the reason is
            # carried: a client has to distinguish "we never got to this plan, here is why"
            # from "this response does not carry the accounting contract at all", and only the
            # first of those is worth recovering from.
            first = strict_result.assessment.issues[0]
            reason = first.code if first is not None else "not_evaluated"
            if first is not None and first.message:
                reason = f"{reason}: {first.message}"
            return strict_result.model_copy(
                update={
                    "assessment": strict_result.assessment.model_copy(
                        update={
                            "operation_accounting": "not_evaluated",
                            "global_failure_reason": reason,
                        }
                    )
                }
            )

        current = self.service.get_document(document_id)
        accepted: list[SemanticOperation] = []
        receipts: list[RejectedOperationReceipt] = []
        for index, operation in enumerate(transaction.operations):
            candidate = transaction.model_copy(
                update={
                    "expected_revision": current.revision,
                    "operations": [*accepted, operation],
                },
                deep=True,
            )
            result = super().compile(document_id, candidate)
            if result.assessment.valid and result.transaction is not None:
                accepted.append(operation)
                continue
            receipts.append(self._rejection_receipt(index, operation, result.assessment))

        if not accepted:
            # Every operation was examined and every one was refused. That is a real,
            # evaluated result: 0 accepted of `proposed` reviewed, and it must be recorded as
            # such rather than left looking unevaluated.
            return self._with_accounting(
                strict_result, proposed=proposed, accepted=0, receipts=receipts
            )

        skipped = proposed - len(accepted)
        label = transaction.label or "Agent semantic transaction"
        if skipped:
            label = f"{label} · applied {len(accepted)}/{len(transaction.operations)} operations"
        recovered = transaction.model_copy(
            update={
                "expected_revision": current.revision,
                "operations": accepted,
                "label": label,
            },
            deep=True,
        )
        return self._with_accounting(
            super().compile(document_id, recovered),
            proposed=proposed,
            accepted=len(accepted),
            receipts=receipts,
        )

    @staticmethod
    def _rejection_receipt(
        index: int,
        operation: SemanticOperation,
        assessment,
    ) -> RejectedOperationReceipt:
        """Turn the strict compiler's refusal into something the agent can act on."""

        kind = getattr(operation, "op", "") or type(operation).__name__
        issue = assessment.issues[0] if assessment.issues else None
        return RejectedOperationReceipt(
            original_index=index,
            operation_id=rejected_operation_id(index, kind),
            operation_kind=kind,
            reason_code=(issue.code if issue is not None else "operation_rejected"),
            message=(issue.message if issue is not None else "operation was not accepted"),
            field_path=(issue.field_path if issue is not None else ""),
            available_values=(issue.available_values if issue is not None else {}),
            suggestions=(issue.suggestions if issue is not None else []),
        )

    @staticmethod
    def _with_accounting(
        compiled: CompiledSemanticTransaction,
        *,
        proposed: int,
        accepted: int,
        receipts: list[RejectedOperationReceipt],
    ) -> CompiledSemanticTransaction:
        completeness = derive_completeness(
            proposed=proposed, accepted=accepted, rejected=len(receipts)
        )
        assessment = compiled.assessment.model_copy(
            update={
                "semantic_operation_count": proposed,
                "operation_accounting": "evaluated",
                "accepted_operation_count": accepted,
                "rejected_operation_count": len(receipts),
                "completeness": completeness,
                "rejected_operations": receipts,
            }
        )
        return compiled.model_copy(update={"assessment": assessment})
