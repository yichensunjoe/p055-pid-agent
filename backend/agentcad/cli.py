from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import ProviderConfig, TransactionRequest


def _default_database_path() -> Path:
    root = Path(__file__).resolve().parents[2]
    return Path(
        os.getenv(
            "PID_AGENT_DATABASE_PATH",
            os.getenv("AGENTCAD_DATABASE_PATH", str(root / "data" / "pid-agent.db")),
        )
    )


def _add_database_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path; defaults to PID_AGENT_DATABASE_PATH",
    )


def _json_payload(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _run_audit_command(args: argparse.Namespace) -> None:
    """Read-only audit / provenance commands.

    The chain is only ever appended by engineering write paths; these commands exist so
    an engineer or CI job can verify and export evidence without the HTTP API.
    """
    from .service import DocumentService, revision_snapshot  # noqa: F401
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry

    database = args.database or _default_database_path()
    service = DocumentService(SQLiteDocumentStore(Path(database)), SymbolRegistry())

    if args.audit_command == "verify":
        verification = service.audit.verify_chain()
        payload = _json_payload(verification.model_dump(mode="json"))
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        raise SystemExit(0 if verification.ok else 2)

    if args.audit_command == "trail":
        records = service.audit.audit_trail(
            document_id=args.document,
            event_type=args.event,
            actor=args.actor,
            status=args.status,
            limit=args.limit,
        )
        print(_json_payload([record.model_dump(mode="json") for record in records]))
        return

    if args.audit_command == "evidence":
        evidence = service.audit.revision_evidence(args.document_id, args.revision)
        print(_json_payload(evidence.model_dump(mode="json")))
        return

    package = service.audit.export_package(
        document_id=args.document,
        limit=args.limit,
    )
    args.output.write_text(
        json.dumps(package, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        _json_payload(
            {
                "output": str(args.output),
                "records": len(package["records"]),
                "chain_ok": package["verification"]["ok"],
            }
        )
    )


def _run_engineering_graph_command(args: argparse.Namespace) -> None:
    """Derive the engineering semantic graph of one drawing.

    Read-only: the database is only opened for reading, so this is safe to run against
    a live project database from a terminal or a CI job.
    """
    from .engineering_ir import build_engineering_graph
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry

    database = args.database or _default_database_path()
    stored = SQLiteDocumentStore(Path(database)).get(args.document_id)
    if stored is None:
        raise SystemExit(f"document not found: {args.document_id}")
    graph = build_engineering_graph(stored.document, SymbolRegistry())
    payload = graph.model_dump(mode="json", by_alias=True)
    if args.summary:
        payload = {
            "schema": payload["schema"],
            "version": payload["version"],
            "document_id": payload["document_id"],
            "document_name": payload["document_name"],
            "revision": payload["revision"],
            "content_hash": payload["content_hash"],
            "counts": payload["counts"],
            "findings": payload["findings"],
            "off_page_object_ids": payload["off_page_object_ids"],
            "connectivity_components": len(payload["connectivity_components"]),
        }
    text = _json_payload(payload)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if graph.counts.errors == 0 else 2)


def _run_engineering_find_command(args: argparse.Namespace) -> None:
    """Locate one engineering object across the project's indexed drawings.

    Read-only, like the other derived-data commands: it opens the database for
    reading and searches the cached engineering graphs, so it is safe on a live
    project. Rows left by an older builder version are skipped rather than guessed
    at; run ``project-index rebuild`` to search them again.
    """
    from .project_index import ProjectIndexService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry

    database = args.database or _default_database_path()
    store = SQLiteDocumentStore(Path(database))
    project_index = ProjectIndexService(store, SymbolRegistry())
    matches = project_index.find_objects(args.ref, limit=args.limit)
    payload = [match.model_dump(mode="json") for match in matches]
    text = _json_payload(payload)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if matches else 2)


