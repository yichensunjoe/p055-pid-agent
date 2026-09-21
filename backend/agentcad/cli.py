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


def _run_validation_command(args: argparse.Namespace) -> None:
    """Validate a stored document, or assess its release readiness. Both are reads.

    Exit code 0 means the gate passed for the question asked (no unwaived blockers for
    ``validate``; ``eligible`` for ``release-readiness``), 2 means it did not. Nothing
    here writes a revision, and this command cannot approve anything: release readiness
    is evidence, and the formal release stays behind the human Approval Gate.

    ``--as-of`` exists because waivers expire; without it the same document could be
    validated twice with two answers and no way to see why.
    """
    from datetime import datetime

    from .audit_models import AuditContext
    from .release_validator import assess_document_release_readiness
    from .service import DocumentNotFoundError, DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry
    from .validation_engine import (
        ValidationTimeError,
        normalize_evaluation_time,
        validate_document,
    )
    from .validation_models import canonical_payload
    from .validation_profile import ProfileError, load_profile

    database = args.database or _default_database_path()
    service = DocumentService(SQLiteDocumentStore(Path(database)), SymbolRegistry())
    try:
        profile = load_profile()
    except ProfileError as exc:
        print(_json_payload({"error": exc.code, "message": exc.message}))
        raise SystemExit(2) from exc

    moment = None
    if args.as_of:
        try:
            parsed = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        except ValueError as exc:
            print(_json_payload({"error": "invalid_as_of", "message": str(exc)}))
            raise SystemExit(2) from exc
        # The engine owns the time contract; the CLI only frames its stable error code
        # for a terminal (R3 P0-2).
        try:
            moment = normalize_evaluation_time(parsed)
        except ValidationTimeError as exc:
            print(
                _json_payload(
                    {
                        "error": exc.code,
                        "message": "--as-of must carry a timezone, e.g. 2026-09-19T12:00:00Z",
                    }
                )
            )
            raise SystemExit(2) from exc
    audit_context = AuditContext(
        actor="cli",
        surface="cli",
        tool_name=(
            "validate_document" if args.command == "validate" else "assess_release_readiness"
        ),
        label=f"{args.command} {args.document_id}",
    )
    try:
        if args.command == "validate":
            result = validate_document(service, args.document_id, profile, now=moment)
            payload = canonical_payload(result)
            passed = not result.has_blockers
            if args.audit:
                service.audit.record_event(
                    "validation.completed",
                    audit_context,
                    document_id=result.document_id,
                    base_revision=result.revision,
                    evidence={
                        "validation_hash": result.result_hash,
                        "profile_id": result.profile_id,
                        "rule_bundle_fingerprint": result.rule_bundle_fingerprint,
                        "evaluated_at": result.evaluated_at.isoformat(),
                        "counts": result.counts.model_dump(mode="json"),
                    },
                )
        else:
            readiness = assess_document_release_readiness(
                service, args.document_id, profile, now=moment
            )
            payload = canonical_payload(readiness)
            passed = readiness.state == "eligible"
            if args.audit:
                service.audit.record_event(
                    "release.readiness.assessed",
                    audit_context,
                    document_id=readiness.document_id,
                    base_revision=readiness.revision,
                    evidence={
                        "readiness_hash": readiness.readiness_hash,
                        "state": readiness.state,
                        "validation_hash": readiness.validation_hash,
                        "evaluated_at": readiness.evaluated_at.isoformat(),
                        "human_approval_required": True,
                    },
                )
    except DocumentNotFoundError as exc:
        print(_json_payload({"error": "document_not_found", "message": str(exc)}))
        raise SystemExit(2) from exc

    if args.summary:
        counts = payload["counts"]
        if args.command == "validate":
            payload = {
                "document_id": payload["document_id"],
                "revision": payload["revision"],
                "profile_id": payload["profile_id"],
                "profile_version": payload["profile_version"],
                "rule_bundle_fingerprint": payload["rule_bundle_fingerprint"],
                "evaluated_at": payload["evaluated_at"],
                "result_hash": payload["result_hash"],
                "counts": counts,
                "validators_skipped": payload["validators_skipped"],
                "issues": [
                    {
                        "code": issue["code"],
                        "severity": issue["severity"],
                        "rule_source": issue["rule_source"],
                        "waiver_status": issue["waiver_status"],
                        "element_ids": issue["element_ids"],
                    }
                    for issue in payload["issues"]
                ],
            }
        else:
            payload = {
                "document_id": payload["document_id"],
                "state": payload["state"],
                "reasons": payload["reasons"],
                "readiness_hash": payload["readiness_hash"],
                "validation_hash": payload["validation_hash"],
                "evaluated_at": payload["evaluated_at"],
                "counts": counts,
                "human_approval_required": payload["human_approval_required"],
            }
    text = _json_payload(payload)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if passed else 2)


