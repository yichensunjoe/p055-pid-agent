"""The TypeSafe drawing path: credentials, candidates, and a plan that refuses what it cannot judge.

The provider is reached through an injected transport, so these tests never open a socket and never
need a key: what is being tested is the *shape* of the decision -- which clauses become which
questions, which answers become which operations, and what happens when the model is not sure.
"""

from __future__ import annotations

import pytest

from agentcad.agent_semantic_models import AddElementOperation, ConnectPortsOperation
from agentcad.models import (
    CreateDocumentRequest,
    Point,
    SymbolElement,
    TransactionRequest,
)
from agentcad.service import DocumentService
from agentcad.store import SQLiteDocumentStore
from agentcad.symbols import SymbolRegistry
from agentcad.typesafe import (
    TYPESAFE_ENV_API_KEY,
    TypesafeClient,
    TypesafeConfig,
    TypesafeError,
    resolve_typesafe_config,
    typesafe_status,
)
from agentcad.typesafe_planner import (
    TypesafeSemanticPlanner,
    candidate_symbols,
    confidence_of,
    split_clauses,
)


class _Recorder:
    """A transport that answers from a script and keeps the payload it was asked with."""

    def __init__(self, answers: dict[str, dict] | None = None, *, model: str = "jev-latest"):
        self.answers = answers or {}
        self.model = model
        self.payloads: list[dict] = []

    def __call__(self, config: TypesafeConfig, payload: dict) -> dict:
        self.payloads.append(payload)
        answered = {question: self._answer(payload, question) for question in payload["questions"]}
        return {"model": self.model, "usage": {"in": 10, "out": 2}, "answers": answered}

    def _answer(self, payload: dict, question: str) -> dict:
        if question in self.answers:
            return self.answers[question]
        definition = payload["questions"][question]
        if definition.get("type") == "choice":
            return {"choice": sorted(definition["criteria"])[0], "confidence": 0.9}
        return {"probability": 0.9, "confidence": 0.9}


# -- credentials -------------------------------------------------------------- #


def test_key_precedence_and_missing_key(monkeypatch) -> None:
    """The request wins, the environment is the fallback, and neither means an explicit error."""

    assert resolve_typesafe_config(api_key="r", environ={TYPESAFE_ENV_API_KEY: "e"}).source == "request"
    from_environment = resolve_typesafe_config(environ={TYPESAFE_ENV_API_KEY: "e"})
    assert (from_environment.api_key, from_environment.source) == ("e", "environment")
    with pytest.raises(TypesafeError) as error:
        resolve_typesafe_config(environ={})
    assert error.value.code == "typesafe_key_missing"
    assert error.value.status_code == 400
    # The key never appears in the publishable description -- that is what it is for.
    described = resolve_typesafe_config(api_key="secret-value").describe()
    assert described["api_key_present"] is True
    assert "secret-value" not in repr(described)


def test_status_reports_configuration_without_calling() -> None:
    configured = typesafe_status(api_key="k")
    assert configured["configured"] is True
    assert configured["api_key_present"] is True
    # The status names the key's presence and never the key: "api_key" itself is not a field.
    assert "api_key" not in configured
    unconfigured = typesafe_status(api_key=None, base_url=None, model=None)
    assert unconfigured["configured"] in {True, False}  # the environment may hold one already
    assert unconfigured["api_key_present"] is unconfigured["configured"]


def test_client_never_reports_a_partial_judgment() -> None:
    """A batch that comes back without an answer is an error, not a default."""

    transport = _Recorder()
    client = TypesafeClient(TypesafeConfig(api_key="k"), transport=transport)
    answer = client.judge({"probe": 1}, {"q": {"type": "noul", "instructions": {"question": "?"}}})
    assert answer["answers"]["q"]["confidence"] == 0.9
    partial = TypesafeClient(
        TypesafeConfig(api_key="k"),
        transport=lambda config, payload: {"answers": {}},
    )
    with pytest.raises(TypesafeError) as error:
        partial.judge({"probe": 1}, {"q": {"type": "noul", "instructions": {"question": "?"}}})
    assert error.value.code == "typesafe_judgment_missing"