def _run_project_index_command(args: argparse.Namespace) -> None:
    """Inspect or rebuild the derived project engineering index."""
    from .project_index import ProjectIndexService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry

    database = args.database or _default_database_path()
    store = SQLiteDocumentStore(Path(database))
    project_index = ProjectIndexService(store, SymbolRegistry())

    if args.index_command == "rebuild":
        report = project_index.rebuild_all(force=args.force, built_by="cli")
        payload = report.model_dump(mode="json", by_alias=True)
        stale_after = report.stale_after
    elif args.index_command == "list":
        entries = project_index.list_entries()
        payload = [entry.model_dump(mode="json") for entry in entries]
        stale_after = [
            entry.document_id
            for entry in entries
            if entry.staleness in {"stale", "missing_document", "builder_outdated"}
        ]
    else:
        project = project_index.project_graph()
        payload = project.model_dump(mode="json", by_alias=True)
        stale_after = project.stale_document_ids

    text = _json_payload(payload)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if not stale_after else 2)


def _run_drafting_command(args: argparse.Namespace) -> None:
    """Deterministic drafting from a terminal or a CI job (M3).

    Read-only by design: ``report`` measures a drawing and ``preview`` returns the
    reproducible transaction, but neither writes. ``--apply`` is deliberately absent —
    a drawing is only changed through the governed transaction channel, so a drafting
    run can never bypass the revision check, the permission gate or the audit record.

    Exit code 2 means the drafting gate failed, so this drops straight into CI as a
    drawing-quality gate.
    """
    from .drafting_engine import DraftingEngine
    from .drafting_models import DraftingPolicy, DraftingRequest
    from .layout_models import RegionBox
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry

    database = args.database or _default_database_path()
    service = DocumentService(SQLiteDocumentStore(Path(database)), SymbolRegistry())
    engine = DraftingEngine(service)
    policy = DraftingPolicy(
        target_score=args.target_score,
        waived_codes=list(args.waive or []),
    )
    region = None
    if args.region is not None:
        x1, y1, x2, y2 = args.region
        region = RegionBox(x1=x1, y1=y1, x2=x2, y2=y2, label=args.region_label or "")
    request = DraftingRequest(
        expected_revision=args.expected_revision,
        region=region,
        element_ids=list(args.element or []),
        locked_element_ids=list(args.lock or []),
        direction=args.direction,
        relayout=not args.no_relayout,
        reroute_connectors=not args.no_reroute,
        place_annotations=not args.no_annotations,
        bridge_crossings=not args.no_bridges,
        resolve_collisions=not args.no_collisions,
        policy=policy,
    )
    if args.drafting_command == "report":
        report = engine.report(args.document_id, request)
        payload = report.model_dump(mode="json", by_alias=True)
        passed = report.gate.passed
        if args.summary:
            payload = {
                "schema": payload["schema"],
                "version": payload["version"],
                "document_id": payload["document_id"],
                "revision": payload["revision"],
                "content_hash": payload["content_hash"],
                "scope_kind": payload["scope_kind"],
                "score": payload["score"],
                "gate": payload["gate"],
                "findings": payload["findings"],
                "crossings": payload["crossings"],
                "junctions": payload["junctions"],
                "locks": payload["locks"],
                "port_count": len(payload["ports"]),
            }
    else:
        preview = engine.preview(args.document_id, request)
        payload = preview.model_dump(mode="json", by_alias=True)
        passed = preview.gate.passed and not preview.metrics.regressions
        if args.summary:
            payload = {
                "document_id": payload["document_id"],
                "current_revision": payload["current_revision"],
                "settled": payload["settled"],
                "score_before": payload["metrics"]["before"]["score"],
                "score_after": payload["metrics"]["after"]["score"],
                "improvements": payload["metrics"]["improvements"],
                "regressions": payload["metrics"]["regressions"],
                "gate": payload["gate"],
                "operation_count": payload["reproducibility"]["operation_count"],
                "transaction_digest": payload["reproducibility"]["transaction_digest"],
                "moved_element_ids": payload["moved_element_ids"],
                "rerouted_connector_ids": payload["rerouted_connector_ids"],
                "bridged_connector_ids": payload["bridged_connector_ids"],
                "locked_element_ids": payload["locked_element_ids"],
                "warnings": payload["warnings"],
                "findings": payload["findings"],
            }
    text = _json_payload(payload)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if passed else 2)


