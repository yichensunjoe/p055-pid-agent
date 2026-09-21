"""Regression tests for the M4 tag-resolution fix.

The bug is worth restating because the tests are shaped by it: the production polish moves
a symbol's fixed ``label`` into an editable ``symbol_label`` annotation and clears the
field, while ``TAG_MISSING``/``TAG_DUPLICATE`` read the field directly. The result was that
on any drawing the product itself produced, every symbol looked untagged and a duplicate
tag could not be reported at all.

So these tests are not "the resolver works". They are "a drawing that the production write
path produced is judged correctly", and the production path is exercised as itself — compile,
governed apply, polish — not simulated by hand-editing a document into the shape we hope the
polish produces.
"""

from __future__ import annotations

from pathlib import Path

from agentcad.models import (
    AddElementOperation,
    CreateDocumentRequest,
    Document,
    Point,
    SymbolElement,
    TextElement,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.tag_resolver import (
    SOURCE_ANNOTATION,
    SOURCE_NONE,
    SOURCE_PROPERTIES_TAG,
    SOURCE_SYMBOL_LABEL,
    describe_symbol_tag,
    resolve_symbol_tag,
)
from agentcad.validation_engine import run_validation
from agentcad.validation_profile import built_in_profile, resolve_profile


def _registry() -> SymbolRegistry:
    return SymbolRegistry()


def _service(tmp_path: Path) -> DocumentService:
    return DocumentService(SQLiteDocumentStore(tmp_path / "tags.db"), _registry())


def _symbol(element_id: str, tag: str, *, x: float = 200.0) -> SymbolElement:
    definition = _registry().get("ball_valve")
    return SymbolElement(
        id=element_id,
        symbol_key="ball_valve",
        position=Point(x=x, y=300),
        width=definition.width,
        height=definition.height,
        label=tag,
        properties={"tag": tag},
    )


def _polished_pair(service: DocumentService, tags: tuple[str, str]) -> str:
    """Two valves joined by a line, written the way the product writes them.

    ``polish_full_diagram_transaction`` runs inside the compiler whenever a transaction
    covers the whole of a document, which is exactly what this is: so the returned drawing
    has empty ``symbol.label`` fields and ``symbol_label`` annotations, and nothing in the
    test had to fake that.
    """

    from agentcad.agent_semantic_models import ConnectPortsOperation, SemanticTransaction
    from agentcad.semantic_compiler_engine import SemanticTransactionCompiler

    document = service.create_document(CreateDocumentRequest(name="tag resolution fixture"))
    operations = [
        AddElementOperation(element=_symbol("v1", tags[0], x=200)),
        AddElementOperation(element=_symbol("v2", tags[1], x=700)),
        ConnectPortsOperation(
            connector_id="p1",
            source_element_id="v1",
            source_port_id="out",
            target_element_id="v2",
            target_port_id="in",
            process_tag="L-1",
            medium="process",
            nominal_diameter="DN50",
        ),
    ]
    compiled = SemanticTransactionCompiler(service).compile(
        document.id,
        SemanticTransaction(operations=operations, expected_revision=0, label="tag fixture"),
    )
    assert compiled.assessment.valid and compiled.transaction is not None, [
        issue.message for issue in compiled.assessment.issues
    ]
    service.apply_transaction(document.id, compiled.transaction, source="system")
    return document.id


def _codes(service: DocumentService, document_id: str) -> list[tuple[str, str, tuple[str, ...]]]:
    document = service.get_document(document_id)
    result = run_validation(
        document, service.symbols, resolve_profile(built_in_profile()), service=service
    )
    return sorted(
        (issue.validator_id, issue.code, tuple(sorted(issue.element_ids)))
        for issue in result.issues
    )


def _messages(service: DocumentService, document_id: str, code: str) -> list[str]:
    document = service.get_document(document_id)
    result = run_validation(
        document, service.symbols, resolve_profile(built_in_profile()), service=service
    )
    return sorted(issue.message for issue in result.issues if issue.code == code)


# -- the resolver itself ----------------------------------------------------- #


def _bare_document(
    symbols: list[SymbolElement], texts: list[TextElement] | None = None
) -> Document:
    return Document(id="doc_bare", name="bare", elements=[*symbols, *(texts or [])])


def _annotation(
    element_id: str, text: str, *, role: str | None = "symbol_label", suffix: str = "label"
) -> TextElement:
    metadata: dict[str, object] = {"parent_element_id": element_id}
    if role is not None:
        metadata["annotation_role"] = role
    return TextElement(
        id=f"{element_id}__{suffix}", position=Point(x=0, y=0), text=text, metadata=metadata
    )


def test_properties_tag_outranks_every_other_source():
    symbol = _symbol("v1", "LABEL-TAG")
    symbol = symbol.model_copy(update={"properties": {"tag": "PROP-TAG"}})
    document = _bare_document([symbol], [_annotation("v1", "ANNOTATION-TAG")])
    described = describe_symbol_tag(document, symbol)
    assert described.tag == "PROP-TAG"
    assert described.source == SOURCE_PROPERTIES_TAG


def test_legacy_label_is_used_when_there_is_no_properties_tag():
    """A drawing that never went through polish must keep behaving exactly as it did."""

    symbol = SymbolElement(
        id="v1",
        symbol_key="ball_valve",
        position=Point(x=0, y=0),
        width=60,
        height=40,
        label="  HV-900  ",
    )
    document = _bare_document([symbol])
    described = describe_symbol_tag(document, symbol)
    assert described.tag == "HV-900"
    assert described.source == SOURCE_SYMBOL_LABEL


def test_the_annotation_is_read_only_when_it_names_the_symbol_as_its_subject():
    symbol = SymbolElement(
        id="v1", symbol_key="ball_valve", position=Point(x=0, y=0), width=60, height=40, label=""
    )
    # An annotation for a *different* symbol, and the virtual label the overlap rules
    # synthesise, are both explicitly not tag sources: neither carries the role.
    document = _bare_document(
        [symbol],
        [
            _annotation("v2", "WRONG-SUBJECT"),
            _annotation("v1", "VIRTUAL", role=None, suffix="virtual"),
            _annotation("v1", "HV-777"),
        ],
    )
    described = describe_symbol_tag(document, symbol)
    assert described.tag == "HV-777"
    assert described.source == SOURCE_ANNOTATION


def test_nothing_anywhere_resolves_to_no_tag():
    symbol = SymbolElement(
        id="v1", symbol_key="ball_valve", position=Point(x=0, y=0), width=60, height=40, label="  "
    )
    document = _bare_document([symbol], [_annotation("v1", "   ")])
    described = describe_symbol_tag(document, symbol)
    assert described.tag == ""
    assert described.source == SOURCE_NONE
    assert resolve_symbol_tag(document, symbol) == ""


def test_conflicting_annotations_resolve_deterministically():
    """Two different texts on one subject must not resolve by iteration order."""

    symbol = SymbolElement(
        id="v1", symbol_key="ball_valve", position=Point(x=0, y=0), width=60, height=40, label=""
    )
    forward = _bare_document(
        [symbol], [_annotation("v1", "HV-2", suffix="a"), _annotation("v1", "HV-1", suffix="b")]
    )
    backward = _bare_document(
        [symbol], [_annotation("v1", "HV-1", suffix="a"), _annotation("v1", "HV-2", suffix="b")]
    )
    first = describe_symbol_tag(forward, symbol)
    second = describe_symbol_tag(backward, symbol)
    assert first.tag == second.tag == "HV-1"
    assert first.conflicting_annotations == ("HV-1", "HV-2")
    # Repeated identical texts are not a conflict: they simply agree.
    agreeing = _bare_document(
        [symbol], [_annotation("v1", "HV-1", suffix="a"), _annotation("v1", "HV-1", suffix="b")]
    )
    assert describe_symbol_tag(agreeing, symbol).conflicting_annotations == ()


def test_resolving_a_tag_never_mutates_the_document():
    symbol = SymbolElement(
        id="v1", symbol_key="ball_valve", position=Point(x=0, y=0), width=60, height=40, label=""
    )
    document = _bare_document([symbol], [_annotation("v1", "HV-1")])
    before = document.model_dump(mode="json")
    resolve_symbol_tag(document, symbol)
    describe_symbol_tag(document, symbol)
    assert document.model_dump(mode="json") == before


# -- what the canonical rules conclude --------------------------------------- #


def test_a_freshly_drafted_drawing_is_not_reported_as_untagged(tmp_path: Path):
    """The bug, at the level the reviewer sees: a correct drawing must not be called wrong."""

    service = _service(tmp_path)
    document_id = _polished_pair(service, ("HV-101", "HV-102"))
    document = service.get_document(document_id)
    # The fixture has to actually be in the polished shape for this test to mean anything.
    assert {element.label for element in document.elements if element.type == "symbol"} == {""}
    assert all(
        element.metadata.get("annotation_role") == "symbol_label"
        for element in document.elements
        if element.type == "text"
    )
    codes = {code for _, code, _ in _codes(service, document_id)}
    assert "TAG_MISSING" not in codes
    assert "TAG_DUPLICATE" not in codes


def test_a_real_duplicate_tag_on_a_polished_drawing_is_reported(tmp_path: Path):
    service = _service(tmp_path)
    document_id = _polished_pair(service, ("HV-101", "HV-101"))
    codes = {(code, element_ids) for _, code, element_ids in _codes(service, document_id)}
    assert ("TAG_DUPLICATE", ("v1", "v2")) in codes
    # The engineering graph is a separate canonical layer and keeps its own rule; both
    # firing on one real duplicate is correct, and no suppression is added to hide it.
    assert ("IR_DUPLICATE_IDENTITY", ()) in {
        (code, element_ids) for _, code, element_ids in _codes(service, document_id)
    }
    assert "TAG_MISSING" not in {code for _, code, _ in _codes(service, document_id)}
    assert any("HV-101" in message for message in _messages(service, document_id, "TAG_DUPLICATE"))


def test_an_untagged_symbol_is_still_reported_when_every_source_is_empty(tmp_path: Path):
    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="untagged"))
    untagged = SymbolElement(
        id="v1",
        symbol_key="ball_valve",
        position=Point(x=200, y=300),
        width=60,
        height=40,
        label="",
    )
    service.apply_transaction(
        document.id,
        __import__("agentcad.models", fromlist=["TransactionRequest"]).TransactionRequest(
            operations=[AddElementOperation(element=untagged)],
            expected_revision=document.revision,
            label="untagged",
        ),
        source="system",
    )
    codes = {code for _, code, _ in _codes(service, document.id)}
    assert "TAG_MISSING" in codes