def _run_repair_command(args: argparse.Namespace) -> None:
    """Repair one finding from the terminal, previewing before the governed write.

    Exit codes: 0 means the drawing now satisfies the success oracle (or, for ``--preview``, that
    a candidate did and no write was needed to say so); 2 means the run refused, failed, or asked
    for a human (``human_required``); 3 means the request could not be bound to a finding at all
    (the drawing has no unwaived finding matching the target, or the evaluation time is invalid).
    """

    from datetime import datetime

    from .repair_models import repair_payload
    from .repair_orchestrator import RepairStaleEvidence, repair_document_finding
    from .service import DocumentNotFoundError, DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry
    from .validation_engine import ValidationTimeError, normalize_evaluation_time
    from .validation_profile import ProfileError, load_profile

    database = args.database or _default_database_path()
    service = DocumentService(SQLiteDocumentStore(Path(database)), SymbolRegistry())
    try:
        profile = load_profile()
    except ProfileError as exc:
        print(_json_payload({"error": exc.code, "message": exc.message}))
        raise SystemExit(2) from exc

    moment = None
    if args.as_of:
        try:
            parsed = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
            moment = normalize_evaluation_time(parsed)
        except (ValueError, ValidationTimeError) as exc:
            print(
                _json_payload(
                    {
                        "error": "invalid_as_of",
                        "message": "--as-of must carry a timezone, e.g. 2026-09-20T12:00:00Z",
                    }
                )
            )
            raise SystemExit(3) from exc

    try:
        run = repair_document_finding(
            service,
            profile,
            args.document_id,
            target_code=args.code or None,
            validator_id=args.validator,
            element_ids=list(args.element) or None,
            hop=args.hop,
            declared_by="surface_request",
            permits_creation=args.permit_creation,
            max_created_ids=1 if args.permit_creation else 0,
            apply_enabled=not args.preview,
            now=moment,
        )
    except DocumentNotFoundError as exc:
        print(_json_payload({"error": "document_not_found", "document_id": args.document_id}))
        raise SystemExit(3) from exc
    except RepairStaleEvidence as exc:
        print(_json_payload({"error": exc.code, "message": str(exc.args[0])}))
        raise SystemExit(3) from exc

    payload = repair_payload(run)
    if args.preview:
        payload["preview"] = True
    text = _json_payload(payload)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if run.repaired else 2)


