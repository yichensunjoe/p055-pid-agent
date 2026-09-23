"""M7 phase 2A: the runtime types that make a partial synthesis accountable.

Phase 1 declared the contract; this module is the part of it that exists at runtime. The
failure it is aimed at had three cooperating causes, and each has a type here:

* the assessment had one axis, so "the accepted operations are legal" and "the plan is
  whole" were the same answer — hence ``Completeness`` beside ``Validity``;
* nothing recorded what had been *proposed*, so the rejected 39% was unrecoverable from the
  database — hence ``SynthesisProposalEvidence``, which stores the raw proposal;
* the compiler computed a full diagnostic for every rejected operation and then discarded
  it — hence ``RejectedOperationReceipt``, which is that diagnostic, kept.

Two naming decisions are deliberate, because the obvious names collide:

* ``accepted_operation_count`` is the count of *semantic* operations the compiler retained
  (120 in the observed incident) while ``compiled_operation_count`` keeps its existing
  meaning of *low-level* operations produced (354 there, because one accepted semantic
  operation can expand into a symbol, a label and a leader). The invariant the remote gate
  wrote as "proposed = compiled + rejected" is implemented here as
  ``proposed == accepted + rejected``; calling the retained count "compiled" would have
  silently changed what an existing field means.
* ``completeness`` is *derived*, never supplied, because a caller that supplied both the
  operations and the completeness verdict could describe a dropped tail as complete.

``operation_accounting`` exists because "nothing was accepted" and "nothing was evaluated"
are different facts and must not share a representation. A plan that failed a revision check
before any operation was examined has no accepted and no rejected count, and recording
``0/0`` for it would be a lie of the same family as the original defect. When accounting was
not performed the counts are ``None`` — not zero — together with the diagnostic that stopped
it, and the two-axis verdict is ``None`` as well, because the matrix only describes
evaluated proposals.

``operation_id`` is derived from the operation's index and kind rather than from a digest,
so that two identical operations stay distinguishable in a receipt list and a receipt can
always be traced back to a position in the submitted proposal.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from .models import StrictModel

Validity = Literal["valid", "invalid"]
Completeness = Literal["complete", "partial", "empty"]
OperationAccounting = Literal["evaluated", "not_evaluated"]

#: The append-only carrier. Deliberately its own table rather than a column on
#: ``agent_tool_calls``: proposals and tool calls are not one-to-one (a session may propose,
#: be rejected, replan and propose again, and only the last attempt may ever become a tool
#: call), so storing evidence inside a tool call would reproduce the original defect — you
#: would again see only the final attempt and not the rejected plans.
PROPOSAL_EVIDENCE_TABLE = "synthesis_proposal_evidence"
PROPOSAL_EVIDENCE_SCHEMA = "m7-proposal-evidence/2"

#: The relations the two axes can take. ``partial`` is a first-class outcome, not an error.
COMPLETENESS_VALUES: tuple[Completeness, ...] = ("complete", "partial", "empty")

#: The two accounting states, spelled out because they must never be conflated.
OPERATION_ACCOUNTING_VALUES: tuple[OperationAccounting, ...] = ("evaluated", "not_evaluated")


def rejected_operation_id(index: int, kind: str) -> str:
    """A stable, traceable id for one rejected operation inside one proposal."""

    return f"op{index:04d}:{kind or 'unknown'}"


def derive_completeness(*, proposed: int, accepted: int, rejected: int) -> Completeness:
    """Derive the completeness axis. Raises when the counts cannot describe a proposal."""

    if min(proposed, accepted, rejected) < 0:
        raise ValueError("counts must not be negative")
    if proposed != accepted + rejected:
        raise ValueError(
            f"proposed ({proposed}) must equal accepted ({accepted}) + rejected ({rejected})"
        )
    if accepted == 0:
        return "empty"
    if rejected == 0:
        return "complete"
    return "partial"


class RejectedOperationReceipt(StrictModel):
    """Why one submitted operation did not make it into the drawing.

    Every field here is copied from a diagnostic the compiler had already produced and
    previously threw away; the receipt exists so the agent can repair rather than guess.
    """

    original_index: int = Field(ge=0)
    operation_id: str
    operation_kind: str
    reason_code: str
    message: str
    field_path: str = ""
    available_values: dict[str, list[str]] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)


def canonical_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_payload(value).encode("utf-8")).hexdigest()


class SynthesisProposalEvidence(StrictModel):
    """One submitted proposal and the verdict on it. Append-only; never updated in place.

    Exactly one row exists per proposal attempt that the system reached a terminal state on,
    including attempts that were never evaluated operation by operation. ``accepted``,
    ``rejected`` and the two-axis verdict are ``None`` in that case; they are never zero,
    because zero is a claim and the truth is that no claim was made.
    """

    proposal_evidence_id: str = Field(default_factory=lambda: f"m7ev_{uuid4().hex}")
    evidence_schema: str = PROPOSAL_EVIDENCE_SCHEMA
    session_id: str
    document_id: str
    proposal_attempt_index: int = Field(default=0, ge=0)

    # Who proposed it, so an offline reader does not need the provider to be reachable.
    provider_class: str = ""
    model: str = ""
    planner_identity: str = ""

    # What was proposed, in full: this is the part that was previously unrecoverable, and it
    # is recorded whatever the accounting state, because the raw proposal is durable evidence
    # in its own right.
    raw_proposed_operations: list[dict[str, Any]] = Field(default_factory=list)
    proposed_operation_count: int = Field(ge=0)

    operation_accounting: OperationAccounting = "evaluated"
    accepted_operation_count: int | None = Field(default=None, ge=0)
    compiled_operation_count: int | None = Field(default=None, ge=0)
    rejected_operation_count: int | None = Field(default=None, ge=0)
    rejected_operations: list[RejectedOperationReceipt] | None = None

    #: Set only when accounting was performed; the six-cell validity × completeness matrix
    #: describes evaluated proposals and only those.
    validity: Validity | None = None
    completeness: Completeness | None = None

    #: Why the proposal stopped before per-operation evaluation, when it did.
    global_failure_reason: str = ""

    compiler_version: str = ""
    proposal_payload_digest: str = ""
    assessment_digest: str = ""

    #: Optional relation only. It is never the container and never the authority: a proposal
    #: can be evaluated without any tool call existing, and a rejected proposal never gets one.
    related_tool_call_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def was_evaluated(self) -> bool:
        return self.operation_accounting == "evaluated"

    def is_success_candidate(self) -> bool:
        """Only a valid *and* complete proposal may be offered for authorisation."""

        return self.validity == "valid" and self.completeness == "complete"

    def problems(self) -> list[str]:
        """The machine invariants this row must satisfy. Empty means the row is coherent."""

        problems: list[str] = []
        if self.proposed_operation_count != len(self.raw_proposed_operations):
            problems.append("proposed_operation_count must equal len(raw_proposed_operations)")

        if not self.was_evaluated:
            # Not evaluated: no counts are invented and the verdict is absent, but the reason
            # must be on the record or the row explains nothing.
            if any(
                value is not None
                for value in (
                    self.accepted_operation_count,
                    self.rejected_operation_count,
                    self.compiled_operation_count,
                    self.rejected_operations,
                    self.validity,
                    self.completeness,
                )
            ):
                problems.append(
                    "a not_evaluated proposal must leave accepted/rejected/verdict null rather "
                    "than reporting zero"
                )
            if not self.global_failure_reason:
                problems.append("a not_evaluated proposal must record why it stopped")
            return problems

        if self.accepted_operation_count is None or self.rejected_operation_count is None:
            problems.append("an evaluated proposal must carry accepted and rejected counts")
            return problems
        if self.rejected_operations is None:
            problems.append("an evaluated proposal must carry a rejected-operation list")
            return problems
        if self.validity is None or self.completeness is None:
            problems.append("an evaluated proposal must carry both verdict axes")
            return problems

        if self.proposed_operation_count != self.accepted_operation_count + self.rejected_operation_count:
            problems.append("proposed_operation_count must equal accepted + rejected")
        if self.rejected_operation_count != len(self.rejected_operations):
            problems.append("rejected_operation_count must equal len(rejected_operations)")

        expected = derive_completeness(
            proposed=self.proposed_operation_count,
            accepted=self.accepted_operation_count,
            rejected=self.rejected_operation_count,
        )
        if self.completeness != expected:
            problems.append(
                f"completeness {self.completeness!r} does not follow from the counts "
                f"(expected {expected!r})"
            )

        if expected == "partial" and self.rejected_operation_count <= 0:
            problems.append("a partial proposal must have at least one rejected operation")
        if expected == "complete" and self.rejected_operation_count != 0:
            problems.append("a complete proposal must have no rejected operations")
        if expected == "empty" and self.accepted_operation_count != 0:
            problems.append("an empty proposal must have no accepted operations")

        for receipt in self.rejected_operations:
            if not receipt.reason_code:
                problems.append(
                    f"rejected operation {receipt.operation_id!r} has no reason_code"
                )
            if not (0 <= receipt.original_index < max(1, self.proposed_operation_count)):
                problems.append(
                    f"rejected operation {receipt.operation_id!r} has an out-of-range index"
                )
        return problems


def build_proposal_evidence(
    *,
    session_id: str,
    document_id: str,
    proposal_attempt_index: int,
    raw_proposed_operations: list[dict[str, Any]],
    accepted_operation_count: int,
    compiled_operation_count: int,
    rejected_operations: list[RejectedOperationReceipt],
    validity: Validity,
    provider_class: str = "",
    model: str = "",
    planner_identity: str = "",
    compiler_version: str = "",
    assessment: dict[str, Any] | None = None,
    related_tool_call_id: str | None = None,
) -> SynthesisProposalEvidence:
    """Assemble one evidence row for a proposal that *was* evaluated operation by operation."""

    proposed = len(raw_proposed_operations)
    rejected = len(rejected_operations)
    completeness = derive_completeness(
        proposed=proposed, accepted=accepted_operation_count, rejected=rejected
    )
    return SynthesisProposalEvidence(
        session_id=session_id,
        document_id=document_id,
        proposal_attempt_index=proposal_attempt_index,
        provider_class=provider_class,
        model=model,
        planner_identity=planner_identity,
        raw_proposed_operations=list(raw_proposed_operations),
        proposed_operation_count=proposed,
        operation_accounting="evaluated",
        accepted_operation_count=accepted_operation_count,
        compiled_operation_count=compiled_operation_count,
        rejected_operation_count=rejected,
        rejected_operations=list(rejected_operations),
        validity=validity,
        completeness=completeness,
        compiler_version=compiler_version,
        proposal_payload_digest=_digest(raw_proposed_operations),
        assessment_digest=_digest(assessment or {}),
        related_tool_call_id=related_tool_call_id,
    )


def build_not_evaluated_evidence(
    *,
    session_id: str,
    document_id: str,
    proposal_attempt_index: int,
    raw_proposed_operations: list[dict[str, Any]],
    global_failure_reason: str,
    provider_class: str = "",
    model: str = "",
    planner_identity: str = "",
    compiler_version: str = "",
    assessment: dict[str, Any] | None = None,
    related_tool_call_id: str | None = None,
) -> SynthesisProposalEvidence:
    """Assemble one evidence row for a proposal that stopped before per-operation evaluation.

    The raw submission is still durable evidence — that is the point — but no count is
    invented to fill the columns, and no verdict is fabricated for a plan the compiler never
    examined operation by operation.
    """

    return SynthesisProposalEvidence(
        session_id=session_id,
        document_id=document_id,
        proposal_attempt_index=proposal_attempt_index,
        provider_class=provider_class,
        model=model,
        planner_identity=planner_identity,
        raw_proposed_operations=list(raw_proposed_operations),
        proposed_operation_count=len(raw_proposed_operations),
        operation_accounting="not_evaluated",
        accepted_operation_count=None,
        compiled_operation_count=None,
        rejected_operation_count=None,
        rejected_operations=None,
        validity=None,
        completeness=None,
        global_failure_reason=global_failure_reason,
        compiler_version=compiler_version,
        proposal_payload_digest=_digest(raw_proposed_operations),
        assessment_digest=_digest(assessment or {}),
        related_tool_call_id=related_tool_call_id,
    )
