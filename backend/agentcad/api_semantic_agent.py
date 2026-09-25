from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import Field

from .agent_semantic import analyze_transaction
from .agent_semantic_models import (
    AgentOperationIssue,
    AgentTransactionAssessment,
    CompiledSemanticTransaction,
    SemanticAgentApplyRequest,
    SemanticAgentPlanResult,
)
from .api_harness import _raise_harness_error
from .audit_models import AuditContext
from .auto_layout_canvas import derive_semantic_canvas
from .auto_layout_geometry import (
    annotate_semantic_layout,
    materialize_semantic_layout,
    route_semantic_layout,
)
from .auto_layout_identity import finalize_semantic_layout
from .auto_layout_semantic import (
    SemanticTopologyIngressError,
    place_semantic_layout,
    plan_semantic_layout,
)
from .diagnostics import DiagnosticLogger
from .flow_topology import build_agent_harness_context
from .harness import AgentHarnessService
from .harness_models import AgentSessionCreateRequest
from .llm import PlannerError
from .m7_diagram_adapter import adapt
from .m7_diagram_spec import load_diagram_spec
from .m7_layout_contract import M7_LAYOUT_CONTRACT_VERSION
from .m7_layout_materialization import (
    MaterializationCanvasError,
    MaterializationError,
    MaterializationTargetNotEmptyError,
    apply_materialized_layout,
    materialize_canonical_layout,
)
from .m7_symbol_geometry import SymbolGeometryError, freeze_symbol_geometry
from .m7_synthesis_models import build_not_evaluated_evidence, build_proposal_evidence
from .m7_text_planner import TypesafeDiagramSpecPlanner
from .models import AgentPlan, StrictModel, TransactionRequest, TransactionResult
from .permissive_semantic_compiler import (
    COMPILER_VERSION,
    PermissiveSemanticTransactionCompiler,
)
from .revision_diagnostics import emit_revision_diagnostics
from .semantic_planner import SemanticAgentPlanner
from .service import (
    DocumentNotFoundError,
    DocumentService,
    InvalidOperationError,
    RevisionConflictError,
)
from .tool_registry import get_default_tool_registry
from .typesafe import (
    DEFAULT_CONFIDENCE_FLOOR,
    TypesafeClient,
    TypesafeError,
    resolve_typesafe_config,
    typesafe_status,
)
from .typesafe_planner import TypesafeSemanticPlanner
from .vision_request_models import (
    VisionAgentGenerateRequest,
    VisionSemanticAgentReplanRequest,
)

VisionPlanningRequest = VisionAgentGenerateRequest | VisionSemanticAgentReplanRequest


class TypesafePlanRequest(StrictModel):
    """A drawing request TypeSafe plans. The key may travel with the request or live in the server."""

    prompt: str = Field(min_length=1, max_length=100_000)
    context: str = Field(default="", max_length=200_000)
    expected_revision: int | None = Field(default=None, ge=0)
    require_visible_output: bool = True
    confidence_floor: float = Field(default=DEFAULT_CONFIDENCE_FLOOR, ge=0, le=1)
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: float | None = Field(default=None, gt=0)