def test_annotation_only_tags_can_duplicate_each_other(tmp_path: Path):
    """The tag source that only exists after polish has to be able to duplicate."""

    service = _service(tmp_path)
    document = service.create_document(CreateDocumentRequest(name="annotation only"))
    symbols = [
        SymbolElement(
            id=element_id,
            symbol_key="ball_valve",
            position=Point(x=x, y=300),
            width=60,
            height=40,
            label="",
        )
        for element_id, x in (("v1", 200), ("v2", 700))
    ]
    texts = [_annotation("v1", "HV-101"), _annotation("v2", "HV-101")]
    from agentcad.models import TransactionRequest

    service.apply_transaction(
        document.id,
        TransactionRequest(
            operations=[
                *(AddElementOperation(element=item) for item in symbols),
                *(AddElementOperation(element=item) for item in texts),
            ],
            expected_revision=document.revision,
            label="annotation only",
        ),
        source="system",
    )
    assert "TAG_DUPLICATE" in {code for _, code, _ in _codes(service, document.id)}


def test_the_required_port_finding_fires_exactly_as_before_and_only_its_name_changed(
    tmp_path: Path,
):
    service = _service(tmp_path)
    document_id = _polished_pair(service, ("HV-101", "HV-102"))
    findings = [
        (code, element_ids)
        for _, code, element_ids in _codes(service, document_id)
        if code == "SYMBOL_REQUIRED_PORT_UNCONNECTED"
    ]
    # Unchanged trigger and unchanged locators: the two unbound valve ports and the two
    # connectors' own ports, exactly as the pre-fix engine reported them for this shape.
    assert findings
    assert {element_ids for _, element_ids in findings} == {("v1",), ("v2",)}
    messages = _messages(service, document_id, "SYMBOL_REQUIRED_PORT_UNCONNECTED")
    assert any("HV-101" in message for message in messages)
    assert any("HV-102" in message for message in messages)
    # The display name used to be the raw id because the label had been cleared; that is
    # the only thing this fix changed here.
    assert not any("的端口" in message and message.startswith("v1 ") for message in messages)
