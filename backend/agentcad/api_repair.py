"""Engineering self-repair surfaces (Charter §50; remote baseline M5 §M5-5).

Two routes, and the split is the point:

* ``POST /repair/documents/{id}/preview`` — plan, compile, shadow-validate and judge the
  candidate, and **write nothing**. This is what an agent or a UI calls while deciding.
* ``POST /repair/documents/{id}`` — the same run with the governed apply allowed, so a drawing
  changes only through the one audited write path every other edit uses.

There is exactly one success oracle and one orchestrator behind both, and the payload they
return is the canonical one the CLI, MCP and the benchmark publish. A caller cannot supply a
plan, a waiver, a profile, a threshold, a success verdict or a validation hash of its own
choosing: it names a finding, and the server decides what repairing it means. Without that,
"the agent said it fixed it" would be a client-side claim.

The routes also cannot approve anything. A repair that passes the oracle leaves the drawing
*repaired* and the document no more released than before (Charter §11, §44-10).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .models import StrictModel
from .release_validator import assess_document_release_readiness
from .repair_models import repair_payload
from .repair_orchestrator import RepairStaleEvidence, repair_document_finding
from .service import DocumentNotFoundError, DocumentService
from .validation_engine import ValidationTimeError, normalize_evaluation_time
from .validation_profile import ProfileError, load_profile


class RepairSurfaceRequest(StrictModel):
    """What a caller is allowed to decide: which finding, and how far the repair may reach.

    Everything else about a repair — the rule bundle, the budgets, the oracle, the single-write
    rule — is server-side policy. ``permits_creation`` defaults to false for the same reason it
    is false in every benchmark case that does not need it: putting deleted equipment back is a
    real capability, and a capability should be granted by the caller that needs it rather than
    assumed by the one that does not.
    """

    code: str = ""
    validator_id: str = ""
    element_ids: list[str] = Field(default_factory=list)
    hop: int = Field(default=1, ge=1, le=2)
    max_touched_existing_ids: int | None = Field(default=None, ge=0, le=64)
    permits_creation: bool = False
    max_created_ids: int = Field(default=0, ge=0, le=1)


def create_repair_router(service: DocumentService) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent engineering self-repair"])

    def _moment(as_of: datetime | None) -> datetime:
        try:
            return normalize_evaluation_time(as_of)
        except ValidationTimeError as exc:
            raise HTTPException(
                status_code=422,
                detail="as_of must carry a timezone, e.g. 2026-09-20T12:00:00Z",
            ) from exc

    def _run(
        document_id: str,
        request: RepairSurfaceRequest,
        *,
        as_of: datetime | None,
        apply_enabled: bool,
    ) -> dict:
        try:
            profile = load_profile()
        except ProfileError as exc:
            raise HTTPException(status_code=500, detail=f"{exc.code}: {exc.message}") from exc
        try:
            run = repair_document_finding(
                service,
                profile,
                document_id,
                target_code=request.code or None,
                validator_id=request.validator_id,
                element_ids=list(request.element_ids) or None,
                hop=request.hop,
                max_touched_existing_ids=request.max_touched_existing_ids,
                declared_by="surface_request",
                permits_creation=request.permits_creation,
                max_created_ids=request.max_created_ids,
                apply_enabled=apply_enabled,
                now=_moment(as_of),
            )
        except DocumentNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail=f"document not found: {document_id}"
            ) from exc
        except RepairStaleEvidence as exc:
            # The finding moved, disappeared or was waived between the caller reading it and
            # asking for it. That is a client-view-is-stale problem, not a server failure.
            raise HTTPException(status_code=409, detail=f"{exc.code}: {exc.args[0]}") from exc
        # The canonical public payload, identical to the CLI's and MCP's: ``schema``, not
        # ``schema_name``, and no machine-scoped fields (document id, audit id, timings).
        payload = repair_payload(run)
        payload["release_state"] = assess_document_release_readiness(
            service, document_id, profile, now=_moment(as_of)
        ).state
        return payload

    @router.post("/documents/{document_id}/repair/preview")
    def preview_repair(
        document_id: str,
        request: RepairSurfaceRequest | None = None,
        as_of: datetime | None = None,
    ) -> dict:
        """Plan and judge a repair without writing it.

        The answer is the same record the apply route would publish, with ``applied.applied``
        false: the candidate that *would* be written, the attempts that were refused, and the
        oracle's verdict on each. ``release_state`` is read from the release validator, never
        inferred here — a repair changes a drawing, not a release status.
        """

        return _run(
            document_id,
            request or RepairSurfaceRequest(),
            as_of=as_of,
            apply_enabled=False,
        )

    @router.post("/documents/{document_id}/repair")
    def apply_repair(
        document_id: str,
        request: RepairSurfaceRequest | None = None,
        as_of: datetime | None = None,
    ) -> dict:
        """Repair one finding, through the same run the preview showed.

        One governed write, one revision, one audit chain — and only after the shadow candidate
        passed the success oracle. A refused candidate leaves the drawing byte-identical, which
        is why this route is safe to call on a drawing the caller cannot undo.
        """

        return _run(
            document_id,
            request or RepairSurfaceRequest(),
            as_of=as_of,
            apply_enabled=True,
        )

    return router


__all__ = ["RepairSurfaceRequest", "create_repair_router"]