def _run_repair_benchmark_command(args: argparse.Namespace) -> None:
    """Run the M5 self-repair benchmark and publish the evidence (baseline §M5-3, §N).

    The gate this command implements is not "the agent repaired things" but "a reviewer can
    recompute that claim". So it always does the two things that make the number mean something:
    the case set is derived from ``--candidate-sha`` rather than listed here, and the published
    payload is handed to the independent verifier, which recomputes the counts, S@1..S@5 and
    the per-family rates from the case records.

    Exit code 0 means the evidence verified *and* every threshold in the frozen spec was met;
    2 means the candidate did not pass, or the evidence did not recompute.
    """

    import tempfile

    from .repair_benchmark import THRESHOLDS
    from .repair_benchmark_runner import BenchmarkContext, run_benchmark
    from .repair_evidence import RepairBenchmarkResult, verify_benchmark_result
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry
    from .validation_profile import built_in_profile, resolve_profile

    def _run(database: Path) -> RepairBenchmarkResult:
        registry = SymbolRegistry()
        service = DocumentService(SQLiteDocumentStore(database), registry)
        context = BenchmarkContext(
            service=service,
            profile=resolve_profile(built_in_profile()),
            registry=registry,
        )
        result = run_benchmark(
            context,
            suite=args.suite,
            candidate_sha=args.candidate_sha,
            safety=not args.no_safety,
        )
        return result

    if args.database is not None:
        result = _run(Path(args.database))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            result = _run(Path(tmp) / "repair-benchmark.db")

    payload = result.model_dump(mode="json", by_alias=True)
    if args.output:
        args.output.write_text(_json_payload(payload) + "\n", encoding="utf-8")

    report_ok = True
    findings: list[str] = []
    if not args.no_verify:
        report = verify_benchmark_result(result)
        report_ok = report.ok
        findings = [f"{finding.code}: {finding.detail}" for finding in report.findings]

    thresholds = result.thresholds or dict(THRESHOLDS)
    summary = {
        "schema": "pid-agent.repair-benchmark-summary",
        "suite": result.suite,
        "track": result.track,
        "candidate_sha": result.candidate_sha,
        "spec_fingerprint": result.spec_fingerprint,
        "generator_fingerprint": result.generator_fingerprint,
        "oracle_version": result.oracle_version,
        "counts": result.counts.model_dump(mode="json"),
        "s_at": result.s_at,
        "family_s_at_5": result.family_s_at_5,
        "failure_taxonomy": result.failure_taxonomy,
        "thresholds": thresholds,
        "gates": result.gates,
        "gate_failures": result.gate_failures,
        "safety": {"passed": result.safety_passed, "total": result.safety_total},
        "cost": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "estimated": result.token_usage_estimated,
        },
        "latency_ms": result.latency_ms,
        "planner": (result.planner.model_dump(mode="json") if result.planner is not None else None),
        "benchmark_result_hash": result.benchmark_result_hash,
        "evidence_verified": report_ok,
        "verification_findings": findings,
    }
    print(_json_payload(summary))
    passed = (
        all(result.gates.values()) and not result.gate_failures and (report_ok or args.no_verify)
    )
    raise SystemExit(0 if passed else 2)


