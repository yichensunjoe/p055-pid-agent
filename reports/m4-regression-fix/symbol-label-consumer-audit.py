"""Read-only audit: what each `symbol.label` consumer concludes on a product-written drawing.

The remote asked for a read-only audit of every M4 `symbol.label` consumer after the tag fix,
to see whether any of them (beyond the three approved) treats the raw field as canonical tag
identity. This builds the same production drawing the fix's regression test builds (semantic
compiler -> governed apply -> production polish, so `label` is empty and the tag lives in a
`symbol_label` annotation), then prints what each layer reports about the tags.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "backend")

from agentcad.agent_semantic_models import ConnectPortsOperation, SemanticTransaction  # noqa: E402
from agentcad.engineering_ir import build_engineering_graph  # noqa: E402
from agentcad.engineering_reports import build_engineering_report  # noqa: E402
from agentcad.models import (  # noqa: E402
    AddElementOperation,
    CreateDocumentRequest,
    Point,
    SymbolElement,
)
from agentcad.semantic_compiler_engine import SemanticTransactionCompiler  # noqa: E402
from agentcad.service import DocumentService  # noqa: E402
from agentcad.store import SQLiteDocumentStore  # noqa: E402
from agentcad.symbols import SymbolRegistry  # noqa: E402
from agentcad.tag_resolver import describe_symbol_tag, resolve_symbol_tag  # noqa: E402
from agentcad.validation_engine import run_validation  # noqa: E402
from agentcad.validation_profile import built_in_profile, resolve_profile  # noqa: E402


def build(service: DocumentService, registry: SymbolRegistry, tags: tuple[str, str]) -> str:
    definition = registry.get("ball_valve")
    document = service.create_document(CreateDocumentRequest(name="label audit"))
    operations = [
        AddElementOperation(
            element=SymbolElement(
                id=element_id,
                symbol_key="ball_valve",
                position=Point(x=x, y=300),
                width=definition.width,
                height=definition.height,
                label=tag,
                properties={"tag": tag},
            )
        )
        for element_id, x, tag in (("v1", 200, tags[0]), ("v2", 700, tags[1]))
    ]
    operations.append(
        ConnectPortsOperation(
            connector_id="p1",
            source_element_id="v1",
            source_port_id="out",
            target_element_id="v2",
            target_port_id="in",
            process_tag="L-1",
            medium="process",
            nominal_diameter="DN50",
        )
    )
    compiled = SemanticTransactionCompiler(service).compile(
        document.id,
        SemanticTransaction(operations=operations, expected_revision=0, label="label audit"),
    )
    assert compiled.assessment.valid and compiled.transaction is not None
    service.apply_transaction(document.id, compiled.transaction, source="system")
    return document.id


def main() -> None:
    registry = SymbolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        service = DocumentService(SQLiteDocumentStore(Path(tmp) / "audit.db"), registry)
        document_id = build(service, registry, ("HV-101", "HV-102"))
        document = service.get_document(document_id)

        print("=== the drawing as the product wrote it ===")
        for element in sorted(document.elements, key=lambda item: item.id):
            if element.type == "symbol":
                print(
                    f"  symbol {element.id}: label={element.label!r} "
                    f"properties.tag={element.properties.get('tag')!r}"
                )
            if element.type == "text":
                print(
                    f"  text   {element.id}: text={element.text!r} "
                    f"role={element.metadata.get('annotation_role')!r}"
                )

        print()
        print("=== consumer 1: the canonical resolver (agentcad/tag_resolver.py) ===")
        for element in sorted(document.elements, key=lambda item: item.id):
            if element.type == "symbol":
                print(
                    f"  {element.id}: {describe_symbol_tag(document, element)!r} "
                    f"-> {resolve_symbol_tag(document, element)!r}"
                )

        print()
        print("=== consumer 2: engineering-report rules (TAG_MISSING / TAG_DUPLICATE) ===")
        result = run_validation(
            document, registry, resolve_profile(built_in_profile()), service=service
        )
        tag_findings = [
            (issue.code, tuple(issue.element_ids), issue.message)
            for issue in result.issues
            if issue.code in {"TAG_MISSING", "TAG_DUPLICATE"}
        ]
        print(f"  {tag_findings or '(no tag findings - correct: two distinct tags)'}")

        print()
        print("=== consumer 3: equipment / instrument schedule rows ===")
        report = build_engineering_report(document, registry)
        for row in [*report.equipment, *report.instruments]:
            print(f"  schedule row {row.element_id}: tag={row.tag!r} name={row.name!r}")

        print()
        print("=== consumer 4: engineering IR records (feeds IR_DUPLICATE_IDENTITY) ===")
        graph = build_engineering_graph(document, registry)
        for record in graph.objects:
            if record.element_ids:
                print(
                    f"  IR {record.kind} {record.element_ids[0]}: tag={record.tag!r} "
                    f"label={record.label!r}"
                )


if __name__ == "__main__":
    main()
