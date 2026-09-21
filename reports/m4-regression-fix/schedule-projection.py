"""Pre/post projection of the schedule rows on a drawing the product itself wrote.

Run it against a pristine archive of the tree before the schedule fix and against the tree
after it; the only difference should be the ``tag`` field of the rows.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "backend")

from agentcad.agent_semantic_models import ConnectPortsOperation, SemanticTransaction  # noqa: E402
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


def build(service: DocumentService, registry: SymbolRegistry) -> str:
    created = service.create_document(CreateDocumentRequest(name="schedule projection"))
    operations = []
    for element_id, symbol_key, tag, x in (
        ("v1", "ball_valve", "HV-101", 200.0),
        ("i1", "pressure_indicator", "PT-101", 700.0),
    ):
        definition = registry.get(symbol_key)
        operations.append(
            AddElementOperation(
                element=SymbolElement(
                    id=element_id,
                    symbol_key=symbol_key,
                    position=Point(x=x, y=300),
                    width=definition.width,
                    height=definition.height,
                    label=tag,
                    properties={"tag": tag},
                )
            )
        )
    operations.append(
        ConnectPortsOperation(
            connector_id="p1",
            source_element_id="v1",
            source_port_id="out",
            target_element_id="i1",
            target_port_id="process",
            process_tag="L-1",
            medium="process",
            nominal_diameter="DN50",
        )
    )
    compiled = SemanticTransactionCompiler(service).compile(
        created.id,
        SemanticTransaction(operations=operations, expected_revision=0, label="schedule projection"),
    )
    assert compiled.assessment.valid and compiled.transaction is not None, [
        issue.message for issue in compiled.assessment.issues
    ]
    service.apply_transaction(created.id, compiled.transaction, source="system")
    return created.id


def main() -> None:
    registry = SymbolRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        service = DocumentService(SQLiteDocumentStore(Path(tmp) / "projection.db"), registry)
        document = service.get_document(build(service, registry))

        print("=== the drawing as the product wrote it ===")
        for element in sorted(document.elements, key=lambda item: item.id):
            if element.type == "symbol":
                print(f"  symbol {element.id}: label={element.label!r} "
                      f"properties.tag={element.properties.get('tag')!r}")
            if element.type == "text":
                print(f"  text   {element.id}: {element.text!r}")

        report = build_engineering_report(document, registry)
        print()
        print("=== schedule projection ===")
        for name, rows in (("equipment", report.equipment), ("instruments", report.instruments)):
            for row in rows:
                print(f"  {name}: {json.dumps(row.model_dump(mode='json'), ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    main()