def _run_repair_scale_command(args: argparse.Namespace) -> None:
    """Run the M5 scale track on a large drawing (baseline §G).

    The gate here is not a percentage: §G fixes five deterministic cases and requires all five
    to clear the full oracle, with zero governance violations. What the command is really for is
    the numbers — scope size, context bytes, attempts and validation time on a drawing with
    thousands of elements — because "the agent still localizes its work when the drawing is
    huge" is the claim that needs evidence.

    Exit code 0 means 5/5; 2 means at least one case failed, or a write happened outside the
    case's policy; 3 means the source could not be prepared at all (a fixture problem, which
    must not be reported as a repair failure).
    """

    import tempfile

    from .repair_benchmark_runner import BenchmarkContext
    from .repair_scale import ScaleSetupError, ScaleSource, run_scale_suite
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry
    from .validation_profile import built_in_profile, resolve_profile

    def _run(database: Path):
        registry = SymbolRegistry()
        service = DocumentService(SQLiteDocumentStore(database), registry)
        context = BenchmarkContext(
            service=service,
            profile=resolve_profile(built_in_profile()),
            registry=registry,
        )
        source = (
            ScaleSource(kind="dwg", path=str(args.source))
            if args.source is not None
            else ScaleSource(kind="synthetic", element_target=args.element_target)
        )
        if args.no_semantic_seed:
            source.semantic_seed = False
        return run_scale_suite(
            context,
            source=source,
            candidate_sha=args.candidate_sha,
            progress=lambda case_id, record: print(
                f"{case_id} {record.classification} attempts={record.attempts}",
                file=sys.stderr,
                flush=True,
            ),
        )

    try:
        if args.database is not None:
            report = _run(Path(args.database))
        else:
            with tempfile.TemporaryDirectory() as tmp:
                report = _run(Path(tmp) / "repair-scale.db")
    except ScaleSetupError as exc:
        error = {"error": "scale_source_unprepared", "message": str(exc)}
        print(_json_payload(error), file=sys.stderr)
        raise SystemExit(3) from exc

    payload = report.model_dump(mode="json", by_alias=True)
    if args.output:
        args.output.write_text(_json_payload(payload) + "\n", encoding="utf-8")
    print(_json_payload(payload))
    passed = report.passed()
    print(
        _json_payload(
            {
                "schema": "pid-agent.repair-scale-summary",
                "track": report.track,
                "candidate_sha": report.candidate_sha,
                "source": report.source,
                "element_count": report.element_count,
                "symbol_count": report.symbol_count,
                "connector_count": report.connector_count,
                "semantic_seed": report.semantic_seed,
                "passed_cases": report.passed_cases,
                "total_cases": report.total_cases,
                "governance_violations": report.governance_violations,
                "passed": passed,
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(0 if passed else 2)


def _run_repair_coverage_command(args: argparse.Namespace) -> None:
    """Run the coverage-extension track: the codes the frozen corpus cannot produce.

    The command answers two questions and exits 2 if either is unmet: did every supported code's
    producer really make the canonical validator raise it (and did the governed repair resolve
    it), and can the unreachable codes still not be staged by the write or the import surface.

    Exit codes: 0 means the discovered matrix is complete and every negative control went red; 2
    means a supported code lost its producer/repair, an "unreachable" code became reachable, or a
    negative control stopped reporting; 3 means the run could not be set up at all.
    """

    import tempfile

    from .repair_benchmark_runner import BenchmarkContext
    from .repair_coverage_extension import extension_payload
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry
    from .validation_profile import built_in_profile, resolve_profile

    def _run(database: Path) -> dict[str, Any]:
        registry = SymbolRegistry()
        service = DocumentService(SQLiteDocumentStore(database), registry)
        context = BenchmarkContext(
            service=service,
            profile=resolve_profile(built_in_profile()),
            registry=registry,
        )
        return extension_payload(context)

    if args.database is not None:
        payload = _run(Path(args.database))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            payload = _run(Path(tmp) / "repair-coverage.db")

    if args.output:
        args.output.write_text(_json_payload(payload) + "\n", encoding="utf-8")
    print(_json_payload(payload))
    uncovered = payload["uncovered"]
    reachable = [
        probe["code"] for probe in payload["representability"] if probe["reachable"]
    ]
    complete = not uncovered and not reachable and payload["negative_controls_all_red"]
    print(
        _json_payload(
            {
                "schema": "pid-agent.repair-coverage-summary",
                "corpus_id": payload["corpus"]["corpus_id"],
                "corpus_version": payload["corpus"]["corpus_version"],
                "corpus_fingerprint": payload["corpus_fingerprint"],
                "case_count": payload["corpus"]["case_count"],
                "covered": payload["covered"],
                "uncovered": uncovered,
                "unreachable": payload["unreachable"],
                "became_reachable": reachable,
                "negative_controls_all_red": payload["negative_controls_all_red"],
                "complete": complete,
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(0 if complete else 2)


def _run_repair_qualification_command(args: argparse.Namespace) -> None:
    """Run the real-model qualification, or report that the project has no credential (§A4).

    Exit codes are part of the evidence story: 0 means a real model qualified, 2 means the
    qualification ran and the candidate did not clear the thresholds (or the evidence did not
    recompute), and 3 means it could not run because there is no usable provider. A missing
    credential must not look like a pass *or* like a failed candidate.
    """

    import tempfile

    from .llm import ProviderNotConfiguredError
    from .repair_benchmark_runner import BenchmarkContext
    from .repair_qualification import (
        awaiting_report,
        configured_provider,
        run_qualification,
    )
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry
    from .validation_profile import built_in_profile, resolve_profile

    provider = configured_provider()
    if provider is None:
        report = awaiting_report(
            candidate_sha=args.candidate_sha,
            reason=(
                "no model provider is configured: set PID_AGENT_LLM_BASE_URL and "
                "PID_AGENT_LLM_MODEL (CI has no external provider by design)"
            ),
        )
        payload = _json_payload(report.model_dump(mode="json", by_alias=True))
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        raise SystemExit(3)

    if args.dry_run:
        report = awaiting_report(
            candidate_sha=args.candidate_sha,
            reason="a provider is configured; --dry-run does not call it",
        )
        payload = _json_payload(report.model_dump(mode="json", by_alias=True))
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        raise SystemExit(0)

    from .repair_model_planner import ModelRepairPlanner

    try:
        planner = ModelRepairPlanner(provider=provider)
    except ProviderNotConfiguredError as exc:
        report = awaiting_report(candidate_sha=args.candidate_sha, reason=str(exc))
        payload = _json_payload(report.model_dump(mode="json", by_alias=True))
        if args.output:
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        raise SystemExit(3) from exc

    def _qualify(database: Path):
        registry = SymbolRegistry()
        service = DocumentService(SQLiteDocumentStore(database), registry)
        context = BenchmarkContext(
            service=service,
            profile=resolve_profile(built_in_profile()),
            registry=registry,
        )
        return run_qualification(
            context,
            planner=planner,
            candidate_sha=args.candidate_sha,
        )

    if args.database is not None:
        report, result = _qualify(Path(args.database))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            report, result = _qualify(Path(tmp) / "repair-qualification.db")

    payload = _json_payload(report.model_dump(mode="json", by_alias=True))
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    if args.database is not None:
        evidence_path = Path(args.database).with_suffix(".evidence.json")
        evidence_path.write_text(
            _json_payload(result.model_dump(mode="json", by_alias=True)) + "\n",
            encoding="utf-8",
        )
    print(payload)
    raise SystemExit(0 if report.status == "qualified" else 2)


def _run_import_cad_command(args: argparse.Namespace) -> None:
    """Import a DWG/DXF drawing as a governed document (Charter §14).

    This is a real write, and it goes through the same channel as every other write:
    one document creation carrying the entire operation set as a single governed
    mutation, with one revision check, one audit record and one undo step. ``--dry-run``
    decodes and translates the file and prints the report without creating anything,
    which is what a CI job or a reviewer should use first. With no file (or
    ``--capabilities``) it prints which converters this machine has, so "cannot import my
    DWG" is answerable before a 1 MB upload.

    Exit code 2 means the import was refused (no converter, unreadable file, no
    geometry, limit exceeded), so this can gate a pipeline.
    """
    from .audit_models import AuditContext
    from .cad_import import CadImporter, CadImportError
    from .cad_models import CadImportOptions
    from .service import DocumentService
    from .store import SQLiteDocumentStore
    from .symbols import SymbolRegistry

    database = args.database or _default_database_path()
    max_source_bytes = int(
        os.getenv(
            "PID_AGENT_MAX_CAD_SOURCE_BYTES",
            str(25 * 1024 * 1024),
        )
    )
    service = DocumentService(SQLiteDocumentStore(Path(database)), SymbolRegistry())
    importer = CadImporter(service, max_source_bytes=max_source_bytes)

    if args.capabilities or args.file is None:
        print(_json_payload(importer.capabilities().model_dump(mode="json")))
        return

    source_path = Path(args.file)
    options = CadImportOptions(
        name=args.name or "",
        frame=tuple(args.frame) if args.frame else None,
        fills="skip" if args.no_fills else "solid",
        layers=list(args.layer or []),
        include_text=not args.no_text,
        curve_segments=args.curve_segments,
        stroke_width=args.stroke_width,
        unit_scale=args.unit_scale,
        max_elements=args.max_elements,
    )
    try:
        if args.dry_run:
            # ``dry_run_path`` shares the loader with a real import, so a missing or
            # unreadable file is a stable CadImportError on both paths instead of a raw
            # FileNotFoundError here and a coded error there.
            plan = importer.dry_run_path(source_path, options=options)
            payload = plan.model_dump(mode="json")
            if args.summary:
                report = payload["report"]
                payload = {
                    "document_name": payload["document_name"],
                    "elements": payload["elements"],
                    "operations": payload["operations"],
                    "source": report["source"],
                    "counts": report["counts"],
                    "frame": report["frame"],
                    "canvas": report["canvas"],
                    "layers": report["layers"],
                    "issues": report["issues"],
                    "warnings": report["warnings"],
                    "logical_mutations": 0,
                    "duration_ms": report["duration_ms"],
                }
        else:
            result = importer.import_path(
                source_path,
                options=options,
                audit=AuditContext(
                    actor="cli",
                    surface="cli",
                    tool_name="import_cad_drawing",
                    label=f"Import CAD file: {source_path.name}",
                    metadata={"path": str(source_path)},
                ),
                source="system",
            )
            payload = result.model_dump(mode="json")
            if args.summary:
                report = payload["report"]
                payload = {
                    "document_id": payload["document_id"],
                    "document_name": payload["document_name"],
                    "revision": payload["revision"],
                    "source": report["source"],
                    "counts": report["counts"],
                    "frame": report["frame"],
                    "canvas": report["canvas"],
                    "layers": report["layers"],
                    "issues": report["issues"],
                    "warnings": report["warnings"],
                    "logical_mutations": report["logical_mutations"],
                    "operations": report["operations"],
                    "duration_ms": report["duration_ms"],
                }
    except CadImportError as exc:
        error = {
            "error": exc.code,
            "message": exc.message,
            "retryable": False,
            **({"detail": exc.detail} if exc.detail else {}),
        }
        text = _json_payload(error)
        if args.output:
            args.output.write_text(text + "\n", encoding="utf-8")
        print(text, file=sys.stderr)
        raise SystemExit(2) from exc
    text = _json_payload(payload)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


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
    database_subparsers = database_parser.add_subparsers(dest="database_command", required=True)
    info_parser = database_subparsers.add_parser(
        "info", help="Migrate if needed and print database identity and schema information"
    )
    _add_database_argument(info_parser)

    backup_parser = database_subparsers.add_parser(
        "backup", help="Create an integrity-checked online SQLite backup"
    )
    _add_database_argument(backup_parser)
    backup_parser.add_argument(
        "--output", type=Path, required=True, help="Destination .pidbak file"
    )
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
    quality_parser.add_argument(
        "--output", type=Path, default=None, help="Optional JSON report path"
    )

    repair_parser = subparsers.add_parser(
        "repair",
        help=(
            "Repair one canonical finding with the agent self-repair orchestrator "
            "(preview first; one governed write when applied)"
        ),
    )
    _add_database_argument(repair_parser)
    repair_parser.add_argument("document_id", help="Document id to repair")
    repair_parser.add_argument("--code", default="", help="Finding code to repair")
    repair_parser.add_argument(
        "--validator", default="", help="Restrict the target to one validator id"
    )
    repair_parser.add_argument(
        "--element",
        action="append",
        default=[],
        help="Restrict the target to findings touching this element (repeatable)",
    )
    repair_parser.add_argument("--hop", type=int, default=1, choices=(1, 2), help="Scope reach")
    repair_parser.add_argument(
        "--permit-creation",
        action="store_true",
        help="Allow the repair to put deleted equipment back (one new element at most)",
    )
    repair_parser.add_argument(
        "--preview",
        action="store_true",
        help="Plan and judge the repair without writing it (the default is to apply)",
    )
    repair_parser.add_argument(
        "--as-of", default="", help="Explicit evaluation time, e.g. 2026-09-20T12:00:00Z"
    )
    repair_parser.add_argument("--output", type=Path, default=None, help="Optional JSON path")

    repair_benchmark_parser = subparsers.add_parser(
        "repair-benchmark",
        help=(
            "Run the M5 agent self-repair benchmark (deterministic Track D) and publish "
            "recomputable evidence"
        ),
    )
    repair_benchmark_parser.add_argument(
        "--suite",
        choices=("dev", "acceptance"),
        default="dev",
        help="dev is the 24-case development suite; acceptance is the 72-case hard gate",
    )
    repair_benchmark_parser.add_argument(
        "--candidate-sha",
        default="",
        help=(
            "Commit the evidence is about. Acceptance seeds derive from it, so a run without "
            "one describes no candidate"
        ),
    )
    repair_benchmark_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path; defaults to a temporary database per run",
    )
    repair_benchmark_parser.add_argument(
        "--no-safety",
        action="store_true",
        help="Skip the safety-negative suite (development only; the gate always runs it)",
    )
    repair_benchmark_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip recomputing the evidence (development only; the gate always verifies)",
    )
    repair_benchmark_parser.add_argument(
        "--output", type=Path, default=None, help="Write the full evidence payload here"
    )

    repair_scale_parser = subparsers.add_parser(
        "repair-scale",
        help=(
            "Run the M5 scale track (baseline §G): the same repairs on a large drawing, with "
            "scope size, context bytes and timings reported per case"
        ),
    )
    repair_scale_parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Real CAD file to import as the big drawing. Omit to build the synthetic "
            "large drawing instead (CI has no private drawing)"
        ),
    )
    repair_scale_parser.add_argument(
        "--element-target",
        type=int,
        default=400,
        help="Approximate element count for the synthetic drawing",
    )
    repair_scale_parser.add_argument(
        "--no-semantic-seed",
        action="store_true",
        help=(
            "Refuse to add the governed valve train that a raw-geometry import needs before "
            "it has anything repairable. Report the failure instead of papering over it"
        ),
    )
    repair_scale_parser.add_argument(
        "--candidate-sha", default="", help="Commit the evidence is about"
    )
    repair_scale_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path; defaults to a temporary database per run",
    )
    repair_scale_parser.add_argument(
        "--output", type=Path, default=None, help="Write the scale report here"
    )

    repair_coverage_parser = subparsers.add_parser(
        "repair-coverage",
        help=(
            "Run the M5 coverage-extension track: deterministic producers for the codes the "
            "frozen 72-case corpus cannot produce, plus the proof that the rest cannot be staged "
            "by any supported surface"
        ),
    )
    repair_coverage_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path; defaults to a temporary database per run",
    )
    repair_coverage_parser.add_argument(
        "--output", type=Path, default=None, help="Write the coverage report here"
    )

    qualification_parser = subparsers.add_parser(
        "repair-qualification",
        help=(
            "Run the M5 real-model qualification (Track M): 24 cases through the same oracle "
            "and orchestrator as the deterministic gate"
        ),
    )
    qualification_parser.add_argument(
        "--candidate-sha", default="", help="Commit the qualification is about"
    )
    qualification_parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite database path; defaults to a temporary database per run",
    )
    qualification_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report whether a provider is configured, without calling it",
    )
    qualification_parser.add_argument(
        "--output", type=Path, default=None, help="Write the qualification report here"
    )

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
    audit_export_parser.add_argument(
        "--output", type=Path, required=True, help="Destination JSON file"
    )
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

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a stored document and print the canonical result (read-only)",
    )
    _add_database_argument(validate_parser)
    validate_parser.add_argument("document_id", help="Document id to validate")
    validate_parser.add_argument(
        "--as-of",
        default="",
        help="Explicit evaluation time for waiver expiry, e.g. 2026-09-19T12:00:00Z",
    )
    validate_parser.add_argument(
        "--audit",
        action="store_true",
        help="Record a read/tool evidence event referencing the result hash",
    )
    validate_parser.add_argument("--summary", action="store_true", help="Counts and issues only")
    validate_parser.add_argument("--output", type=Path, default=None, help="Optional JSON path")

    readiness_parser = subparsers.add_parser(
        "release-readiness",
        help=("Assess release readiness (eligible/not_eligible evidence; never an approval)"),
    )
    _add_database_argument(readiness_parser)
    readiness_parser.add_argument("document_id", help="Document id to assess")
    readiness_parser.add_argument("--as-of", default="", help="Explicit evaluation time")
    readiness_parser.add_argument(
        "--audit",
        action="store_true",
        help="Record a read/tool evidence event referencing the readiness hash",
    )
    readiness_parser.add_argument("--summary", action="store_true", help="State and reasons only")
    readiness_parser.add_argument("--output", type=Path, default=None, help="Optional JSON path")

    import_cad_parser = subparsers.add_parser(
        "import-cad",
        help=(
            "Import a DWG/DXF drawing as a new document (geometry, layers, text and "
            "block provenance; no engineering semantics)"
        ),
    )
    _add_database_argument(import_cad_parser)
    import_cad_parser.add_argument(
        "file",
        type=Path,
        nargs="?",
        default=None,
        help="DWG or DXF file to import; omit to list available decoders",
    )
    import_cad_parser.add_argument("--name", default="", help="Document name")
    import_cad_parser.add_argument(
        "--frame",
        type=float,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        default=None,
        help="Crop window in source coordinates; default is the drawing's own extents",
    )
    import_cad_parser.add_argument(
        "--layer",
        action="append",
        default=[],
        help="Import only this source layer (repeatable)",
    )
    import_cad_parser.add_argument("--no-text", action="store_true", help="Skip text")
    import_cad_parser.add_argument(
        "--no-fills",
        action="store_true",
        help="Skip solid fills (SOLID/TRACE/solid HATCH)",
    )
    import_cad_parser.add_argument(
        "--curve-segments",
        type=int,
        default=24,
        help="Samples per turn when an arc/ellipse becomes a polyline",
    )
    import_cad_parser.add_argument("--stroke-width", type=float, default=1.8)
    import_cad_parser.add_argument(
        "--unit-scale",
        type=float,
        default=1.0,
        help="Divide every coordinate and size by this factor",
    )
    import_cad_parser.add_argument("--max-elements", type=int, default=200_000)

    import_cad_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Decode and report without creating a document",
    )
    import_cad_parser.add_argument(
        "--capabilities",
        action="store_true",
        help="List the DWG/DXF decoders available on this machine",
    )
    import_cad_parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the report without the full element lists",
    )
    import_cad_parser.add_argument("--output", type=Path, default=None, help="Optional JSON path")

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
    elif args.command in {"validate", "release-readiness"}:
        _run_validation_command(args)
    elif args.command == "import-cad":
        _run_import_cad_command(args)
    elif args.command == "repair":
        _run_repair_command(args)
    elif args.command == "repair-benchmark":
        _run_repair_benchmark_command(args)
    elif args.command == "repair-scale":
        _run_repair_scale_command(args)
    elif args.command == "repair-coverage":
        _run_repair_coverage_command(args)
    elif args.command == "repair-qualification":
        _run_repair_qualification_command(args)
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