# -- candidates and clauses --------------------------------------------------- #


def test_clauses_are_classified_by_verb() -> None:
    clauses = split_clauses("新增一台燃料盐泵，把缓冲罐接到分离塔；说明一下画布")
    assert [clause.kind for clause in clauses] == ["add", "connect", "unknown"]


def test_candidate_symbols_narrow_by_phrase() -> None:
    registry = SymbolRegistry()
    pumps = candidate_symbols(registry, "新增一台泵")
    assert pumps, "the catalogue must offer pump candidates for a pump phrase"
    family = ("pump", "blower", "compressor", "fan")
    assert all(
        any(hint in f"{row.key} {row.name} {row.category}".casefold() for hint in family)
        for row in pumps
    )
    assert len(pumps) <= 24
    # A phrase with no hint offers the catalogue rather than an empty question.
    assert candidate_symbols(registry, "新增一个设备")


# -- the plan ----------------------------------------------------------------- #


def _service(tmp_path) -> tuple[DocumentService, str]:
    service = DocumentService(SQLiteDocumentStore(tmp_path / "typesafe.db"), SymbolRegistry())
    document = service.create_document(
        CreateDocumentRequest(name="typesafe drawing"), source="system"
    )
    return service, document.id


def _symbol_key(registry: SymbolRegistry) -> str:
    return next(row.key for row in registry.list() if "pump" in row.key)


def _place(service: DocumentService, document_id: str, label: str, x: float) -> str:
    element_id = f"sym_{label}_{int(x)}"
    definition = service.symbols.get(_symbol_key(service.symbols))
    service.apply_transaction(
        document_id,
        TransactionRequest(
            operations=[
                AddElementOperation(
                    element=SymbolElement(
                        id=element_id,
                        symbol_key=definition.key,
                        position=Point(x=x, y=120),
                        width=definition.width,
                        height=definition.height,
                        label=label,
                        properties={"tag": label},
                    )
                )
            ],
            label="seed",
        ),
    )
    return element_id


def test_confidence_reads_every_primitive_shape_a_live_answer_can_have() -> None:
    """A live ``noul`` answers ``{"type": "noul", "noul": 0.97}`` -- not a Choice distribution.

    Measured against api.typesafe.ai, so the shapes below are the service's, not a guess: reading
    only ``confidence``/``probabilities`` would score a sure presence judgment as 0.0 and quietly
    skip a clause the model was certain about.
    """

    assert confidence_of({"type": "noul", "noul": 0.97}) == 0.97
    assert confidence_of({"choice": "centrifugal_pump", "confidence": 0.88}) == 0.88
    assert confidence_of({"probabilities": {"a": 0.2, "b": 0.7}}) == 0.7
    assert confidence_of({}) == 0.0
    # The Choice distribution wins when both are present: it is the more specific number.
    assert confidence_of({"confidence": 0.5, "probabilities": {"a": 0.9}}) == 0.9


def test_plan_adds_the_symbol_the_judgment_chose(tmp_path) -> None:
    service, document_id = _service(tmp_path)
    recorder = _Recorder()
    planner = TypesafeSemanticPlanner(service, service.symbols, client_factory=lambda config: TypesafeClient(config, transport=recorder))

    plan = planner.plan(
        document_id,
        _input("新增一台泵 TAG-P101"),
    )

    operations = plan.transaction.operations
    assert len(operations) == 1
    assert isinstance(operations[0], AddElementOperation)
    assert operations[0].element.symbol_key in {row.key for row in candidate_symbols(service.symbols, "泵")}
    assert operations[0].element.label == "TAG-P101"
    # The question carried the candidates, and the state carried only names -- never a key.
    assert recorder.payloads[0]["model"] == "jev-latest"
    assert "api_key" not in repr(recorder.payloads[0])


