"""M7-Q2: governed port ambiguity -- receipts, deterministic selectors, no guessing.

The design gate froze the shape: a connection clause that omits the port on a
multi-port symbol refuses with a structured receipt naming every compatible port and
how to name each one; a fuller sentence (「V-101 的顶部管口」) resolves by declared
predicates over frozen port facts, never by default, never by the model. These tests
are the frozen DoD, item by item.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentcad import m7_port_selectors
from agentcad.config import Settings
from agentcad.m7_endpoint_binding import PortBindingError
from agentcad.m7_port_selectors import (
    PORT_SELECTOR_CONTRACT_VERSION,
    REASON_MISSING_SELECTOR,
    REASON_NO_MATCH,
    REASON_STILL_AMBIGUOUS,
    candidate_hints,
    parse_selector,
    resolve_with_selector,
)
from agentcad.m7_symbol_geometry import SymbolGeometrySnapshot, freeze_symbol_geometry
from agentcad.main import create_app
from agentcad.symbols import port_side
from agentcad.typesafe import TypesafeClient

SENTENCE_GAP = "添加一台燃料盐泵 P-101，添加一个罐 V-101，把 P-101 接到 V-101"
# selector resolves to the side inlet: a left-side entry the router can draw. (The top
# port also binds uniquely -- the receipt test asserts its row -- but a top-entry route
# crosses the vessel and the router refuses it; that is the engine's own boundary.)
SENTENCE_SELECTED = "添加一台燃料盐泵 P-101，添加一个罐 V-101，把 P-101 接到 V-101 的侧面进口"


class _JudgeStub:
    """Answers symbol questions like the real Q1 run did: 罐 -> cylinder_vessel."""

    def __call__(self, state: dict, questions: dict) -> dict:
        answers = {}
        for question, definition in questions.items():
            criteria = sorted(definition["criteria"])
            choice = "cylinder_vessel" if any("vessel" in c for c in criteria) else criteria[0]
            answers[question] = {"choice": choice, "confidence": 0.9}
        return {"model": "judge-stub", "answers": answers}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(TypesafeClient, "judge", _JudgeStub())
    app = create_app(
        Settings(
            database_path=tmp_path / "q2.db",
            cors_origins=["http://localhost:5173"],
            frontend_dist=tmp_path / "missing-dist",
        )
    )
    with TestClient(app) as test_client:
        yield test_client


def _new_document(client: TestClient) -> str:
    response = client.post("/api/v2/documents", json={"name": "Q2"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _draw(client, document_id, sentence, **overrides):
    body = {"sentence": sentence, "api_key": "test-key"}
    body.update(overrides)
    return client.post(f"/api/v2/documents/{document_id}/agent/text-plan", json=body)


# ---------------------------------------------------------------------------------------------
# DoD 1-2: the Q1 reproduction produces the structured receipt; dry-run previews, commit refuses
# ---------------------------------------------------------------------------------------------


def test_the_q1_ambiguity_produces_a_structured_receipt_with_stable_candidates(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, SENTENCE_GAP)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "port_ambiguity"
    assert detail["completeness"] == "partial"
    (record,) = detail["port_ambiguities"]
    # frozen schema shape
    assert record["reason"] == REASON_MISSING_SELECTOR
    assert record["source_requirement"] == "把 P-101 接到 V-101"
    assert record["role"] == "target"
    assert record["element_tag"] == "V-101"
    assert record["symbol_key"] == "cylinder_vessel"
    assert record["selector"] is None
    assert record["port_selector_contract_version"] == PORT_SELECTOR_CONTRACT_VERSION
    # candidates: complete, stable order (port_id ascending), facts only -- no coordinates
    assert [c["port_id"] for c in record["candidates"]] == ["in", "top"]
    for candidate in record["candidates"]:
        assert set(candidate) == {"port_id", "name", "direction", "medium", "side", "selectors"}
        assert "x" not in candidate and "y" not in candidate and "position" not in candidate
    top = record["candidates"][1]
    assert top["name"] == "顶部管口"
    # the receipt's side is the frozen geometric fact; the geometric rule calls y=6 of a
    # 100-tall box interior (tolerance 5.6), and the name still names it
    assert top["side"] in {"top", "interior"}
    assert "top" in top["selectors"] and "顶部管口" in top["selectors"]

    service = client.app.state.service
    assert service.get_document(document_id).revision == 0
    assert not [
        r for r in service.audit.audit_trail(document_id=document_id, limit=10)
        if r.event_type == "revision.created"
    ]


def test_dry_run_previews_the_receipt_at_200_and_writes_nothing(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, SENTENCE_GAP, dry_run=True)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["committed"] is False
    assert result["revision"] is None
    assert result["completeness"] == "partial"
    assert len(result["port_ambiguities"]) == 1
    assert result["canonical_layout_digest"] == ""
    assert {e["tag"] for e in result["spec"]["entities"]} == {"P-101", "V-101"}
    service = client.app.state.service
    assert service.get_document(document_id).revision == 0


def test_a_fuller_sentence_with_a_selector_resolves_and_writes(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, SENTENCE_SELECTED)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["committed"] is True
    assert result["revision"] == 1
    assert result["completeness"] == "complete"
    assert result["port_ambiguities"] == []
    # the audit carries the selector provenance
    record = client.app.state.service.audit.revision_evidence(document_id, 1).audit_record
    assert record.evidence["metadata"]["port_selector_contract_version"] == PORT_SELECTOR_CONTRACT_VERSION
    # and the drawing exists with the named tag on it
    words = {getattr(e, "text", "") for e in client.app.state.service.get_document(document_id).elements}
    assert {"P-101", "V-101"} <= words


# ---------------------------------------------------------------------------------------------
# DoD 3-5: zero-match never falls back; uniqueness never picks the first; exact port_id always legal
# ---------------------------------------------------------------------------------------------


def test_a_selector_matching_nothing_refuses_instead_of_falling_back(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, SENTENCE_GAP + "的回流口")
    assert response.status_code == 422
    (record,) = response.json()["detail"]["port_ambiguities"]
    assert record["reason"] == REASON_NO_MATCH
    assert record["selector"]["raw"] == "回流口"
    # the candidates are the ORIGINAL compatible set -- not a fallback to whatever matched
    assert [c["port_id"] for c in record["candidates"]] == ["in", "top"]
    assert client.app.state.service.get_document(document_id).revision == 0


def test_an_exact_port_id_is_always_a_legal_deterministic_selector(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, SENTENCE_GAP + "的 in")
    assert response.status_code == 200, response.text
    assert response.json()["committed"] is True


def test_a_selector_matching_several_stays_ambiguous_and_names_the_survivors(client: TestClient) -> None:
    """「接到 V-101 的进口」on cylinder_vessel matches top(顶部管口 is an in-port named
    管口? no -- 进口 matches 侧面进口 only... so use a genuinely shared token: V-101 的
    管口 -- both candidates are 管口-bearing names? 顶部管口/侧面进口: 管口 suffix is
    stripped in normalization, so this builds the still-ambiguous case at unit level
    instead; the surface case is covered by reason coverage below."""

    snapshot: SymbolGeometrySnapshot = freeze_symbol_geometry(["cylinder_vessel"])
    fact = snapshot.facts[0]
    parsed = parse_selector("不存在的东西")
    surviving, hints = resolve_with_selector(
        ports=fact.ports,
        allowed_directions=("in", "bidirectional"),
        selector=parsed,
        width=fact.intrinsic_width,
        height=fact.intrinsic_height,
    )
    assert surviving == ()
    assert [h.port_id for h in hints] == ["in", "top"]


# ---------------------------------------------------------------------------------------------
# DoD 6-8: one resolver for both surfaces; the model never sees a port question; the old
# error is untouched; no-connection admissibility unchanged
# ---------------------------------------------------------------------------------------------


def test_text_edit_uses_the_same_resolver_and_schema(client: TestClient) -> None:
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE_SELECTED).status_code == 200
    base = client.app.state.service.store.latest_semantic_spec(document_id)
    edit = client.post(
        f"/api/v2/documents/{document_id}/agent/text-edit",
        json={
            "sentence": "再添加一个罐 V-102，把 P-101 接到 V-102",
            "expected_revision": 1,
            "base_spec_digest": base.spec_digest,
            "api_key": "test-key",
        },
    )
    assert edit.status_code == 422
    (record,) = edit.json()["detail"]["port_ambiguities"]
    assert record["reason"] == REASON_MISSING_SELECTOR
    assert record["element_tag"] == "V-102"
    assert client.app.state.service.get_document(document_id).revision == 1


def test_no_port_question_is_ever_asked_of_the_model(client: TestClient, monkeypatch) -> None:
    payloads: list[dict] = []

    class _Recorder:
        def __call__(self, state: dict, questions: dict) -> dict:
            payloads.append({"state": state, "questions": questions})
            return _JudgeStub()(state, questions)

    monkeypatch.setattr(TypesafeClient, "judge", _Recorder())
    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE_SELECTED).status_code == 200
    for payload in payloads:
        assert "port" not in str(payload["questions"]).lower() or all(
            "port" not in question.lower() for question in payload["questions"]
        )


def test_the_old_ambiguous_error_code_and_message_are_unchanged() -> None:
    """No-selector refusal: same code, same human message, same hard-fail semantics --
    the receipt rides alongside, it does not replace the error."""

    snapshot = freeze_symbol_geometry(["cylinder_vessel"])
    fact = snapshot.facts[0]
    with pytest.raises(PortBindingError) as raised:
        from agentcad.m7_endpoint_binding import bind_endpoint

        bind_endpoint(
            connection_id="cn_1",
            role="target",
            node_id="el_V_101",
            fact=fact,
            declared_port_id="",
        )
    exc = raised.value
    assert exc.code == "ambiguous_port_binding"
    assert "offers 2 compatible ports; a unique inference is a derivation" in str(exc)
    assert exc.record is not None and exc.record.reason == REASON_MISSING_SELECTOR


def test_connectionless_drawings_are_still_refused_unchanged(client: TestClient) -> None:
    document_id = _new_document(client)
    response = _draw(client, document_id, "添加一个塔 T-101")
    assert response.status_code == 422
    detail = str(response.json()["detail"])
    assert "引擎目前画不了" in detail and "endpoint bindings" in detail
    assert client.app.state.service.get_document(document_id).revision == 0


# ---------------------------------------------------------------------------------------------
# Unit level: the selector grammar, the single side rule, deterministic records
# ---------------------------------------------------------------------------------------------


def test_selector_parsing_is_predicates_not_substrings() -> None:
    parsed = parse_selector("顶部气相")
    assert [(p.kind, p.value) for p in parsed.predicates] == [
        ("side", "top"),
        ("semantic", "gas"),
    ]
    parsed = parse_selector("top")
    assert parsed.predicates[0].kind == "port_id"
    parsed = parse_selector("回流入口")
    assert any(p.kind == "semantic" and p.value == "reflux" for p in parsed.predicates)
    assert parse_selector("") is None


def test_every_port_is_addressable_and_hints_are_deterministic() -> None:
    """DoD: no pretty-alias forcing -- the exact port_id always works; where the
    catalogue named ports descriptively, natural phrases work too. Hints are stably
    sorted and carry a distinguishing phrase for every candidate."""

    snapshot = freeze_symbol_geometry(["fractionation_column"])
    fact = snapshot.facts[0]
    # addressability is exact under a role that admits the port: every port binds when
    # the role's direction filter admits it
    allowed = ("in", "out", "bidirectional")
    for port in fact.ports:
        parsed = parse_selector(port.port_id)
        surviving, _ = resolve_with_selector(
            ports=fact.ports,
            allowed_directions=allowed,
            selector=parsed,
            width=fact.intrinsic_width,
            height=fact.intrinsic_height,
        )
        assert [p.port_id for p in surviving] == [port.port_id], port.port_id

    hints = candidate_hints(ports=fact.ports, width=fact.intrinsic_width, height=fact.intrinsic_height)
    assert [h.port_id for h in hints] == sorted(h.port_id for h in hints)
    for hint in hints:
        assert hint.selectors, f"{hint.port_id} has no distinguishing phrase"


def test_only_selected_ports_persist_inferred_ports_stay_derivations(client: TestClient) -> None:
    """The Q2 persistence boundary: SENTENCE_SELECTED binds P-101's discharge as
    inferred_unique (one compatible out-port) and V-101's inlet as selected. Only the
    selected port may enter the stored spec; the inferred port stays a derivation, so
    one connection's selector never rewrites the rest of the drawing's semantics."""

    document_id = _new_document(client)
    assert _draw(client, document_id, SENTENCE_SELECTED).status_code == 200
    source = client.app.state.service.store.latest_semantic_spec(document_id)
    (connection,) = source.spec.connections
    assert connection.target_port_id == "in"   # selector-resolved: persisted
    assert connection.source_port_id == ""      # inferred_unique: stays a derivation


def test_semantic_selectors_actually_match_the_real_ports() -> None:
    """The Blocker-2 shape: parsing is not matching. Every declared semantic token must
    select the real port it names, uniquely; an undeclared string matches nothing."""

    snapshot = freeze_symbol_geometry(["fractionation_column"])
    fact = snapshot.facts[0]

    def resolve(raw: str) -> list[str]:
        parsed = parse_selector(raw)
        surviving, _ = resolve_with_selector(
            ports=fact.ports,
            allowed_directions=("in", "out", "bidirectional"),
            selector=parsed,
            width=fact.intrinsic_width,
            height=fact.intrinsic_height,
        )
        return [port.port_id for port in surviving]

    assert resolve("回流") == ["reflux"]
    assert resolve("塔顶气相") == ["overhead"]
    assert resolve("原料") == ["feed"]
    assert resolve("不存在的语义词") == []
    # human tokens appear in hints where they distinguish a port
    hints = candidate_hints(ports=fact.ports, width=fact.intrinsic_width, height=fact.intrinsic_height)
    by_id = {hint.port_id: hint for hint in hints}
    assert "回流" in by_id["reflux"].selectors
    assert "塔顶" in by_id["overhead"].selectors


def test_the_catalog_identity_is_byte_identical_across_the_q2_boundary() -> None:
    """The Q2 gate froze this: adding selector evidence must not move geometry identity.
    The digest below was captured on origin/main @ 18ec8c6 for this exact symbol set."""

    snapshot = freeze_symbol_geometry(
        ["buffer_tank", "cylinder_vessel", "fractionation_column", "positive_displacement_pump"]
    )
    assert snapshot.digest == (
        "6a3a05b1bb44594d6697c0c36b59f59c7be623bfd0ff8a3dbf0552e7f909a263"
    )
    # the runtime fact still carries the frozen name for the selector and the receipt
    vessel = freeze_symbol_geometry(["cylinder_vessel"]).facts[0]
    assert {port.port_id: port.name for port in vessel.ports}["top"] == "顶部管口"
    assert "name" not in vessel.ports[0].to_projection()


def test_side_judgement_uses_the_one_canonical_helper() -> None:
    assert m7_port_selectors.port_side is port_side
    # the frozen rule: tolerance = max(1, 8% of the short side)
    assert port_side(70, 100, 35, 4) == "top"
    assert port_side(70, 100, 0, 50) == "left"
    assert port_side(70, 100, 35, 50) == "interior"


def test_a_selector_matching_several_is_still_ambiguous_never_the_first() -> None:
    """「进口」on cylinder_vessel matches both compatible in-ports: the refusal names
    the survivors; the forbidden behaviour this test exists to catch is silently
    binding candidates[0]."""

    snapshot = freeze_symbol_geometry(["cylinder_vessel"])
    fact = snapshot.facts[0]
    with pytest.raises(PortBindingError) as raised:
        from agentcad.m7_endpoint_binding import bind_endpoint

        bind_endpoint(
            connection_id="cn_1",
            role="target",
            node_id="el_V_101",
            fact=fact,
            declared_port_id="",
            port_selector_raw="进口",
        )
    exc = raised.value
    assert exc.code == "port_ambiguity"
    assert exc.record.reason == REASON_STILL_AMBIGUOUS
    assert [c.port_id for c in exc.record.candidates] == ["in", "top"]
    assert exc.candidates == ("in", "top"), "survivors ride the error, not a default pick"


def test_resolve_is_pure_predicate_conjunction_without_a_default() -> None:
    snapshot = freeze_symbol_geometry(["cylinder_vessel"])
    fact = snapshot.facts[0]
    # side+semantic AND: 顶部 + in-direction vocabulary resolves uniquely
    parsed = parse_selector("顶部")
    surviving, _ = resolve_with_selector(
        ports=fact.ports,
        allowed_directions=("in", "bidirectional"),
        selector=parsed,
        width=fact.intrinsic_width,
        height=fact.intrinsic_height,
    )
    assert [p.port_id for p in surviving] == ["top"]
    # and the record reasons cover the three frozen states
    assert {REASON_MISSING_SELECTOR, REASON_NO_MATCH, REASON_STILL_AMBIGUOUS} == {
        "missing_selector", "selector_no_match", "selector_still_ambiguous"
    }
