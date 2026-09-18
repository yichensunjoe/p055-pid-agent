from __future__ import annotations

from math import ceil, isfinite
from time import perf_counter
from urllib.parse import parse_qsl
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import create_v1_compat_router, create_v2_router
from .api_acceptance import create_acceptance_router
from .api_audit import create_audit_router
from .api_cad_import import create_cad_import_router
from .api_documents import create_documents_router
from .api_drafting import create_drafting_router
from .api_dxf import create_dxf_router
from .api_engineering import create_engineering_router
from .api_export import _max_export_pixels, create_export_router
from .api_harness import create_harness_router
from .api_layout import create_layout_router
from .api_reports import create_reports_router
from .api_semantic_agent import create_semantic_agent_router
from .config import Settings
from .diagnostics import DiagnosticLogger
from .harness import AgentHarnessService
from .llm import OpenAICompatiblePlanner
from .project_index import ProjectIndexService
from .provider_security import ProviderNetworkPolicy
from .request_context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from .security import RequestBoundary, redact_query_string
from .service import DocumentService
from .store import SQLiteDocumentStore
from .symbols import SymbolRegistry
from .tool_registry import get_default_tool_registry
from .vision_semantic_planner import VisionSemanticAgentPlanner

VERSION = __version__


class RequestDiagnosticsASGIMiddleware:
    def __init__(self, app, diagnostics: DiagnosticLogger, service: DocumentService):
        self.app = app
        self.diagnostics = diagnostics
        self.service = service

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        should_log = path.startswith("/api/") or path == "/health"
        if not should_log:
            return await self.app(scope, receive, send)

        request_id = uuid4().hex
        started = perf_counter()
        query_string = scope.get("query_string", b"").decode("utf-8", errors="replace")
        method = scope.get("method", "GET")
        client = scope.get("client")
        client_host = client[0] if client else None

        self.diagnostics.emit(
            "http.request.started",
            request_id=request_id,
            method=method,
            path=path,
            query=redact_query_string(query_string),
            client=client_host,
        )
        context_token = bind_request_context(
            RequestContext(
                request_id=request_id,
                method=method,
                path=path,
                client=client_host or "",
            )
        )

        legacy_prefix = "/api/v2/documents/"
        legacy_suffix = "/export.png"
        if method == "GET" and path.startswith(legacy_prefix) and path.endswith(legacy_suffix):
            document_id = path[len(legacy_prefix) : -len(legacy_suffix)].strip("/")
            try:
                params = dict(parse_qsl(query_string, keep_blank_values=True))
                scale = float(params.get("scale", "1"))
                if isfinite(scale) and 0.1 <= scale <= 8:
                    document = self.service.get_document(document_id)
                    output_width = max(1, ceil(document.canvas.width * scale))
                    output_height = max(1, ceil(document.canvas.height * scale))
                    requested_pixels = output_width * output_height
                    max_pixels = _max_export_pixels()
                    if requested_pixels > max_pixels:
                        self.diagnostics.emit(
                            "export.rejected",
                            request_id=request_id,
                            document_id=document.id,
                            revision=document.revision,
                            format="png",
                            export_range="canvas",
                            legacy_route=True,
                            error_code="export_too_large",
                            requested_pixels=requested_pixels,
                            max_pixels=max_pixels,
                            output_width=output_width,
                            output_height=output_height,
                        )
                        response = JSONResponse(
                            status_code=413,
                            content={
                                "detail": {
                                    "error": "export_too_large",
                                    "message": "PNG export exceeds the configured pixel limit",
                                    "retryable": True,
                                    "requested_pixels": requested_pixels,
                                    "max_pixels": max_pixels,
                                    "output": {
                                        "width": output_width,
                                        "height": output_height,
                                    },
                                    "suggestions": [
                                        "降低 scale",
                                        "使用 export-v2 的 content 或 viewport 范围",
                                        "使用 SVG 导出超大图纸",
                                    ],
                                }
                            },
                        )
                        return await response(scope, receive, send)
            except Exception:
                pass

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-pid-agent-request-id", request_id.encode("ascii")))
                message["headers"] = headers
                self.diagnostics.emit(
                    "http.request.completed",
                    request_id=request_id,
                    method=method,
                    path=path,
                    status_code=message.get("status", 200),
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            self.diagnostics.emit(
                "http.request.failed",
                request_id=request_id,
                method=method,
                path=path,
                duration_ms=round((perf_counter() - started) * 1000, 2),
                error=exc,
            )
            raise
        finally:
            reset_request_context(context_token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.validate()
    symbols = SymbolRegistry()
    store = SQLiteDocumentStore(settings.database_path)
    service = DocumentService(store=store, symbols=symbols)
    provider_policy = ProviderNetworkPolicy(
        mode=settings.deployment_mode,
        allow_hosts=settings.provider_allow_hosts,
        allow_cidrs=settings.provider_allow_cidrs,
    )
    planner = OpenAICompatiblePlanner(
        service=service,
        symbols=symbols,
        provider_policy=provider_policy,
        max_response_bytes=settings.provider_max_response_bytes,
        max_timeout_seconds=settings.agent_timeout_seconds,
    )
    diagnostics_path = settings.diagnostics_path or settings.database_path.with_suffix(
        ".diagnostics.jsonl"
    )
    diagnostics = DiagnosticLogger(diagnostics_path, service_version=VERSION)
    semantic_planner = VisionSemanticAgentPlanner(
        service=service,
        symbols=symbols,
        diagnostics=diagnostics,
        provider_policy=provider_policy,
        max_response_bytes=settings.provider_max_response_bytes,
        max_timeout_seconds=settings.agent_timeout_seconds,
    )
    harness = AgentHarnessService(
        service=service,
        store=store,
        registry=get_default_tool_registry(),
        diagnostics=diagnostics,
    )
    project_index = ProjectIndexService(store=store, registry=symbols)

    shared = settings.deployment_mode == "shared"
    app = FastAPI(
        title="P&ID-Agent",
        version=VERSION,
        description="Lightweight, editable and agent-ready P&ID workspace",
        docs_url=None if shared else "/docs",
        redoc_url=None if shared else "/redoc",
        openapi_url=None if shared else "/openapi.json",
    )
    app.state.service = service
    app.state.diagnostics = diagnostics
    app.state.settings = settings
    app.state.provider_policy = provider_policy
    app.state.harness = harness
    app.state.project_index = project_index

    def json_safe(value):
        if isinstance(value, float) and not isfinite(value):
            return None
        if isinstance(value, dict):
            return {str(key): json_safe(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(child) for child in value]
        if isinstance(value, BaseException):
            return str(value)
        return value

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"detail": json_safe(exc.errors())},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "If-Match"],
        expose_headers=[
            "X-PID-Agent-Request-ID",
            "Content-Disposition",
            "X-PID-Agent-PDF-Page-Count",
            "X-PID-Agent-PDF-Page-Number",
            "X-PID-Agent-PDF-Paper-Size",
            "X-PID-Agent-PDF-Orientation",
            "X-PID-Agent-PDF-Layout",
            "X-PID-Agent-DXF-Version",
            "X-PID-Agent-DXF-Entity-Count",
            "X-PID-Agent-DXF-Layer-Count",
            "X-PID-Agent-DXF-Units",
            "X-PID-Agent-DXF-Scale",
            "X-PID-Agent-Report-Revision",
            "X-PID-Agent-Report-Scope",
            "X-PID-Agent-Report-Row-Count",
        ],
    )

    app.add_middleware(
        RequestDiagnosticsASGIMiddleware,
        diagnostics=diagnostics,
        service=service,
    )

    app.add_middleware(
        RequestBoundary,
        settings=settings,
    )

    app.include_router(create_v2_router(service, planner, diagnostics, VERSION, harness))
    app.include_router(create_documents_router(service))
    app.include_router(create_harness_router(harness))
    app.include_router(
        create_acceptance_router(
            symbols,
            diagnostics,
            provider_policy=provider_policy,
            audit=service.audit,
        )
    )
    app.include_router(create_audit_router(service))
    app.include_router(create_engineering_router(service, project_index))
    app.include_router(create_export_router(service, diagnostics))
    app.include_router(create_dxf_router(service, diagnostics))
    app.include_router(create_layout_router(service, diagnostics))
    app.include_router(create_drafting_router(service, diagnostics))
    app.include_router(
        create_cad_import_router(
            service,
            diagnostics,
            max_source_bytes=settings.max_import_body_bytes,
        )
    )
    app.include_router(create_reports_router(service))
    app.include_router(create_semantic_agent_router(service, semantic_planner, diagnostics, harness))
    app.include_router(create_v1_compat_router(service))

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "P&ID-Agent", "version": VERSION}

    diagnostics.emit(
        "server.runtime.created",
        database_path=settings.database_path,
        database_instance_id=store.database_instance_id,
        diagnostics_path=diagnostics_path,
        symbol_count=len(symbols.list()),
        deployment_mode=settings.deployment_mode,
        api_auth_enabled=bool(settings.api_token),
    )

    if settings.frontend_dist.exists():
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")

    return app


app = create_app()