def test_plan_connects_two_placed_devices(tmp_path) -> None:
    service, document_id = _service(tmp_path)
    _place(service, document_id, "T-101", 100)
    _place(service, document_id, "T-102", 300)
    recorder = _Recorder()
    planner = TypesafeSemanticPlanner(service, service.symbols, client_factory=lambda config: TypesafeClient(config, transport=recorder))

    plan = planner.plan(document_id, _input("把 T-101 接到 T-102"))

    operations = plan.transaction.operations
    assert len(operations) == 1
    assert isinstance(operations[0], ConnectPortsOperation)
    assert {operations[0].source_element_id, operations[0].target_element_id} == {"sym_T-101_100", "sym_T-102_300"}
    assert operations[0].source_port_id and operations[0].target_port_id


def test_a_judgment_below_the_floor_is_skipped_not_guessed(tmp_path) -> None:
    """The whole point of a calibrated number: when nothing is close, nothing is drawn."""

    service, document_id = _service(tmp_path)
    recorder = _Recorder({"add_0": {"choice": "", "confidence": 0.05}})
    planner = TypesafeSemanticPlanner(service, service.symbols, client_factory=lambda config: TypesafeClient(config, transport=recorder))

    with pytest.raises(TypesafeError) as error:
        planner.plan(document_id, _input("新增一台泵"))
    assert error.value.code == "typesafe_no_operation_selected"
    assert "0.05" in error.value.message or "阈值" in error.value.message


def test_a_choice_outside_the_candidates_is_refused(tmp_path) -> None:
    service, document_id = _service(tmp_path)
    recorder = _Recorder({"add_0": {"choice": "flux_capacitor", "confidence": 0.99}})
    planner = TypesafeSemanticPlanner(service, service.symbols, client_factory=lambda config: TypesafeClient(config, transport=recorder))

    with pytest.raises(TypesafeError) as error:
        planner.plan(document_id, _input("新增一台泵"))
    assert error.value.code == "typesafe_no_operation_selected"


def test_the_route_is_mounted_and_a_missing_key_is_an_explicit_400(tmp_path, monkeypatch) -> None:
    """The UI has to be able to *ask* whether a key works, and to hear a clear no when there is none."""

    from fastapi.testclient import TestClient

    from agentcad.config import Settings
    from agentcad.main import create_app

    monkeypatch.delenv(TYPESAFE_ENV_API_KEY, raising=False)
    client = TestClient(
        create_app(
            Settings(
                database_path=tmp_path / "typesafe-route.db",
                cors_origins=["http://localhost:5173"],
                frontend_dist=tmp_path / "missing-dist",
            )
        )
    )
    status = client.get("/api/v2/provider/typesafe/status")
    assert status.status_code == 200
    body = status.json()
    assert body["configured"] is False
    assert body["error_code"] == "typesafe_key_missing"
    assert "api_key" not in body

    rejected = client.post("/api/v2/provider/typesafe/verify", json={})
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "typesafe_key_missing"


def test_the_route_reports_a_key_that_only_the_server_environment_holds(tmp_path, monkeypatch) -> None:
    """The panel's whole reason for asking: a key exported in a shell profile is invisible to the browser.

    This is the shape the local development machine has -- ``TYPESAFE_API_KEY`` in ``~/.zshrc`` -- and the
    panel must be able to say "leave the field empty, the server has one" instead of "no key configured".
    """

    from fastapi.testclient import TestClient

    from agentcad.config import Settings
    from agentcad.main import create_app

    monkeypatch.setenv(TYPESAFE_ENV_API_KEY, "server-side-key")
    client = TestClient(
        create_app(
            Settings(
                database_path=tmp_path / "typesafe-route-env.db",
                cors_origins=["http://localhost:5173"],
                frontend_dist=tmp_path / "missing-dist",
            )
        )
    )
    body = client.get("/api/v2/provider/typesafe/status").json()
    assert body["configured"] is True
    assert body["api_key_present"] is True
    assert body["api_key_source"] == "environment"
    assert body["error_code"] is None
    assert "server-side-key" not in str(body)


def _input(prompt: str):
    from agentcad.api_semantic_agent import _TypesafePlanInput

    return _TypesafePlanInput(
        prompt=prompt,
        typesafe_config=TypesafeConfig(api_key="test-key"),
        expected_revision=None,
    )