class TypesafeVerifyRequest(StrictModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: float | None = Field(default=None, gt=0)


class TextPlanRequest(StrictModel):
    """One sentence the natural-language surface draws.

    ``dry_run`` plans and finalizes the layout but never touches the document: preview is a
    request flag on the write path, not a second endpoint that could drift away from it.
    """

    sentence: str = Field(min_length=1, max_length=10_000)
    dry_run: bool = False
    confidence_floor: float = Field(default=DEFAULT_CONFIDENCE_FLOOR, ge=0, le=1)
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    timeout_seconds: float | None = Field(default=None, gt=0)


class TextPlanResult(StrictModel):
    """What the sentence became: the specification, the layout identities, and the write fact."""

    document_id: str
    committed: bool
    #: The revision the governed writer committed; ``None`` on a dry run, so a caller can tell
    #: "previewed" from "drawn" without trusting a status string.
    revision: int | None = None
    spec: dict[str, Any]
    #: "complete" | "partial" | "empty": whether the specification is the whole sentence. A
    #: commit response is always "complete"; a preview may be partial and says what is missing
    #: through ``undelivered``.
    completeness: str = "complete"
    undelivered: list[str] = Field(default_factory=list)
    #: Machine-visible catalogue-gap records (schema = CATALOG_GAP_REQUIRED_FIELDS). The
    #: alternatives are reporting only; they never become judgment candidates.
    catalog_gaps: list[dict] = Field(default_factory=list)
    canonical_layout_digest: str
    materialization_digest: str
    notes: list[str]
    skipped: list[str]
    unknown_tags: list[str]
    model: str
    latency_ms: float
    question_count: int
    judgment_count: int


def _finalize_spec_layout(spec) -> Any:
    """The frozen deterministic chain, as the phase-2B/3 proofs run it: no coordinates in.

    Kept as one helper so the route reads as the pipeline it is; every stage is the proved
    module, and the caller never sees an intermediate layout it could mistake for the result.
    """

    topology = adapt(load_diagram_spec(spec.model_dump(mode="json")))
    snapshot = freeze_symbol_geometry(entity.symbol_key for entity in spec.entities)
    staged = plan_semantic_layout(topology)
    placed = place_semantic_layout(staged)
    materialized = materialize_semantic_layout(placed, snapshot)
    routed = route_semantic_layout(materialized, snapshot)
    annotated = annotate_semantic_layout(
        routed, {entity.engineering_id: entity.tag for entity in spec.entities}
    )
    canvassed = derive_semantic_canvas(annotated)
    return finalize_semantic_layout(canvassed, topology)


@dataclass(frozen=True)
class _TypesafePlanInput:
    """What the planner reads, so its ``plan`` signature stays the one the model planner has."""

    prompt: str
    typesafe_config: Any
    expected_revision: int | None = None


def _provider_fields(request: VisionPlanningRequest) -> dict[str, Any]:
    provider = request.provider
    return {
        "base_url": provider.base_url if provider else None,
        "model": provider.model if provider else None,
        "timeout_seconds": provider.timeout_seconds if provider else None,
        "api_key_present": bool(provider and provider.api_key),
        "reference_image_count": len(request.images),
        "require_visible_output": request.require_visible_output,
    }


def _enforce_visible_output_requirement(
    service: DocumentService,
    document_id: str,
    required: bool,
    compiled: CompiledSemanticTransaction,
) -> CompiledSemanticTransaction:
    """Apply the web-agent-only empty-canvas contract after permissive compilation.

    Generic semantic compilation remains valid for MCP and programmatic layer/system
    management. Callers must opt in when their user interaction explicitly requires
    a visible drawing result.
    """
    if not required or not compiled.assessment.valid:
        return compiled
    current = service.get_document(document_id)
    if current.elements or (compiled.assessment.resulting_element_count or 0) > 0:
        return compiled
    issue = AgentOperationIssue(
        operation_index=None,
        operation="transaction",
        code="empty_full_diagram",
        message=(
            "当前网页 Agent 请求要求生成可见图形，但编译结果只包含图层或系统等结构操作。"
            "请在同一事务中添加设备、管线或仪表。"
        ),
        field_path="transaction.operations",
        suggestions=[
            "添加至少一个真实设备、阀门、管线或仪表元素。",
            "如果用户只要求管理图层或系统，请将 require_visible_output 设为 false。",
        ],
    )
    assessment = compiled.assessment.model_copy(
        update={"valid": False, "issues": [*compiled.assessment.issues, issue]},
        deep=True,
    )
    return compiled.model_copy(
        update={"transaction": None, "assessment": assessment},
        deep=True,
    )


def _operation_types(plan, compiled) -> dict[str, Any]:
    return {
        "semantic_operation_types": [item.op for item in plan.transaction.operations],
        "compiled_operation_types": (
            [item.op for item in compiled.transaction.operations]
            if compiled.transaction is not None
            else []
        ),
        "annotation_metrics": (
            compiled.annotation_metrics.model_dump(mode="json")
            if compiled.annotation_metrics is not None
            else None
        ),
        "diagram_quality": (
            compiled.diagram_quality.model_dump(mode="json", by_alias=True)
            if compiled.diagram_quality is not None
            else None
        ),
    }


def _result(
    session_id: str,
    plan,
    compiled,
    *,
    attempt: int,
    parent_plan_id: str | None = None,
) -> SemanticAgentPlanResult:
    compiled_plan = (
        AgentPlan(explanation=plan.explanation, transaction=compiled.transaction)
        if compiled.transaction is not None
        else None
    )
    return SemanticAgentPlanResult(
        session_id=session_id,
        plan=plan,
        compiled_plan=compiled_plan,
        assessment=compiled.assessment,
        attempt=attempt,
        parent_plan_id=parent_plan_id,
        annotation_metrics=compiled.annotation_metrics,
        diagram_quality=compiled.diagram_quality,
    )


def _raise_service_error(exc: Exception):
    if isinstance(exc, DocumentNotFoundError):
        raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc
    if isinstance(exc, RevisionConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


PLANNER_IDENTITY = "semantic-agent/plan-v2"


def _record_proposal_evidence(
    harness: AgentHarnessService,
    *,
    session_id: str,
    document_id: str,
    plan,
    compiled,
    request: VisionPlanningRequest,
) -> None:
    """Persist what was proposed, before anyone can replan on top of it.

    Recorded whenever the compiler performed per-operation accounting, which is exactly the
    case that used to leave no trace. The row keeps the *submitted* operations, not the
    accepted ones, because the difference between them is the finding.
    """

    assessment = compiled.assessment
    previous = harness.latest_synthesis_proposal_evidence(session_id)
    attempt = previous.proposal_attempt_index + 1 if previous is not None else 0
    provider = request.provider
    common = {
        "session_id": session_id,
        "document_id": document_id,
        "proposal_attempt_index": attempt,
        "raw_proposed_operations": [
            operation.model_dump(mode="json") for operation in plan.transaction.operations
        ],
        "provider_class": "llm" if provider is not None and provider.base_url else "unknown",
        "model": (provider.model if provider is not None and provider.model else ""),
        "planner_identity": PLANNER_IDENTITY,
        "compiler_version": COMPILER_VERSION,
        "assessment": assessment.model_dump(mode="json"),
    }
    if assessment.operation_accounting == "evaluated":
        evidence = build_proposal_evidence(
            **common,
            accepted_operation_count=assessment.accepted_operation_count,
            compiled_operation_count=assessment.compiled_operation_count,
            rejected_operations=list(assessment.rejected_operations),
            validity="valid" if assessment.valid else "invalid",
        )
    else:
        # The proposal still reached a terminal state, so the raw submission is durable
        # evidence in its own right. No count is invented for a plan the compiler never
        # examined operation by operation; the reason it stopped is recorded instead. The
        # compiler already put that reason on the assessment, so the record and the client
        # read the same sentence rather than two renderings of it.
        first_issue = assessment.issues[0] if assessment.issues else None
        reason = assessment.global_failure_reason or (
            first_issue.code if first_issue is not None else "not_evaluated"
        )
        evidence = build_not_evaluated_evidence(
            **common,
            global_failure_reason=reason,
        )
    harness.record_synthesis_proposal_evidence(evidence)


def _with_harness_context(
    service: DocumentService,
    document_id: str,
    request: VisionPlanningRequest,
):
    document = service.get_document(document_id)
    harness = build_agent_harness_context(document, service.symbols)
    context = "\n\n".join(
        part
        for part in (
            request.context.strip(),
            "Automatic P&ID-Agent Harness Context:\n"
            + json.dumps(harness, ensure_ascii=False, separators=(",", ":")),
        )
        if part
    )
    return request.model_copy(update={"context": context})


def create_semantic_agent_router(
    service: DocumentService,
    planner: SemanticAgentPlanner,
    diagnostics: DiagnosticLogger | None = None,
    harness: AgentHarnessService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v2", tags=["P&ID-Agent semantic planning"])
    compiler = PermissiveSemanticTransactionCompiler(service)
    harness = harness or AgentHarnessService(
        service=service,
        store=service.store,
        registry=get_default_tool_registry(),
        diagnostics=diagnostics,
    )

    def start_session(document_id: str, request: VisionPlanningRequest):
        provider = request.provider
        return harness.create_session(
            AgentSessionCreateRequest(
                document_id=document_id,
                actor="web-user",
                provider=provider.base_url if provider and provider.base_url else "",
                model=provider.model if provider and provider.model else "",
                metadata={"surface": "rest", "workflow": "semantic-agent"},
            )
        )

    @router.get("/agent/semantic-tool-schema")
    def semantic_tool_schema():
        definition = get_default_tool_registry().require("plan_pid_agent_semantic_transaction")
        return definition.llm_tool_schema()

    @router.get("/documents/{document_id}/agent/harness-context")
    def agent_harness_context(document_id: str):
        try:
            return build_agent_harness_context(service.get_document(document_id), service.symbols)
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc

    @router.post(
        "/documents/{document_id}/transactions/analyze",
        response_model=AgentTransactionAssessment,
    )
    def analyze_low_level_transaction(document_id: str, request: TransactionRequest):
        try:
            return analyze_transaction(service, document_id, request)
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc

    @router.post(
        "/documents/{document_id}/agent/plan-v2",
        response_model=SemanticAgentPlanResult,
    )
    def plan_semantic_transaction(document_id: str, request: VisionAgentGenerateRequest):
        started = perf_counter()
        try:
            session = start_session(document_id, request)
        except Exception as exc:
            return _raise_harness_error(exc)
        if diagnostics is not None:
            diagnostics.emit(
                "llm.semantic_plan.started",
                document_id=document_id,
                expected_revision=request.expected_revision,
                prompt_chars=len(request.prompt),
                context_chars=len(request.context),
                **_provider_fields(request),
            )
        try:
            prepared_request = _with_harness_context(service, document_id, request)
            plan = planner.plan(document_id, prepared_request)
            compiled = compiler.compile(document_id, plan.transaction)
            compiled = _enforce_visible_output_requirement(
                service, document_id, request.require_visible_output, compiled
            )
            _record_proposal_evidence(
                harness,
                session_id=session.id,
                document_id=document_id,
                plan=plan,
                compiled=compiled,
                request=request,
            )
        except PlannerError as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.semantic_plan.failed",
                    document_id=document_id,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    error_code=exc.code,
                    provider_status=exc.provider_status,
                    error=exc,
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc

        if diagnostics is not None:
            diagnostics.emit(
                "llm.semantic_plan.completed",
                document_id=document_id,
                plan_id=plan.plan_id,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                valid=compiled.assessment.valid,
                stage=compiled.assessment.stage,
                semantic_operation_count=len(plan.transaction.operations),
                compiled_operation_count=compiled.assessment.compiled_operation_count,
                issue_codes=[item.code for item in compiled.assessment.issues],
                affected_element_ids=compiled.assessment.affected_element_ids,
                **_operation_types(plan, compiled),
            )
        return _result(session.id, plan, compiled, attempt=0)

    # -- TypeSafe: configure the key, prove it works, then draw with judgments ------------------ #

    @router.get("/provider/typesafe/status")
    def typesafe_provider_status(api_key: str | None = None, base_url: str | None = None,
                                 model: str | None = None):
        """Whether a call would be possible -- reported without making one and without the key."""

        return typesafe_status(api_key=api_key, base_url=base_url, model=model)

    @router.post("/provider/typesafe/verify")
    def verify_typesafe_provider(request: TypesafeVerifyRequest):
        """Ask TypeSafe one real judgment, because "a key is configured" is not "the key works"."""

        try:
            config = resolve_typesafe_config(
                api_key=request.api_key,
                base_url=request.base_url,
                model=request.model,
                timeout_seconds=request.timeout_seconds,
            )
            return {"ok": True, **config.describe(), **TypesafeClient(config).verify()}
        except TypesafeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc

    @router.post(
        "/documents/{document_id}/agent/typesafe-plan",
        response_model=SemanticAgentPlanResult,
    )
    def plan_with_typesafe(document_id: str, request: TypesafePlanRequest):
        """Draw by judgment: the same plan envelope, chosen by System One instead of generated."""

        started = perf_counter()
        try:
            typesafe_config = resolve_typesafe_config(
                api_key=request.api_key,
                base_url=request.base_url,
                model=request.model,
                timeout_seconds=request.timeout_seconds,
            )
            session = harness.create_session(
                AgentSessionCreateRequest(
                    document_id=document_id,
                    actor="web-user",
                    provider=typesafe_config.base_url,
                    model=typesafe_config.model,
                    metadata={"surface": "rest", "workflow": "typesafe-drawing"},
                )
            )
            planner = TypesafeSemanticPlanner(
                service, service.symbols, confidence_floor=request.confidence_floor
            )
            plan = planner.plan(
                document_id,
                _TypesafePlanInput(
                    prompt=request.prompt,
                    typesafe_config=typesafe_config,
                    expected_revision=request.expected_revision,
                ),
            )
            compiled = compiler.compile(document_id, plan.transaction)
            compiled = _enforce_visible_output_requirement(
                service, document_id, request.require_visible_output, compiled
            )
        except TypesafeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        except PlannerError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc
        if diagnostics is not None:
            diagnostics.emit(
                "llm.semantic_plan.completed",
                document_id=document_id,
                plan_id=plan.plan_id,
                backend="typesafe",
                duration_ms=round((perf_counter() - started) * 1000, 2),
                valid=compiled.assessment.valid,
                stage=compiled.assessment.stage,
                semantic_operation_count=len(plan.transaction.operations),
                compiled_operation_count=compiled.assessment.compiled_operation_count,
                issue_codes=[item.code for item in compiled.assessment.issues],
                **_operation_types(plan, compiled),
            )
        return _result(session.id, plan, compiled, attempt=0)

    # -- Natural-language surface: one sentence in, one governed revision out ------------------- #

    @router.post(
        "/documents/{document_id}/agent/text-plan",
        response_model=TextPlanResult,
    )
    def plan_text_drawing(document_id: str, request: TextPlanRequest):
        """Draw from one sentence: judged planning, the frozen chain, the one governed write.

        The target must be empty — the surface creates a drawing, it never overwrites one.
        A refused preflight leaves the document byte-identical, revision included.
        """

        started = perf_counter()
        try:
            typesafe_config = resolve_typesafe_config(
                api_key=request.api_key,
                base_url=request.base_url,
                model=request.model,
                timeout_seconds=request.timeout_seconds,
            )
            service.get_document(document_id)  # 404 outside the write, no session for a ghost
            session = harness.create_session(
                AgentSessionCreateRequest(
                    document_id=document_id,
                    actor="web-user",
                    provider=typesafe_config.base_url,
                    model=typesafe_config.model,
                    metadata={
                        "surface": "rest",
                        "workflow": "nl-text-plan",
                        "dry_run": request.dry_run,
                    },
                )
            )
            planner = TypesafeDiagramSpecPlanner(
                service.symbols, confidence_floor=request.confidence_floor
            )
            planned = planner.plan(request.sentence, typesafe_config=typesafe_config)
            if not request.dry_run and planned.completeness != "complete":
                # The frozen synthesis rule: a partial result can never be reported as success.
                # Preview may show what is missing; a commit must be the whole sentence, and a
                # refused commit is a no-op -- this raise happens before any write is attempted.
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": (
                            "typesafe_spec_partial"
                            if planned.completeness == "partial"
                            else "typesafe_spec_no_device"
                        ),
                        "completeness": planned.completeness,
                        "sentence": request.sentence.strip(),
                        "skipped": list(planned.skipped),
                        "unknown_tags": list(planned.unknown_tags),
                        "undelivered": list(planned.undelivered),
                        "catalog_gaps": [dict(gap) for gap in planned.catalog_gaps],
                    },
                )
            finalized = _finalize_spec_layout(planned.spec)
            layout = materialize_canonical_layout(finalized, document_id=document_id)
        except TypesafeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc
        except (SemanticTopologyIngressError, SymbolGeometryError) as exc:
            # The sentence planned fine; the frozen chain refused the drawing (today that
            # includes the connection-less drawing the router declines). A refusal about
            # the sentence's shape is a client-visible 422, not an uncaught 500.
            raise HTTPException(
                status_code=422,
                detail=f"这句话规划出的图纸引擎目前画不了：{exc}",
            ) from exc

        payload: dict[str, Any] = {
            "document_id": document_id,
            "committed": False,
            "revision": None,
            "spec": planned.spec.model_dump(mode="json"),
            "canonical_layout_digest": finalized.canonical_layout_digest,
            "materialization_digest": layout.materialization_digest,
            "notes": list(planned.notes),
            "skipped": list(planned.skipped),
            "unknown_tags": list(planned.unknown_tags),
            "completeness": planned.completeness,
            "undelivered": list(planned.undelivered),
            "catalog_gaps": [dict(gap) for gap in planned.catalog_gaps],
            "model": planned.model,
            "latency_ms": planned.latency_ms,
            "question_count": planned.question_count,
            "judgment_count": planned.judgment_count,
        }
        if request.dry_run:
            return TextPlanResult(**payload)

        # Read the revision at the last moment and let the writer refuse a raced target: the
        # drawing that was verified belongs to this revision, and a conflict is terminal.
        current = service.get_document(document_id)
        audit = AuditContext(
            actor="web-user",
            surface="rest",
            tool_name="draw_text_plan",
            session_id=session.id,
            provider=typesafe_config.base_url,
            model=typesafe_config.model,
            validation_status="valid",
            metadata={
                "workflow": "nl-text-plan",
                "sentence": request.sentence.strip(),
                "layout_contract_version": M7_LAYOUT_CONTRACT_VERSION,
            },
        )
        try:
            applied = apply_materialized_layout(
                service,
                layout,
                expected_revision=current.revision,
                audit=audit,
                label=f"NL 出图：{request.sentence.strip()[:40]}",
            )
        except MaterializationTargetNotEmptyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except MaterializationCanvasError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except MaterializationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if diagnostics is not None:
            diagnostics.emit(
                "nl.text_plan.completed",
                document_id=document_id,
                session_id=session.id,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                committed=True,
                revision=applied.document.revision,
                entity_count=len(planned.spec.entities),
                connection_count=len(planned.spec.connections),
                question_count=planned.question_count,
                judgment_count=planned.judgment_count,
                canonical_layout_digest=finalized.canonical_layout_digest,
                materialization_digest=layout.materialization_digest,
            )
        return TextPlanResult(
            **{**payload, "committed": True, "revision": applied.document.revision}
        )

    @router.post("/documents/{document_id}/agent/plan-v2-stream")
    async def plan_semantic_transaction_stream(
        document_id: str,
        request: VisionAgentGenerateRequest,
    ):
        try:
            session = start_session(document_id, request)
            prepared_request = _with_harness_context(service, document_id, request)
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc

        async def event_generator():
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

            def worker():
                try:
                    for event_type, chunk in planner.stream_plan_events(document_id, prepared_request):
                        loop.call_soon_threadsafe(queue.put_nowait, (event_type, chunk))
                    loop.call_soon_threadsafe(queue.put_nowait, ("EOF", None))
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))
                    loop.call_soon_threadsafe(queue.put_nowait, ("EOF", None))

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

            plan = None
            while True:
                event_type, chunk = await queue.get()
                if event_type == "EOF":
                    break
                if event_type == "error":
                    yield f"event: error\ndata: {json.dumps({'message': chunk}, ensure_ascii=False)}\n\n"
                    return
                if event_type in ("thinking", "content"):
                    yield f"event: {event_type}\ndata: {json.dumps({'delta': chunk}, ensure_ascii=False)}\n\n"
                elif event_type == "plan":
                    plan = chunk

            if plan is not None:
                compiled = compiler.compile(document_id, plan.transaction)
                compiled = _enforce_visible_output_requirement(
                    service, document_id, request.require_visible_output, compiled
                )
                _record_proposal_evidence(
                    harness,
                    session_id=session.id,
                    document_id=document_id,
                    plan=plan,
                    compiled=compiled,
                    request=request,
                )
                res = _result(session.id, plan, compiled, attempt=0)
                yield f"event: complete\ndata: {json.dumps(res.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            else:
                try:
                    plan = planner.plan(document_id, prepared_request)
                    compiled = compiler.compile(document_id, plan.transaction)
                    compiled = _enforce_visible_output_requirement(
                        service, document_id, request.require_visible_output, compiled
                    )
                    _record_proposal_evidence(
                        harness,
                        session_id=session.id,
                        document_id=document_id,
                        plan=plan,
                        compiled=compiled,
                        request=request,
                    )
                    res = _result(session.id, plan, compiled, attempt=0)
                    yield f"event: complete\ndata: {json.dumps(res.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                except Exception as exc:
                    yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/documents/{document_id}/agent/replan",
        response_model=SemanticAgentPlanResult,
    )
    def replan_semantic_transaction(
        document_id: str,
        request: VisionSemanticAgentReplanRequest,
    ):
        started = perf_counter()
        try:
            provider = request.provider
            session = harness.ensure_session(
                document_id,
                session_id=request.session_id,
                actor="web-user",
                provider=provider.base_url if provider and provider.base_url else "",
                model=provider.model if provider and provider.model else "",
            )
            failed = compiler.compile(document_id, request.failed_plan.transaction)
            failed = _enforce_visible_output_requirement(
                service, document_id, request.require_visible_output, failed
            )
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.semantic_replan.started",
                    document_id=document_id,
                    parent_plan_id=request.failed_plan.plan_id,
                    attempt=request.attempt,
                    expected_revision=request.expected_revision,
                    failure_stage=failed.assessment.stage,
                    failure_issue_codes=[item.code for item in failed.assessment.issues],
                    failed_semantic_operation_types=[
                        item.op for item in request.failed_plan.transaction.operations
                    ],
                    prompt_chars=len(request.prompt),
                    context_chars=len(request.context),
                    **_provider_fields(request),
                )
            prepared_request = _with_harness_context(service, document_id, request)
            plan = planner.replan(document_id, prepared_request, failed.assessment)
            compiled = compiler.compile(document_id, plan.transaction)
            compiled = _enforce_visible_output_requirement(
                service, document_id, request.require_visible_output, compiled
            )
            _record_proposal_evidence(
                harness,
                session_id=session.id,
                document_id=document_id,
                plan=plan,
                compiled=compiled,
                request=request,
            )
        except PlannerError as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.semantic_replan.failed",
                    document_id=document_id,
                    parent_plan_id=request.failed_plan.plan_id,
                    attempt=request.attempt,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    error_code=exc.code,
                    provider_status=exc.provider_status,
                    error=exc,
                )
            raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"document not found: {exc.args[0]}") from exc

        if diagnostics is not None:
            diagnostics.emit(
                "llm.semantic_replan.completed",
                document_id=document_id,
                plan_id=plan.plan_id,
                parent_plan_id=request.failed_plan.plan_id,
                attempt=request.attempt,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                valid=compiled.assessment.valid,
                stage=compiled.assessment.stage,
                semantic_operation_count=len(plan.transaction.operations),
                compiled_operation_count=compiled.assessment.compiled_operation_count,
                issue_codes=[item.code for item in compiled.assessment.issues],
                affected_element_ids=compiled.assessment.affected_element_ids,
                **_operation_types(plan, compiled),
            )
        return _result(
            session.id,
            plan,
            compiled,
            attempt=request.attempt,
            parent_plan_id=request.failed_plan.plan_id,
        )

    @router.post(
        "/documents/{document_id}/agent/apply-v2",
        response_model=TransactionResult,
    )
    def apply_semantic_plan(document_id: str, request: SemanticAgentApplyRequest):
        started = perf_counter()
        intent = {"transaction": request.transaction.model_dump(mode="json")}
        try:
            authorized = harness.authorize(
                session_id=request.session_id,
                tool_name="apply_compiled_agent_transaction",
                document_id=document_id,
                intent=intent,
                approval_id=request.approval_id,
                base_revision=request.transaction.expected_revision,
                metadata={
                    "surface": "rest",
                    "plan_id": request.plan_id,
                    "parent_plan_id": request.parent_plan_id,
                    "attempt": request.attempt,
                },
            )
        except Exception as exc:
            return _raise_harness_error(exc)

        if diagnostics is not None:
            diagnostics.emit(
                "llm.semantic_apply.started",
                session_id=request.session_id,
                approval_id=request.approval_id,
                document_id=document_id,
                plan_id=request.plan_id,
                parent_plan_id=request.parent_plan_id,
                attempt=request.attempt,
                expected_revision=request.transaction.expected_revision,
                transaction_label=request.transaction.label,
                compiled_operation_types=[item.op for item in request.transaction.operations],
            )
        try:
            result = harness.apply_authorized(
                authorized,
                document_id,
                request.transaction,
                metadata={
                    "surface": "rest",
                    "endpoint": "agent/apply-v2",
                    "plan_id": request.plan_id,
                    "parent_plan_id": request.parent_plan_id,
                    "attempt": request.attempt,
                },
                validation_evidence={
                    "tool": "apply_compiled_agent_transaction",
                    "operation_count": len(request.transaction.operations),
                    "plan_id": request.plan_id,
                },
            )
        except (DocumentNotFoundError, InvalidOperationError, RevisionConflictError) as exc:
            if diagnostics is not None:
                diagnostics.emit(
                    "llm.semantic_apply.rejected",
                    session_id=request.session_id,
                    approval_id=request.approval_id,
                    document_id=document_id,
                    plan_id=request.plan_id,
                    parent_plan_id=request.parent_plan_id,
                    attempt=request.attempt,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                    error=exc,
                )
            return _raise_service_error(exc)
        emit_revision_diagnostics(
            service,
            result.document,
            action="transaction",
            source="llm",
            diagnostics=diagnostics,
            label=request.transaction.label,
            operation_count=len(request.transaction.operations),
            extra={
                "session_id": request.session_id,
                "approval_id": request.approval_id,
            },
        )
        if diagnostics is not None:
            diagnostics.emit(
                "llm.semantic_apply.completed",
                session_id=request.session_id,
                approval_id=request.approval_id,
                document_id=document_id,
                plan_id=request.plan_id,
                parent_plan_id=request.parent_plan_id,
                attempt=request.attempt,
                revision=result.document.revision,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                transaction_label=request.transaction.label,
                applied_operation_count=result.applied_operations,
            )
        return result

    return router