def _run_database_command(args: argparse.Namespace) -> None:
    from .database_recovery import (
        DatabaseRecoveryError,
        create_backup,
        database_info,
        restore_backup,
    )

    database_path = args.database or _default_database_path()
    try:
        if args.database_command == "info":
            payload = {"ok": True, "database": asdict(database_info(database_path, migrate=True))}
        elif args.database_command == "backup":
            result = create_backup(database_path, args.output, overwrite=args.overwrite)
            payload = {"ok": True, "backup": asdict(result)}
        elif args.database_command == "restore":
            result = restore_backup(
                args.input,
                database_path,
                expected_instance_id=args.expect_instance_id,
                allow_instance_mismatch=args.allow_instance_mismatch,
            )
            payload = {"ok": True, "restore": asdict(result)}
        else:  # pragma: no cover - argparse guarantees a subcommand
            raise AssertionError(f"unsupported database command: {args.database_command}")
    except DatabaseRecoveryError as exc:
        print(
            _json_payload(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                    },
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    print(_json_payload(payload))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="pid-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the P&ID-Agent API and web app")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--reload", action="store_true")

    subparsers.add_parser("mcp", help="Run the MCP server over stdio")
    subparsers.add_parser("transaction-schema", help="Print the transaction JSON schema")

    database_parser = subparsers.add_parser(
        "db", help="Inspect, back up, or restore the SQLite document database"
    )
    database_subparsers = database_parser.add_subparsers(
        dest="database_command", required=True
    )
    info_parser = database_subparsers.add_parser(
        "info", help="Migrate if needed and print database identity and schema information"
    )
    _add_database_argument(info_parser)

    backup_parser = database_subparsers.add_parser(
        "backup", help="Create an integrity-checked online SQLite backup"
    )
    _add_database_argument(backup_parser)
    backup_parser.add_argument("--output", type=Path, required=True, help="Destination .pidbak file")
    backup_parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing regular backup file"
    )

    restore_parser = database_subparsers.add_parser(
        "restore", help="Verify and atomically restore a SQLite backup"
    )
    _add_database_argument(restore_parser)
    restore_parser.add_argument("--input", type=Path, required=True, help="Source .pidbak file")
    restore_parser.add_argument(
        "--expect-instance-id",
        default=None,
        help=(
            "Explicitly confirm the backup instance id; required for a missing/corrupt target "
            "or an intentional instance override"
        ),
    )
    restore_parser.add_argument(
        "--allow-instance-mismatch",
        action="store_true",
        help="Allow replacing a different database instance when paired with confirmation",
    )

    matrix_parser = subparsers.add_parser(
        "model-matrix",
        help="Run the semantic acceptance matrix against an OpenAI-compatible provider",
    )
    matrix_parser.add_argument("--base-url", required=True)
    matrix_parser.add_argument("--model", required=True)
    matrix_parser.add_argument(
        "--api-key",
        default="",
        help="API key value; prefer --api-key-env to avoid command history exposure",
    )
    matrix_parser.add_argument(
        "--api-key-env",
        default="PID_AGENT_MATRIX_API_KEY",
        help="Environment variable containing the API key",
    )
    matrix_parser.add_argument("--timeout", type=float, default=120)
    matrix_parser.add_argument("--repetitions", type=int, default=3)
    matrix_parser.add_argument("--max-replans", type=int, default=3)
    matrix_parser.add_argument(
        "--include-complex-diagram",
        action="store_true",
        help="Add the 30-50 element complex full-diagram generation scenario",
    )
    matrix_parser.add_argument("--output", default="", help="Optional JSON report path")

    quality_parser = subparsers.add_parser(
        "quality-harness",
        help="Run deterministic symbol, topology, and Agent-contract checks without a model",
    )
    quality_parser.add_argument(
        "--symbol-path",
        action="append",
        type=Path,
        default=[],
        help="Additional symbol JSON file or directory; may be repeated",
    )
    quality_parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path")

    audit_parser = subparsers.add_parser(
        "audit",
        help="Read the append-only audit / provenance chain (verify, trail, export)",
    )
    _add_database_argument(audit_parser)
    audit_subparsers = audit_parser.add_subparsers(dest="audit_command", required=True)
    audit_verify_parser = audit_subparsers.add_parser(
        "verify", help="Recompute the whole hash chain and report divergences"
    )
    audit_verify_parser.add_argument(
        "--output", type=Path, default=None, help="Optional JSON report path"
    )
    audit_trail_parser = audit_subparsers.add_parser(
        "trail", help="Print audit records, newest first, with optional filters"
    )
    audit_trail_parser.add_argument("--document", default=None, help="Filter by document id")
    audit_trail_parser.add_argument("--event", default=None, help="Filter by event type")
    audit_trail_parser.add_argument("--actor", default=None, help="Filter by actor")
    audit_trail_parser.add_argument("--status", default=None, help="Filter by status")
    audit_trail_parser.add_argument("--limit", type=int, default=200)
    audit_export_parser = audit_subparsers.add_parser(
        "export", help="Write a review evidence package (records + verification)"
    )
    audit_export_parser.add_argument("--document", default=None, help="Limit to one document")
    audit_export_parser.add_argument("--limit", type=int, default=1000)
    audit_export_parser.add_argument("--output", type=Path, required=True, help="Destination JSON file")
    audit_evidence_parser = audit_subparsers.add_parser(
        "evidence", help="Print the evidence bound to one document revision"
    )
    audit_evidence_parser.add_argument("document_id")
    audit_evidence_parser.add_argument("revision", type=int)

    graph_parser = subparsers.add_parser(
        "engineering-graph",
        help="Derive the engineering semantic graph of one drawing (objects/topology/findings)",
    )
    _add_database_argument(graph_parser)
    graph_parser.add_argument("document_id")
    graph_parser.add_argument(
        "--summary",
        action="store_true",
        help="Print counts and findings only, without the object list",
    )
    graph_parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path")

    find_parser = subparsers.add_parser(
        "engineering-find",
        help="Locate one engineering object across the indexed drawings (id, tag key or tag)",
    )
    _add_database_argument(find_parser)
    find_parser.add_argument(
        "ref", help="Engineering id (eq_…), tag key (equipment:p-101), tag (P-101) or element id"
    )
    find_parser.add_argument("--limit", type=int, default=50, help="Maximum matches to print")
    find_parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path")

    index_parser = subparsers.add_parser(
        "project-index",
        help="Inspect or rebuild the derived project engineering index",
    )
    _add_database_argument(index_parser)
    index_subparsers = index_parser.add_subparsers(dest="index_command", required=True)
    index_rebuild_parser = index_subparsers.add_parser(
        "rebuild", help="Rebuild every document's derived engineering graph"
    )
    index_rebuild_parser.add_argument(
        "--force", action="store_true", help="Rebuild even when the row is already fresh"
    )
    index_rebuild_parser.add_argument(
        "--output", type=Path, default=None, help="Optional JSON report path"
    )
    index_list_parser = index_subparsers.add_parser(
        "list", help="List index rows with revision-based freshness"
    )
    index_list_parser.add_argument(
        "--output", type=Path, default=None, help="Optional JSON report path"
    )
    index_project_parser = index_subparsers.add_parser(
        "project", help="Print the project-wide engineering graph"
    )
    index_project_parser.add_argument(
        "--output", type=Path, default=None, help="Optional JSON report path"
    )

    drafting_parser = subparsers.add_parser(
        "drafting",
        help=(
            "Deterministic drafting (M3): measure a drawing or preview a reproducible "
            "layout/route/annotation repair without writing"
        ),
    )
    _add_database_argument(drafting_parser)
    drafting_subparsers = drafting_parser.add_subparsers(dest="drafting_command", required=True)
    for name, help_text in (
        ("report", "Measure ports, crossings, junctions, collisions and the gate"),
        ("preview", "Preview a deterministic drafting transaction without writing"),
    ):
        sub = drafting_subparsers.add_parser(name, help=help_text)
        sub.add_argument("document_id")
        sub.add_argument(
            "--expected-revision",
            type=int,
            default=None,
            help="Refuse to run unless the document is at this revision",
        )
        sub.add_argument(
            "--region",
            type=float,
            nargs=4,
            metavar=("X1", "Y1", "X2", "Y2"),
            default=None,
            help="Region relayout scope",
        )
        sub.add_argument("--region-label", default=None, help="Label recorded for the region")
        sub.add_argument(
            "--element",
            action="append",
            default=[],
            help="Explicit scope element id (repeatable)",
        )
        sub.add_argument(
            "--lock",
            action="append",
            default=[],
            help="Freeze one element for this run (repeatable)",
        )
        sub.add_argument(
            "--direction",
            choices=["horizontal", "vertical"],
            default="horizontal",
        )
        sub.add_argument("--no-relayout", action="store_true", help="Route-only tidy-up")
        sub.add_argument("--no-reroute", action="store_true")
        sub.add_argument("--no-annotations", action="store_true")
        sub.add_argument("--no-bridges", action="store_true")
        sub.add_argument("--no-collisions", action="store_true")
        sub.add_argument("--target-score", type=float, default=95, help="Gate score target")
        sub.add_argument(
            "--waive",
            action="append",
            default=[],
            help=(
                "Waive one finding code (repeatable). Waived findings are still "
                "reported, marked as waived; only the gate stops blocking on them."
            ),
        )
        sub.add_argument(
            "--summary",
            action="store_true",
            help="Print the gate and diffs only, without the full object lists",
        )
        sub.add_argument("--output", type=Path, default=None, help="Optional JSON report path")

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("agentcad.main:app", host=args.host, port=args.port, reload=args.reload)
    elif args.command == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
    elif args.command == "db":
        _run_database_command(args)
    elif args.command == "model-matrix":
        from .model_acceptance import ModelMatrixRequest, run_model_matrix
        from .symbols import SymbolRegistry

        api_key = args.api_key or os.getenv(args.api_key_env, "")
        request = ModelMatrixRequest(
            provider=ProviderConfig(
                base_url=args.base_url,
                model=args.model,
                api_key=api_key or None,
                timeout_seconds=args.timeout,
            ),
            repetitions=args.repetitions,
            max_replans=args.max_replans,
            include_complex_diagram=args.include_complex_diagram,
        )
        report = run_model_matrix(request, SymbolRegistry())
        payload = _json_payload(report.model_dump(mode="json"))
        if args.output:
            Path(args.output).write_text(payload + "\n", encoding="utf-8")
        print(payload)
        raise SystemExit(0 if report.accepted else 2)
    elif args.command == "audit":
        _run_audit_command(args)
    elif args.command == "engineering-graph":
        _run_engineering_graph_command(args)
    elif args.command == "engineering-find":
        _run_engineering_find_command(args)
    elif args.command == "project-index":
        _run_project_index_command(args)
    elif args.command == "drafting":
        _run_drafting_command(args)
    elif args.command == "quality-harness":
        from .quality_harness import run_quality_harness, symbol_load_failure_report
        from .symbols import SymbolCatalogLoadError, SymbolRegistry

        try:
            report = run_quality_harness(SymbolRegistry(search_paths=args.symbol_path))
        except SymbolCatalogLoadError as exc:
            report = symbol_load_failure_report(exc)
        payload = _json_payload(report.model_dump(mode="json", by_alias=True))
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        raise SystemExit(0 if report.passed else 2)
    else:
        print(_json_payload(TransactionRequest.model_json_schema()))


if __name__ == "__main__":
    main()
